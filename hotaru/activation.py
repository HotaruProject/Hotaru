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
    def bind(self, loaded: LoadedModule, namespace: dict[str, Any], kernel: Any) -> tuple[str, ...]:
        handlers: list[tuple[str, Any]] = []
        for name in loaded.manifest.commands:
            if not name.isidentifier():
                raise ActivationError(f"module command is invalid: {name}")
            handler = namespace.get(f"command_{name}")
            if not callable(handler):
                raise ActivationError(f"module handler is missing: {name}")
            handlers.append((name, handler))
        bound: list[str] = []
        try:
            for name, handler in handlers:
                kernel.registry.register(
                    name,
                    handler,
                    module_id=loaded.manifest.module_id,
                )
                bound.append(name)
        except Exception as exc:
            for name in bound:
                kernel.unregister_module_command(loaded.manifest.module_id, name)
            raise ActivationError(f"module command binding failed: {loaded.manifest.module_id}") from exc
        return tuple(bound)

    def unbind(self, loaded: LoadedModule, commands: tuple[str, ...], kernel: Any) -> None:
        for name in commands:
            kernel.unregister_module_command(loaded.manifest.module_id, name)

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
        self._bindings: dict[str, tuple[Any, tuple[str, ...]]] = {}
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
            self._bindings[loaded.manifest.module_id] = (kernel, commands)
            return active
        namespace: dict[str, Any] = {
            "__name__": f"hotaru_module_{loaded.manifest.module_id}",
            "__file__": str(loaded.path),
        }
        try:
            with module_scope():
                exec(compile(loaded.source, str(loaded.path), "exec"), namespace, namespace)
            commands = self.binder.bind(loaded, namespace, kernel)
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
            self._bindings[loaded.manifest.module_id] = (kernel, commands)
            self._register_rehydrator(loaded.manifest.module_id, namespace)
            return active
        except Exception as exc:
            if "commands" in locals():
                self.binder.unbind(loaded, commands, kernel)
            raise ActivationError(f"module activation failed: {loaded.manifest.module_id}") from exc

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
            self.binder.unbind(active.loaded, binding[1], binding[0])
        del self._active[module_id]
        return True

    def get(self, module_id: str) -> ActiveModule | None:
        return self._active.get(module_id)

    def items(self) -> tuple[ActiveModule, ...]:
        return tuple(self._active.values())
