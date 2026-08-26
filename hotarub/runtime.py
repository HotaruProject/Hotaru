from dataclasses import dataclass
from typing import Any

from .config import RuntimeConfig
from .commands import CommandParser
from .kernel import Kernel
from .registry import Handler


@dataclass(slots=True)
class Runtime:
    config: RuntimeConfig
    app: Any = None
    kernel: Kernel | None = None

    @classmethod
    def from_env(cls) -> "Runtime":
        return cls(RuntimeConfig.from_env())

    def build(self) -> Any:
        self.config.validate()
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
        return self.app

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
