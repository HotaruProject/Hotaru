from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import StateStore


ACCOUNT_VAULT_RE = re.compile(r"hotaru-[0-9a-f]{64}\.vault")


def vault_name(user_id: int, account_number: int) -> str:
    if user_id <= 0 or account_number <= 0:
        raise ValueError("user_id and account_number must be positive")
    digest = hashlib.sha256(f"{user_id}:{account_number}".encode("ascii")).hexdigest()
    return f"hotaru-{digest}.vault"


@dataclass(frozen=True, slots=True)
class AccountProfile:
    user_id: int
    account_number: int
    vault_name: str
    session_dir: Path
    enabled: bool = True

    @classmethod
    def create(cls, user_id: int, account_number: int, session_dir: str | Path) -> "AccountProfile":
        return cls(user_id, account_number, vault_name(user_id, account_number), Path(session_dir))

    def validate(self) -> None:
        if self.user_id <= 0 or self.account_number <= 0:
            raise ValueError("account identity must be positive")
        if not ACCOUNT_VAULT_RE.fullmatch(self.vault_name):
            raise ValueError("account vault name is invalid")
        if self.session_dir.name in {"", ".", ".."}:
            raise ValueError("account session directory is invalid")


class AccountError(RuntimeError):
    pass


class AccountManager:
    KEY = "accounts:registry"

    def __init__(self, state: StateStore, session_dir: str | Path) -> None:
        self.state = state
        self.session_dir = Path(session_dir)

    def _load(self) -> dict[str, dict[str, Any]]:
        raw = self.state.get_setting(self.KEY, {})
        return raw if isinstance(raw, dict) else {}

    def _save(self, registry: dict[str, dict[str, Any]]) -> None:
        self.state.set_setting(self.KEY, registry)

    def _entry(self, account_number: int) -> dict[str, Any]:
        return self._load().get(str(account_number))

    def register(self, user_id: int, account_number: int) -> AccountProfile:
        profile = AccountProfile.create(user_id, account_number, self.session_dir)
        profile.validate()
        registry = self._load()
        for number, entry in registry.items():
            if entry.get("user_id") == user_id and int(number) != account_number:
                raise AccountError(f"user {user_id} already has account #{number}")
        registry[str(account_number)] = {
            "user_id": user_id,
            "account_number": account_number,
            "vault_name": profile.vault_name,
            "session_dir": str(self.session_dir),
            "enabled": True,
            "pid": None,
        }
        self._save(registry)
        return profile

    def unregister(self, account_number: int) -> bool:
        registry = self._load()
        if str(account_number) not in registry:
            return False
        registry.pop(str(account_number))
        self._save(registry)
        return True

    def set_enabled(self, account_number: int, enabled: bool) -> bool:
        registry = self._load()
        entry = registry.get(str(account_number))
        if entry is None:
            return False
        entry["enabled"] = bool(enabled)
        self._save(registry)
        return True

    def set_pid(self, account_number: int, pid: int | None) -> bool:
        registry = self._load()
        entry = registry.get(str(account_number))
        if entry is None:
            return False
        entry["pid"] = pid
        self._save(registry)
        return True

    def profile(self, account_number: int) -> AccountProfile | None:
        entry = self._entry(account_number)
        if entry is None:
            return None
        return AccountProfile(
            user_id=int(entry.get("user_id", 0)),
            account_number=int(entry.get("account_number", account_number)),
            vault_name=str(entry.get("vault_name", "")),
            session_dir=Path(entry.get("session_dir", str(self.session_dir))),
            enabled=bool(entry.get("enabled", True)),
        )

    def items(self) -> tuple[AccountProfile, ...]:
        result = []
        for number in sorted(self._load(), key=lambda item: int(item)):
            profile = self.profile(int(number))
            if profile is not None:
                result.append(profile)
        return tuple(result)

    def vault_path(self, account_number: int) -> Path | None:
        profile = self.profile(account_number)
        if profile is None:
            return None
        return profile.session_dir / profile.vault_name

    def vault_ready(self, account_number: int) -> bool:
        path = self.vault_path(account_number)
        return path is not None and path.is_file() and path.stat().st_size > 0

    def spawn(self, account_number: int) -> int:
        profile = self.profile(account_number)
        if profile is None:
            raise AccountError(f"account #{account_number} is not registered")
        if not self.vault_ready(account_number):
            raise AccountError(f"account #{account_number} vault is missing; authorize it first")
        if not profile.enabled:
            raise AccountError(f"account #{account_number} is disabled")
        vault = self.vault_path(account_number)
        session_base = str(vault)[: -len(".vault")]
        environment = dict(os.environ)
        environment["HOTARU_ACCOUNT_SESSION"] = session_base
        process = subprocess.Popen(
            [sys.executable, "-m", "hotaru", "--account", str(account_number)],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
        )
        self.set_pid(account_number, process.pid)
        return process.pid

    def stop(self, account_number: int) -> bool:
        registry = self._load()
        entry = registry.get(str(account_number))
        if entry is None:
            return False
        pid = entry.get("pid")
        if not isinstance(pid, int):
            return False
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        self.set_pid(account_number, None)
        return True
