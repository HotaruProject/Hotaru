from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .state import StateStore
from .accounts import account_db_path


DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "sanctuary/state.sqlite3"


def discover_state(path: str | Path = DEFAULT_STATE_PATH) -> Path:
    bootstrap = Path(path)
    session_dir = Path(".")
    active = None
    if bootstrap.exists() and bootstrap.stat().st_size > 0:
        state = StateStore(bootstrap)
        try:
            raw = state.get_setting("session-dir")
            if raw:
                session_dir = Path(str(raw)).expanduser()
            active = state.get_setting("active-account")
        except Exception:
            pass
        finally:
            state.close()
    root = session_dir.expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    else:
        root = root.resolve()
    if isinstance(active, int) and active > 0:
        candidate = account_db_path(root, active)
        if candidate.is_file():
            return candidate
    found = sorted(root.glob("sanctuary/account-*/hotaru-*.sqlite3")) or sorted(root.glob("sanctuary/account-*/state-*.sqlite3")) or sorted(root.glob("account-*/hotaru-*.sqlite3")) or sorted(root.glob("account-*/state-*.sqlite3"))
    if found:
        return found[0]
    return bootstrap


@dataclass(frozen=True)
class RuntimeConfig:
    api_id: int | None
    api_hash: str | None
    bot_token: str | None
    owner_id: int | None
    prefix: str
    session_name: str
    session_dir: Path
    state_path: Path
    backup_keep: int
    command_timeout: float = 60.0

    @classmethod
    def from_database(cls, path: str | Path = DEFAULT_STATE_PATH) -> "RuntimeConfig":
        path = discover_state(path)
        state = StateStore(path)
        try:
            required = ("api-id", "api-hash", "prefix", "session-name", "session-dir", "backup-keep")
            if any(state.get_setting(key) is None for key in required):
                from .login import collect_settings
                collect_settings(state)
            values = {key: state.get_setting(key) for key in ("api-id", "api-hash", "bot-token", "owner-id", "prefix", "session-name", "session-dir", "backup-keep", "command-timeout", "inline-enabled")}
            return cls(
                api_id=values["api-id"],
                api_hash=values["api-hash"],
                bot_token=values["bot-token"],
                owner_id=values["owner-id"],
                prefix=values["prefix"],
                session_name=values["session-name"],
                session_dir=Path(values["session-dir"]),
                state_path=Path(path),
                backup_keep=values["backup-keep"],
                command_timeout=float(values["command-timeout"] if values["command-timeout"] is not None else 60.0),
            )
        finally:
            state.close()

    def validate(self) -> None:
        if (self.api_id is None) != (self.api_hash is None):
            raise ValueError("API ID and API hash must be configured together")
        if self.api_hash is not None and not self.api_hash.strip():
            raise ValueError("API hash must not be empty")
        if self.bot_token is not None and not self.bot_token.strip():
            raise ValueError("bot token must not be empty")
        if self.api_id is None and self.bot_token is None:
            raise ValueError("configure MTProto credentials or bot token in the database")
        if len(self.prefix) != 1 or self.prefix.isspace():
            raise ValueError("prefix must be one non-whitespace character")
        if self.owner_id is not None and self.owner_id == 0:
            raise ValueError("owner ID must be nonzero")
        if self.backup_keep < 1:
            raise ValueError("backup retention must be positive")
        if not self.session_name or Path(self.session_name).name != self.session_name:
            raise ValueError("session name must be a simple filename")
        if self.api_id is not None and self.api_id <= 0:
            raise ValueError("API ID must be positive")
