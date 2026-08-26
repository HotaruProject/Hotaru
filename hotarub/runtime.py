from dataclasses import dataclass
from typing import Any

from .config import RuntimeConfig


@dataclass(slots=True)
class Runtime:
    config: RuntimeConfig
    app: Any = None

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
        return self.app

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
