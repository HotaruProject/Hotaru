from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .modules import HmodLoader, LoadedModule
from .tasks import TaskSupervisor
from relay.firewall import module_scope


class ActivationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ActiveModule:
    loaded: LoadedModule
    context: Any


Starter = Callable[[LoadedModule], Any]


@dataclass(frozen=True, slots=True)
class ModuleInstance:
    loaded: LoadedModule
    namespace: dict[str, Any]
    commands: tuple[str, ...]


class ModuleBinder:
    def bind(self, loaded: LoadedModule, namespace: dict[str, Any], kernel: Any, *, is_kernel: bool = False) -> tuple[str, ...]:
        handlers: list[tuple[str, Any]] = []
        for name in loaded.manifest.commands:
            if not name.isidentifier():
                raise ActivationError(f"module command is invalid: {name}")
            handler = namespace.get(f"command_{name}")
            if not callable(handler):
                raise ActivationError(f"module handler is missing: {name}")
            handlers.append((name, handler))
        
        watcher_handlers: list[tuple[str, Any]] = []
        for name in loaded.manifest.watchers:
            if not name.isidentifier():
                raise ActivationError(f"module watcher is invalid: {name}")
            handler = namespace.get(f"watcher_{name}")
            if not callable(handler):
                raise ActivationError(f"watcher handler is missing: {name}")
            watcher_handlers.append((name, handler))

        bound: list[str] = []
        bound_watchers: list[str] = []
        try:
            for name, handler in handlers:
                kernel.registry.register(
                    name,
                    handler,
                    module_id=loaded.manifest.module_id,
                    kernel=is_kernel,
                )
                bound.append(name)
            for name, handler in watcher_handlers:
                kernel.registry.register_watcher(name, handler, module_id=loaded.manifest.module_id, sandbox=False)
                bound_watchers.append(name)
            for alias, command in loaded.manifest.aliases.items():
                kernel.registry.register_alias(alias, command)
        except Exception as exc:
            for name in bound:
                kernel.unregister_module_command(loaded.manifest.module_id, name)
            for name in bound_watchers:
                kernel.registry.unregister_watcher(name, loaded.manifest.module_id)
            for alias in loaded.manifest.aliases:
                kernel.registry.unregister_alias(alias)
            raise ActivationError(f"module command binding failed: {loaded.manifest.module_id}") from exc
        return tuple(bound)

    def unbind(self, loaded: LoadedModule, commands: tuple[str, ...], kernel: Any, *, is_kernel: bool = False) -> None:
        for name in commands:
            if is_kernel and hasattr(kernel.registry, "unregister_kernel"):
                kernel.registry.unregister_kernel(name, module_id=loaded.manifest.module_id)
            else:
                kernel.unregister_module_command(loaded.manifest.module_id, name)
        for name in loaded.manifest.watchers:
            kernel.registry.unregister_watcher(name, loaded.manifest.module_id)
        for alias in loaded.manifest.aliases:
            kernel.registry.unregister_alias(alias)

    def bind_sandbox(self, loaded: LoadedModule, kernel: Any) -> tuple[str, ...]:
        for name in loaded.manifest.commands:
            if not name.isidentifier():
                raise ActivationError(f"module command is invalid: {name}")
        for name in loaded.manifest.commands:
            kernel.registry.register(
                name,
                None,
                module_id=loaded.manifest.module_id,
                sandbox=True,
            )
        for name in loaded.manifest.watchers:
            kernel.registry.register_watcher(name, None, module_id=loaded.manifest.module_id, sandbox=True)
        for alias, command in loaded.manifest.aliases.items():
            kernel.registry.register_alias(alias, command)
        return tuple(loaded.manifest.commands)


