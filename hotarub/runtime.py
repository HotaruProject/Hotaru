from dataclasses import dataclass
from typing import Any

from .capabilities import CapabilityBroker
from .backup import BackupService
from .callbacks import CallbackDenied, CallbackRouter
from .config import RuntimeConfig
from .commands import CommandParser
from .events import EventRouter
from .kernel import Kernel
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
        self.kernel = Kernel(
            parser=CommandParser(self.config.prefix),
            owner_id=self.config.owner_id,
        )
        self.state = StateStore(self.config.state_path)
        self.capabilities = CapabilityBroker()
        self.responses = ResponseService()
        self.context_factory = ModuleContextFactory(self.state, self.responses)
        self.callbacks = CallbackRouter()
        self.observatory = Observatory()
        self.event_router = EventRouter(self._event_error)
        self.backups = BackupService()
        self.tasks = TaskSupervisor()
        self.modules = ModuleManager(tasks=self.tasks)
        self.kernel.context_factory = self.context_factory
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

    async def activate_module(self, path: str, *, health: Any = None) -> Any:
        if self.modules is None or self.kernel is None:
            raise RuntimeError("build the runtime before activating modules")
        return await self.modules.activate_source(path, self.kernel, health=health)

    async def deactivate_module(self, module_id: str) -> bool:
        if self.modules is None:
            raise RuntimeError("build the runtime before deactivating modules")
        return await self.modules.deactivate(module_id)

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

    async def run(self) -> None:
        if self.app is None:
            self.build()
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
