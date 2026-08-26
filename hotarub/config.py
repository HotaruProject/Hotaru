from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    api_id: int | None
    api_hash: str | None
    bot_token: str | None
    owner_id: int | None
    prefix: str
    session_name: str
    session_dir: Path
    state_path: Path

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        api_id_value = os.environ.get("HOTARU_API_ID")
        api_hash = os.environ.get("HOTARU_API_HASH")
        bot_token = os.environ.get("HOTARU_BOT_TOKEN")
        owner_id_value = os.environ.get("HOTARU_OWNER_ID")
        if bool(api_id_value) != bool(api_hash):
            raise ValueError("HOTARU_API_ID and HOTARU_API_HASH must be provided together")
        api_id = int(api_id_value) if api_id_value else None
        return cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            owner_id=int(owner_id_value) if owner_id_value else None,
            prefix=os.environ.get("HOTARU_PREFIX", "!"),
            session_name=os.environ.get("HOTARU_SESSION_NAME", "hotarub"),
            session_dir=Path(os.environ.get("HOTARU_SESSION_DIR", "sanctuary/sessions")),
            state_path=Path(os.environ.get("HOTARU_STATE_PATH", "sanctuary/state.sqlite3")),
        )

    def validate(self) -> None:
        if self.api_id is None and self.bot_token is None:
            raise ValueError("configure MTProto credentials or HOTARU_BOT_TOKEN")
        if len(self.prefix) != 1 or self.prefix.isspace():
            raise ValueError("HOTARU_PREFIX must be one non-whitespace character")
        if self.owner_id is not None and self.owner_id == 0:
            raise ValueError("HOTARU_OWNER_ID must be nonzero")
        if not self.session_name or Path(self.session_name).name != self.session_name:
            raise ValueError("HOTARU_SESSION_NAME must be a simple filename")
        if self.api_id is not None and self.api_id <= 0:
            raise ValueError("HOTARU_API_ID must be positive")
