from dataclasses import dataclass
from typing import Any

from .capabilities import CapabilityBroker
from .backup import BackupService
from .callbacks import CallbackDenied, CallbackRouter
from .config import RuntimeConfig
from .commands import CommandParser
from .kernel import Kernel
from .modules import HmodLoader
from .observatory import Observatory
from .registry import Handler
from .response import ResponseService
from .state import StateStore


@dataclass(slots=True)
class Runtime:
    config: RuntimeConfig
    app: Any = None
    kernel: Kernel | None = None
    state: StateStore | None = None
    capabilities: CapabilityBroker | None = None
    modules: HmodLoader | None = None
    responses: ResponseService | None = None
    callbacks: CallbackRouter | None = None
    observatory: Observatory | None = None
    backups: BackupService | None = None

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
        self.kernel.attach(self.app)
        self.state = StateStore(self.config.state_path)
        self.capabilities = CapabilityBroker()
        self.modules = HmodLoader()
        self.responses = ResponseService()
        self.callbacks = CallbackRouter()
        self.observatory = Observatory()
        self.backups = BackupService()
        self.app.on_cb(self._on_callback)
        return self.app

    async def _on_callback(self, callback: Any) -> object | None:
        if self.callbacks is None:
            return None
        try:
            return await self.callbacks.dispatch(callback)
        except CallbackDenied:
            return None

    def register_callback(self, action: str, handler: Any) -> None:
        if self.callbacks is None:
            raise RuntimeError("build the runtime before registering callbacks")
        self.callbacks.register(action, handler)

    def register_command(self, name: str, handler: Handler, *, kernel: bool = False) -> None:
        if self.kernel is None:
            raise RuntimeError("build the runtime before registering commands")
        self.kernel.registry.register(name, handler, kernel=kernel)

    async def run(self) -> None:
        if self.app is None:
            self.build()
        await self.app.run()

    def stop(self) -> None:
        if self.app is not None:
            self.app.stop()

    async def close(self) -> None:
        if self.app is not None:
            await self.app.close()
        if self.state is not None:
            self.state.close()