class ModuleManager:
    def __init__(
        self,
        loader: HmodLoader | None = None,
        *,
        timeout: float = 5.0,
        tasks: TaskSupervisor | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.loader = loader or HmodLoader()
        self.binder = ModuleBinder()
        self.timeout = timeout
        self.tasks = tasks
        self.form_cleanup: Callable[[str], Any] | None = None
        self._active: dict[str, ActiveModule] = {}
        self._bindings: dict[str, tuple[Any, tuple[str, ...], bool]] = {}  # kernel, commands, is_kernel
        self._rehydrators: dict[str, Callable[[dict[str, Any]], Any]] = {}

    def _register_rehydrator(self, module_id: str, namespace: dict[str, Any]) -> None:
        callback = namespace.get("rehydrate_form")
        if callable(callback):
            self._rehydrators[module_id] = callback

    def rehydrate_form(self, module_id: str, payload: dict[str, Any]) -> Any:
        callback = self._rehydrators.get(module_id)
        if callback is None:
            return None
        return callback(payload)

    async def activate(
        self,
        path: str | Path,
        starter: Starter,
        *,
        health: Callable[[Any], Any] | None = None,
    ) -> ActiveModule:
        loaded = self.loader.load(path)
        module_id = loaded.manifest.module_id
        if module_id in self._active:
            raise ActivationError(f"module is already active: {module_id}")
        try:
            context = starter(loaded)
            if inspect.isawaitable(context):
                context = await asyncio.wait_for(context, timeout=self.timeout)
            if health is not None:
                result = health(context)
                if inspect.isawaitable(result):
                    with module_scope():
                        result = await asyncio.wait_for(result, timeout=self.timeout)
                if result is False:
                    raise RuntimeError("health check returned false")
        except Exception as exc:
            await self._cleanup(context if "context" in locals() else None)
            raise ActivationError(f"module activation failed: {module_id}") from exc
        active = ActiveModule(loaded, context)
        self._active[module_id] = active
        return active

    async def activate_source(
        self,
        path: str | Path,
        kernel: Any,
        *,
        health: Callable[[ModuleInstance], Any] | None = None,
        sandbox: Any = None,
        trusted: bool = False,
        is_kernel: bool = False,
    ) -> ActiveModule:
        loaded = self.loader.load(path)
        behind_sandbox = not trusted
        if behind_sandbox and sandbox is not None:
            await sandbox.start_module(loaded.manifest.module_id, loaded.source, list(loaded.manifest.commands))
            for name in loaded.manifest.commands:
                if not name.isidentifier():
                    raise ActivationError(f"module command is invalid: {name}")
            commands = self.binder.bind_sandbox(loaded, kernel)
            instance = ModuleInstance(loaded, {"__sandbox__": True}, commands)
            active = ActiveModule(loaded, instance)
            if loaded.manifest.module_id in self._active:
                raise ActivationError(f"module is already active: {loaded.manifest.module_id}")
            self._active[loaded.manifest.module_id] = active
            self._bindings[loaded.manifest.module_id] = (kernel, commands, False)
            
            if self.tasks is not None and kernel.context_factory is not None:
                for task_name, task_def in loaded.manifest.tasks.items():
                    if task_def.get("autostart", False):
                        ctx = kernel.context_factory.create(loaded.manifest.module_id, None)
                        
                        async def sandbox_task_handler(c=ctx, s=sandbox, m=loaded.manifest.module_id, t=task_name):
                            await s.call(m, t, [], {}, target=f"task_{t}")
                            
                        self.tasks.spawn(
                            loaded.manifest.module_id, 
                            self._run_task(task_name, task_def, sandbox_task_handler, ctx),
                            name=f"hotaru:task:sandbox:{loaded.manifest.module_id}:{task_name}"
                        )
                        
            return active
        namespace: dict[str, Any] = {
            "__name__": f"hotaru_module_{loaded.manifest.module_id}",
            "__file__": str(loaded.path),
        }
        try:
            with module_scope():
                exec(compile(loaded.source, str(loaded.path), "exec"), namespace, namespace)
            commands = self.binder.bind(loaded, namespace, kernel, is_kernel=is_kernel)
            instance = ModuleInstance(loaded, namespace, commands)
            if health is not None:
                result = health(instance)
                if inspect.isawaitable(result):
                    with module_scope():
                        result = await asyncio.wait_for(result, timeout=self.timeout)
                if result is False:
                    raise RuntimeError("health check returned false")
            active = ActiveModule(loaded, instance)
            if loaded.manifest.module_id in self._active:
                raise ActivationError(f"module is already active: {loaded.manifest.module_id}")
            self._active[loaded.manifest.module_id] = active
            self._bindings[loaded.manifest.module_id] = (kernel, commands, is_kernel)
            self._register_rehydrator(loaded.manifest.module_id, namespace)
            
            if self.tasks is not None and kernel.context_factory is not None:
                for task_name, task_def in loaded.manifest.tasks.items():
                    if task_def.get("autostart", False):
                        handler = namespace.get(f"task_{task_name}")
                        if handler and callable(handler):
                            ctx = kernel.context_factory.create(loaded.manifest.module_id, None)
                            self.tasks.spawn(
                                loaded.manifest.module_id, 
                                self._run_task(task_name, task_def, handler, ctx),
                                name=f"hotaru:task:{loaded.manifest.module_id}:{task_name}"
                            )
            return active
        except Exception as exc:
            if "commands" in locals():
                self.binder.unbind(loaded, commands, kernel, is_kernel=is_kernel)
            raise ActivationError(f"module activation failed: {loaded.manifest.module_id}") from exc

    async def _run_task(self, name: str, task_def: dict[str, Any], handler: Any, ctx: Any) -> None:
        interval = float(task_def.get("interval", 60.0))
        while True:
            try:
                with module_scope():
                    result = handler(ctx)
                    if inspect.isawaitable(result):
                        await result
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(interval)

    async def _cleanup(self, context: Any) -> None:
        if context is None:
            return
        for name in ("stop", "close"):
            method = getattr(context, name, None)
            if method is None:
                continue
            result = method()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=self.timeout)
            return

    async def deactivate(self, module_id: str, stopper: Callable[[ActiveModule], Any] | None = None) -> bool:
        active = self._active.get(module_id)
        if active is None:
            return False
        if stopper is not None:
            result = stopper(active)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=self.timeout)
        if self.tasks is not None:
            await self.tasks.cancel_module(module_id)
        if hasattr(self, "form_cleanup") and self.form_cleanup is not None:
            result = self.form_cleanup(module_id)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=self.timeout)
        binding = self._bindings.pop(module_id, None)
        if binding is not None:
            kern, commands, ik = binding
            self.binder.unbind(active.loaded, commands, kern, is_kernel=ik)
        del self._active[module_id]
        return True

    def get(self, module_id: str) -> ActiveModule | None:
        return self._active.get(module_id)

    def items(self) -> tuple[ActiveModule, ...]:
        return tuple(self._active.values())
