from dataclasses import dataclass
from getpass import getpass
from pathlib import Path

from .state import StateStore


DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "sanctuary/state.sqlite3"


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
    backup_keep: int
    command_timeout: float = 60.0

    @classmethod
    def from_database(cls, path: str | Path = DEFAULT_STATE_PATH) -> "RuntimeConfig":
        state = StateStore(path)
        try:
            required = ("api-id", "api-hash", "owner-id", "prefix", "session-name", "session-dir", "backup-keep")
            if any(state.get_setting(key) is None for key in required):
                if not __import__("sys").stdin.isatty():
                    raise ValueError("runtime settings are missing; run from an interactive TTY")
                print("Hotaru first-run database setup")
                api_id = int(input("Telegram API ID: ").strip())
                api_hash = getpass("Telegram API hash: ")
                owner_raw = input("Owner Telegram ID: ").strip()
                owner_id = int(owner_raw) if owner_raw else None
                prefix = input("Command prefix [!]: ").strip() or "!"
                session_name = input("Session name [hotaru]: ").strip() or "hotaru"
                session_dir = input("Session directory [.]: ").strip() or "."
                backup_keep = int(input("Backups to keep [7]: ").strip() or "7")
                values = {
                    "api-id": api_id,
                    "api-hash": api_hash,
                    "bot-token": None,
                    "owner-id": owner_id,
                    "prefix": prefix,
                    "session-name": session_name,
                    "session-dir": session_dir,
                    "backup-keep": backup_keep,
                }
                for key, value in values.items():
                    state.set_setting(key, value)
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
