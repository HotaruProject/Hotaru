from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .capabilities import CapabilityBroker
from .backup import BackupService
from .callbacks import CallbackBinding, CallbackDenied, CallbackRouter
from .config import RuntimeConfig
from .commands import CommandParser
from .events import EventRouter
from .kernel import Kernel
from .modules import ModuleStager
from .activation import ModuleManager
from .observatory import Observatory
from .registry import Handler
from .response import ModuleContextFactory, ResponseService
from .state import StateStore
from .tasks import TaskSupervisor


@dataclass(slots=True)
class Runtime:
    config: RuntimeConfig
    app: Any = None
    kernel: Kernel | None = None
    state: StateStore | None = None
    capabilities: CapabilityBroker | None = None
    modules: ModuleManager | None = None
    stager: ModuleStager | None = None
    responses: ResponseService | None = None
    callbacks: CallbackRouter | None = None
    observatory: Observatory | None = None
    backups: BackupService | None = None
    context_factory: ModuleContextFactory | None = None
    tasks: TaskSupervisor | None = None
    event_router: EventRouter | None = None
    closed: bool = False

    @classmethod
    def from_env(cls) -> "Runtime":
        return cls(RuntimeConfig.from_env())

    def build(self) -> Any:
        self.config.validate()
        if self.app is not None:
            return self.app
        from goygram import GoyGram

        self.config.session_dir.mkdir(parents=True, exist_ok=True)
        session_name = str(self.config.session_dir / self.config.session_name)
        self.app = GoyGram(
            bot_token=self.config.bot_token,
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            session_name=session_name,
        )
        self.responses = ResponseService()
        self.kernel = Kernel(
            parser=CommandParser(self.config.prefix),
            owner_id=self.config.owner_id,
            response_service=self.responses,
        )
        self.state = StateStore(self.config.state_path)
        self.capabilities = CapabilityBroker()
        self.context_factory = ModuleContextFactory(self.state, self.responses)
        self.callbacks = CallbackRouter()
        self.observatory = Observatory()
        self.event_router = EventRouter(self._event_error)
        self.backups = BackupService()
        self.tasks = TaskSupervisor()
        self.modules = ModuleManager(tasks=self.tasks)
        self.stager = ModuleStager(self.modules.loader)
        self.kernel.context_factory = self.context_factory
        self.kernel.registry.register("ver", self._command_ver, kernel=True)
        self.kernel.registry.register("ls", self._command_ls, kernel=True)
        self.kernel.registry.register("mi", self._command_mi, kernel=True)
        self.kernel.registry.register("hlp", self._command_hlp, kernel=True)
        self.kernel.registry.register("ld", self._command_ld, kernel=True)
        self.kernel.registry.register("ul", self._command_ul, kernel=True)
        self.kernel.registry.register("rl", self._command_rl, kernel=True)
        self.kernel.registry.register("rm", self._command_rm, kernel=True)
        self.kernel.registry.register("bk", self._command_bk, kernel=True)
        self.kernel.registry.register("on", self._command_on, kernel=True)
        self.kernel.registry.register("off", self._command_off, kernel=True)
        self.callbacks.register("remove_confirm", self._remove_confirm)
        self.callbacks.register("restore_confirm", self._restore_confirm)
        self.callbacks.register("help_page", self._help_page)
        self.callbacks.register("module_detail", self._module_detail)
        self.kernel.attach(self.app)
        self.app.on_cb(self._on_callback)
        self.event_router.attach_aux(self.app)
        return self.app

    async def _on_callback(self, callback: Any) -> object | None:
        if self.callbacks is None:
            return None
        try:
            return await self.callbacks.dispatch(callback)
        except CallbackDenied:
            return None

    def _command_ver(self, invocation: Any) -> str:
        from . import __version__

        return f"HotaruUB {__version__}"

    def _command_ls(self, invocation: Any) -> str:
        if self.modules is None:
            return "modules: unavailable"
        items = self.modules.items()
        if not items:
            return "modules: none"
        return "modules:\n" + "\n".join(sorted(item.loaded.manifest.module_id for item in items))

    def _module_detail_text(self, module_id: str) -> str:
        if self.modules is None:
            return "module unavailable"
        active = self.modules.get(module_id)
        if active is None:
            if self.state is not None and module_id in self.state.module_ids():
                return f"module: {module_id}\nstatus: disabled\nlasterror: {self.state.namespace(module_id).get('lasterror', 'none')}"
            return f"module not active: {module_id}"
        manifest = active.loaded.manifest
        commands = ", ".join(manifest.commands) if manifest.commands else "none"
        capabilities = ", ".join(manifest.capabilities) if manifest.capabilities else "none"
        return f"module: {manifest.module_id}\nversion: {manifest.version}\ncommands: {commands}\ncapabilities: {capabilities}\ndescription: {manifest.description}"

    def _command_mi(self, invocation: Any) -> str:
        if len(invocation.args) != 1:
            return "usage: !mi <module-id>"
        return self._module_detail_text(invocation.args[0].casefold())

    async def _command_hlp(self, invocation: Any) -> tuple[str, list[dict[str, str]]] | str:
        page = 0
        if invocation.args:
            if len(invocation.args) != 1:
                return "usage: !hlp [page] | !hlp <module-id>"
            if not invocation.args[0].isdigit():
                return self._command_mi(invocation)
            page = int(invocation.args[0])
        return self._help_render(page, invocation.chat_id, invocation.message_id)

    def _help_render(self, page: int, chat_id: int | str | None, message_id: int) -> tuple[str, list[dict[str, str]]] | str:
        if self.kernel is None or self.callbacks is None or page < 0:
            return "help unavailable"
        names = sorted(self.kernel.registry.names())
        entries: list[str] = []
        entry_ids: list[str | None] = []
        if self.modules is not None:
            active = {item.loaded.manifest.module_id: item.loaded.manifest.version for item in self.modules.items()}
            known = set(self.state.module_ids()) if self.state is not None else set()
            for module_id in sorted(set(active) | known):
                if module_id in active:
                    entries.append(f"{module_id} [on] v{active[module_id]}")
                else:
                    entries.append(f"{module_id} [off]")
                entry_ids.append(module_id)
        if not entries:
            entries = [f"{self.config.prefix}{name}" for name in names]
            entry_ids = [None] * len(entries)
        page_size = 24
        start = page * page_size
        if start >= len(entries) and entries:
            return "help page unavailable"
        current = entries[start : start + page_size]
        text = "catalog: " + (", ".join(current) or "none")
        if self.kernel.owner_id is None or chat_id is None:
            return text
        buttons: list[dict[str, str]] = []
        for module_id in entry_ids[start : start + page_size]:
            if module_id is None:
                continue
            handle = self.callbacks.store.issue(
                CallbackBinding(self.kernel.owner_id, chat_id, message_id),
                {"action": "module_detail", "payload": module_id},
            )
            buttons.append({"text": module_id, "callback_data": handle})
        if start + page_size < len(entries):
            handle = self.callbacks.store.issue(
                CallbackBinding(self.kernel.owner_id, chat_id, message_id),
                {"action": "help_page", "payload": page + 1},
            )
            buttons.append({"text": "Next", "callback_data": handle})
        return text, buttons

    def stage_module_url(self, source: str, destination: str | Path | None = None) -> Any:
        if self.stager is None:
            raise RuntimeError("build the runtime before staging modules")
        target = Path(destination) if destination is not None else self.config.state_path.parent / "constellations"
        return self.stager.stage_url(source, target)

    async def _command_ld(self, invocation: Any) -> str:
        if len(invocation.args) != 1:
            return "usage: !ld <path-to-hmod>"
        try:
            if invocation.args[0].startswith("https://"):
                loaded = self.stage_module_url(invocation.args[0])
            else:
                loaded = self.stage_module(invocation.args[0])
        except Exception as exc:
            return f"load failed: {type(exc).__name__}"
        return f"staged: {loaded.manifest.module_id} {loaded.manifest.version}"

    async def _command_ul(self, invocation: Any) -> str:
        if len(invocation.args) != 1 or self.modules is None:
            return "usage: !ul <module-id>"
        module_id = invocation.args[0].casefold()
        if await self.deactivate_module(module_id):
            return f"unloaded: {module_id}"
        return f"module not active: {module_id}"

    async def _command_rl(self, invocation: Any) -> str:
        if len(invocation.args) != 1 or self.modules is None:
            return "usage: !rl <module-id>"
        module_id = invocation.args[0].casefold()
        active = self.modules.get(module_id)
        if active is None:
            return f"module not active: {module_id}"
        old_path = active.loaded.path
        old_source = active.loaded.source
        await self.deactivate_module(module_id)
        try:
            await self.activate_module(str(old_path))
        except Exception as exc:
            try:
                if self.stager is None:
                    raise RuntimeError("module stager is unavailable")
                self.stager.stage_text(old_source, old_path.parent)
                await self.modules.activate_source(old_path, self.kernel)
            except Exception:
                return f"reload failed: {type(exc).__name__}; rollback failed"
            return f"reload failed: {type(exc).__name__}; previous version restored"
        return f"reloaded: {module_id}"

    async def _command_off(self, invocation: Any) -> str:
        if len(invocation.args) != 1 or self.modules is None or self.state is None:
            return "usage: !off <module-id>"
        module_id = invocation.args[0].casefold()
        if self.modules.get(module_id) is None:
            return f"module not active: {module_id}"
        if await self.deactivate_module(module_id):
            self.state.namespace(module_id).set("enabled", False)
            return f"disabled: {module_id}"
        return f"module not active: {module_id}"

    async def _command_on(self, invocation: Any) -> str:
        if len(invocation.args) != 1 or self.modules is None or self.state is None:
            return "usage: !on <module-id>"
        module_id = invocation.args[0].casefold()
        if self.modules.get(module_id) is not None:
            return f"module already active: {module_id}"
        namespace = self.state.namespace(module_id)
        source_path = namespace.get("sourcepath")
        if not isinstance(source_path, str):
            candidate = self.config.state_path.parent / "constellations" / f"{module_id}.hmod"
            if not candidate.is_file():
                return f"module source unavailable: {module_id}"
            source_path = str(candidate)
        try:
            await self.activate_module(source_path)
        except Exception as exc:
            return f"enable failed: {type(exc).__name__}"
        return f"enabled: {module_id}"

    def create_backup(self) -> Path:
        if self.backups is None or self.state is None or self.modules is None:
            raise RuntimeError("backup services are not ready")
        module_paths = [active.loaded.path for active in self.modules.items()]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = self.config.state_path.parent / "backups" / f"{stamp}.hbk"
        result = self.backups.create(destination, state_path=self.config.state_path, module_paths=module_paths, metadata={"reason": "operator"})
        removed = self.backups.prune(destination.parent, keep=self.config.backup_keep)
        if self.observatory is not None:
            self.observatory.emit("backup", "created", files=len(module_paths) + 1, removed=len(removed))
        return result

    async def _command_bk(self, invocation: Any) -> tuple[str, list[dict[str, str]]] | str:
        if not invocation.args:
            try:
                archive = self.create_backup()
            except Exception as exc:
                return f"backup failed: {type(exc).__name__}"
            return f"backup created: {archive.name}"
        action = invocation.args[0].casefold()
        if action == "list" and len(invocation.args) == 1:
            directory = self.config.state_path.parent / "backups"
            names = sorted(path.name for path in directory.glob("*.hbk") if path.is_file())
            return "backups: none" if not names else "backups:\n" + "\n".join(names)
        if action == "test" and len(invocation.args) == 2:
            try:
                plan = self.backups.plan(invocation.args[1])
            except Exception as exc:
                return f"backup invalid: {type(exc).__name__}"
            return f"backup valid: {len(plan.files)} files"
        if action == "restore" and len(invocation.args) == 2 and self.callbacks is not None and self.kernel is not None and self.kernel.owner_id is not None:
            try:
                self.backups.plan(invocation.args[1])
            except Exception as exc:
                return f"backup invalid: {type(exc).__name__}"
            handle = self.callbacks.store.issue(
                CallbackBinding(self.kernel.owner_id, invocation.chat_id, invocation.message_id),
                {"action": "restore_confirm", "payload": invocation.args[1]},
            )
            return ("confirm restore", [{"text": "Confirm", "callback_data": handle}])
        return "usage: !bk | !bk list | !bk test <archive> | !bk restore <archive>"

    async def _command_rm(self, invocation: Any) -> tuple[str, list[dict[str, str]]] | str:
        if len(invocation.args) != 1 or self.modules is None or self.callbacks is None:
            return "usage: !rm <module-id>"
        if self.kernel is None or self.kernel.owner_id is None:
            return "rm unavailable: explicit owner id required"
        module_id = invocation.args[0].casefold()
        if self.modules.get(module_id) is None:
            return f"module not active: {module_id}"
        handle = self.callbacks.store.issue(
            CallbackBinding(self.kernel.owner_id, invocation.chat_id, invocation.message_id),
            {"action": "remove_confirm", "payload": module_id},
        )
        return (f"confirm removal: {module_id}", [{"text": "Confirm", "callback_data": handle}])

    async def _remove_confirm(self, callback: Any, payload: Any) -> object:
        if not isinstance(payload, str) or self.modules is None:
            await callback.answer("Invalid removal request", alert=True)
            return None
        active = self.modules.get(payload)
        if active is None:
            await callback.answer("Module is already unloaded", alert=True)
            return await callback.edit(f"module not active: {payload}")
        path = active.loaded.path
        self._backup_before_activation(path)
        await self.deactivate_module(payload)
        try:
            path.unlink()
        except OSError:
            await self.modules.activate_source(path, self.kernel)
            await callback.answer("Removal failed", alert=True)
            return await callback.edit(f"removal failed: {payload}")
        await callback.answer("Module removed", alert=True)
        return await callback.edit(f"removed: {payload}")

    async def _module_detail(self, callback: Any, payload: Any) -> object:
        if not isinstance(payload, str):
            await callback.answer("Invalid module", alert=True)
            return None
        return await callback.edit(self._module_detail_text(payload))

    async def _help_page(self, callback: Any, payload: Any) -> object:
        if not isinstance(payload, int) or payload < 0:
            await callback.answer("Invalid help page", alert=True)
            return None
        result = self._help_render(payload, callback.chat_id, callback.msg_id)
        if isinstance(result, tuple):
            text, buttons = result
            return await callback.edit(text, kbd=buttons)
        return await callback.edit(result)

    async def _restore_confirm(self, callback: Any, payload: Any) -> object:
        if not isinstance(payload, str) or self.backups is None:
            await callback.answer("Invalid restore request", alert=True)
            return None
        try:
            plan = self.backups.plan(payload)
            await self.restore_filesystem(plan, self.config.state_path.parent / "constellations")
        except Exception as exc:
            await callback.answer("Restore failed", alert=True)
            return await callback.edit(f"restore failed: {type(exc).__name__}")
        await callback.answer("Restore completed", alert=True)
        return await callback.edit("restore completed")

    def _event_error(self, error: Exception) -> None:
        if self.observatory is not None:
            self.observatory.emit("events", "handler_error", error=type(error).__name__)

    def register_callback(self, action: str, handler: Any) -> None:
        if self.callbacks is None:
            raise RuntimeError("build the runtime before registering callbacks")
        self.callbacks.register(action, handler)

    def register_command(self, name: str, handler: Handler, *, kernel: bool = False) -> None:
        if self.kernel is None:
            raise RuntimeError("build the runtime before registering commands")
        self.kernel.registry.register(name, handler, kernel=kernel)

    def register_module_command(self, module_id: str, name: str, handler: Handler) -> None:
        if self.kernel is None:
            raise RuntimeError("build the runtime before registering commands")
        self.kernel.register_module_command(module_id, name, handler)

    def _backup_before_activation(self, path: str | Path) -> Path:
        if self.backups is None or self.state is None or self.modules is None:
            raise RuntimeError("backup services are not ready")
        candidate = Path(path)
        self.modules.loader.load(candidate)
        module_paths = [active.loaded.path for active in self.modules.items()]
        if candidate not in module_paths:
            module_paths.append(candidate)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = self.config.state_path.parent / "backups" / f"{stamp}.hbk"
        result = self.backups.create(
            destination,
            state_path=self.config.state_path,
            module_paths=module_paths,
            metadata={"reason": "module_activation", "module": str(candidate.name)},
        )
        removed = self.backups.prune(destination.parent, keep=self.config.backup_keep)
        if self.observatory is not None:
            self.observatory.emit("backup", "created", files=len(module_paths) + 1, removed=len(removed))
        return result

    def stage_module(self, source: str | Path, destination: str | Path | None = None) -> Any:
        if self.stager is None:
            raise RuntimeError("build the runtime before staging modules")
        target = Path(destination) if destination is not None else self.config.state_path.parent / "constellations"
        return self.stager.stage(source, target)

    async def activate_module(self, path: str, *, health: Any = None) -> Any:
        if self.modules is None or self.kernel is None:
            raise RuntimeError("build the runtime before activating modules")
        self._backup_before_activation(path)
        result = await self.modules.activate_source(path, self.kernel, health=health)
        module_id = result.loaded.manifest.module_id
        namespace = self.state.namespace(module_id) if self.state is not None else None
        if namespace is not None:
            namespace.set("enabled", True)
            namespace.set("sourcepath", str(result.loaded.path))
        return result

    async def deactivate_module(self, module_id: str) -> bool:
        if self.modules is None:
            raise RuntimeError("build the runtime before deactivating modules")
        return await self.modules.deactivate(module_id)

    async def restore_backup(self, plan: Any, activate: Any, *, rollback: Any | None = None, timeout: float = 10.0) -> Any:
        if self.backups is None:
            raise RuntimeError("backup service is not ready")
        return await self.backups.restore(plan, activate, rollback=rollback, timeout=timeout)

    async def restore_filesystem(self, plan: Any, modules_path: str | Path) -> None:
        if self.backups is None or self.state is None or self.modules is None:
            raise RuntimeError("runtime services are not ready")
        if self.modules.items():
            raise RuntimeError("unload modules before filesystem restore")
        state = self.state
        self.state = None
        state.close()
        try:
            await self.backups.restore(
                plan,
                lambda staged: self.backups.activate_staged(
                    staged,
                    state_path=self.config.state_path,
                    modules_path=modules_path,
                ),
            )
        finally:
            self.state = StateStore(self.config.state_path)
            self.context_factory = ModuleContextFactory(self.state, self.responses)
            if self.kernel is not None:
                self.kernel.context_factory = self.context_factory
            self.modules = ModuleManager(tasks=self.tasks)

    def status(self) -> dict[str, Any]:
        return {
            "runtime": "closed" if self.closed else ("ready" if self.app is not None else "new"),
            "kernel": self.kernel is not None,
            "state": self.state is not None,
            "capabilities": self.capabilities is not None,
            "modules": self.modules is not None,
            "responses": self.responses is not None,
            "callbacks": self.callbacks is not None,
            "observatory": self.observatory is not None,
            "backups": self.backups is not None,
            "tasks": self.tasks is not None,
            "event_router": self.event_router is not None,
        }

    def health(self) -> bool:
        return all(self.status().values())

    async def restore_enabled_modules(self) -> tuple[str, ...]:
        if self.state is None or self.modules is None or self.kernel is None:
            raise RuntimeError("runtime services are not ready")
        restored: list[str] = []
        for module_id in self.state.module_ids()[:256]:
            namespace = self.state.namespace(module_id)
            if namespace.get("enabled") is not True:
                continue
            source_path = namespace.get("sourcepath")
            if not isinstance(source_path, str):
                continue
            try:
                await self.activate_module(source_path)
            except Exception as exc:
                namespace.set("enabled", False)
                namespace.set("lasterror", type(exc).__name__)
                if self.observatory is not None:
                    self.observatory.emit("modules", "restore_error", module=module_id, error=type(exc).__name__)
                continue
            restored.append(module_id)
        return tuple(restored)

    async def run(self) -> None:
        if self.app is None:
            self.build()
        await self.restore_enabled_modules()
        await self.app.run()

    def stop(self) -> None:
        if self.app is not None:
            self.app.stop()

    async def close(self) -> None:
        if self.closed:
            return
        if self.modules is not None:
            for active in tuple(self.modules.items()):
                await self.modules.deactivate(active.loaded.manifest.module_id)
        if self.tasks is not None:
            await self.tasks.close()
        if self.app is not None:
            await self.app.close()
        if self.state is not None:
            self.state.close()
        self.closed = True
