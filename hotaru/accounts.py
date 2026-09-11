from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import StateStore


VAULT_RE = re.compile(r"hotaru-(\d+)\.vault")


def vault_name(session_name: str) -> str:
    return f"{session_name}.vault"


@dataclass(frozen=True, slots=True)
class AccountProfile:
    user_id: int
    account_number: int
    session_name: str
    session_dir: Path
    enabled: bool = True

    @property
    def vault_name(self) -> str:
        return vault_name(self.session_name)

    @property
    def session_base(self) -> str:
        return str(self.session_dir / self.session_name)

    def validate(self) -> None:
        if self.user_id <= 0 or self.account_number <= 0:
            raise ValueError("account identity must be positive")
        if not self.session_name or Path(self.session_name).name != self.session_name:
            raise ValueError("account session name is invalid")
        if self.session_dir.name in {"", ".", ".."}:
            raise ValueError("account session directory is invalid")


class AccountError(RuntimeError):
    pass


class AccountManager:
    def __init__(self, state: StateStore, session_dir: str | Path) -> None:
        self.state = state
        self.session_dir = Path(session_dir)

    def _rows(self) -> list[tuple]:
        return self.state.connection.execute(
            "SELECT user_id, account_number, vault_name, session_dir, enabled, pid FROM accounts ORDER BY account_number"
        ).fetchall()

    @staticmethod
    def _profile(row: tuple) -> AccountProfile:
        vault = str(row[2])
        session_name = vault[: -len(".vault")] if vault.endswith(".vault") else vault
        return AccountProfile(
            user_id=int(row[0]),
            account_number=int(row[1]),
            session_name=session_name,
            session_dir=Path(str(row[3])),
            enabled=bool(row[4]),
        )

    def items(self) -> tuple[AccountProfile, ...]:
        return tuple(self._profile(row) for row in self._rows())

    def profile(self, account_number: int) -> AccountProfile | None:
        for row in self._rows():
            if int(row[1]) == account_number:
                return self._profile(row)
        return None

    def find_by_session(self, session_name: str) -> AccountProfile | None:
        for profile in self.items():
            if profile.session_name == session_name:
                return profile
        return None

    def next_free_number(self) -> int:
        taken = {int(row[1]) for row in self._rows()}
        number = 1
        while number in taken:
            number += 1
        return number

    def register(self, user_id: int, account_number: int, session_name: str | None = None) -> AccountProfile:
        if account_number <= 0:
            raise AccountError("account number must be positive")
        if session_name is None:
            session_name = f"hotaru-{user_id}"
        profile = AccountProfile(
            user_id=user_id,
            account_number=account_number,
            session_name=session_name,
            session_dir=self.session_dir,
        )
        profile.validate()
        existing = self.find_by_session(session_name)
        if existing is not None and existing.account_number != account_number:
            raise AccountError(f"session {session_name} is already registered as account #{existing.account_number}")
        for row_profile in self.items():
            if row_profile.user_id == user_id and row_profile.account_number != account_number:
                raise AccountError(f"user {user_id} already has account #{row_profile.account_number}")
        with self.state.connection:
            self.state.connection.execute(
                "INSERT INTO accounts(user_id, account_number, vault_name, session_dir, enabled, pid) "
                "VALUES (?, ?, ?, ?, 1, NULL) "
                "ON CONFLICT(user_id, account_number) DO UPDATE SET vault_name=excluded.vault_name, session_dir=excluded.session_dir",
                (profile.user_id, profile.account_number, profile.vault_name, str(profile.session_dir)),
            )
        result = self.profile(account_number)
        assert result is not None
        return result

    def ensure_primary(self, user_id: int, session_name: str) -> AccountProfile:
        existing = self.find_by_session(session_name)
        if existing is not None:
            if existing.user_id != user_id:
                with self.state.connection:
                    self.state.connection.execute(
                        "UPDATE accounts SET user_id = ? WHERE account_number = ?",
                        (user_id, existing.account_number),
                    )
                existing = self.profile(existing.account_number)
                assert existing is not None
            return existing
        number = 1 if self.profile(1) is None else self.next_free_number()
        return self.register(user_id, number, session_name)

    def sync_vaults(self) -> list[AccountProfile]:
        discovered: list[AccountProfile] = []
        if not self.session_dir.is_dir():
            return discovered
        known = {profile.session_name for profile in self.items()}
        for path in sorted(self.session_dir.glob("hotaru-*.vault")):
            match = VAULT_RE.fullmatch(path.name)
            if match is None:
                continue
            session_name = path.name[: -len(".vault")]
            if session_name in known or session_name == "hotaru-inline":
                continue
            user_id = int(match.group(1))
            profile = self.register(user_id, self.next_free_number(), session_name)
            discovered.append(profile)
        return discovered

    def unregister(self, account_number: int) -> bool:
        with self.state.connection:
            result = self.state.connection.execute(
                "DELETE FROM accounts WHERE account_number = ?", (account_number,)
            )
        return result.rowcount > 0

    def set_enabled(self, account_number: int, enabled: bool) -> bool:
        with self.state.connection:
            result = self.state.connection.execute(
                "UPDATE accounts SET enabled = ? WHERE account_number = ?",
                (1 if enabled else 0, account_number),
            )
        return result.rowcount > 0

    def set_pid(self, account_number: int, pid: int | None) -> bool:
        with self.state.connection:
            result = self.state.connection.execute(
                "UPDATE accounts SET pid = ? WHERE account_number = ?",
                (pid, account_number),
            )
        return result.rowcount > 0

    def running_pid(self, account_number: int) -> int | None:
        row = self.state.connection.execute(
            "SELECT pid FROM accounts WHERE account_number = ?", (account_number,)
        ).fetchone()
        if row is None or not isinstance(row[0], int):
            return None
        try:
            os.kill(row[0], 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            return row[0]
        return row[0]

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
        running = self.running_pid(account_number)
        if running is not None:
            raise AccountError(f"account #{account_number} is already running (pid {running})")
        environment = dict(os.environ)
        environment["HOTARU_ACCOUNT_SESSION"] = profile.session_base
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
        pid = self.running_pid(account_number)
        if pid is None:
            return False
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        self.set_pid(account_number, None)
        return True
