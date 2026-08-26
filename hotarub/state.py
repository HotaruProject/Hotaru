from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any


class StateError(ValueError):
    pass


class StateNamespace:
    def __init__(self, connection: sqlite3.Connection, module_id: str) -> None:
        self._connection = connection
        self.module_id = module_id

    def get(self, key: str, default: Any = None) -> Any:
        self._validate_key(key)
        row = self._connection.execute(
            "SELECT value FROM module_state WHERE module_id = ? AND key = ?",
            (self.module_id, key),
        ).fetchone()
        return default if row is None else json.loads(row[0])

    def set(self, key: str, value: Any) -> None:
        self._validate_key(key)
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._connection:
            self._connection.execute(
                "INSERT INTO module_state(module_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(module_id, key) DO UPDATE SET value = excluded.value",
                (self.module_id, key, encoded),
            )

    def delete(self, key: str) -> bool:
        self._validate_key(key)
        with self._connection:
            result = self._connection.execute(
                "DELETE FROM module_state WHERE module_id = ? AND key = ?",
                (self.module_id, key),
            )
        return result.rowcount == 1

    def keys(self) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT key FROM module_state WHERE module_id = ? ORDER BY key",
            (self.module_id,),
        ).fetchall()
        return tuple(row[0] for row in rows)

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9:.-]{0,127}", key):
            raise StateError("state key is invalid")


class StateStore:
    def __init__(self, path: str | Path = "sanctuary/state.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS module_state ("
            "module_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
            "PRIMARY KEY(module_id, key))"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS runtime_settings ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS accounts ("
            "user_id INTEGER NOT NULL, account_number INTEGER NOT NULL, vault_name TEXT NOT NULL, "
            "session_dir TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
            "PRIMARY KEY(user_id, account_number), UNIQUE(vault_name))"
        )
        self.connection.commit()
        self.path.chmod(0o600)

    def namespace(self, module_id: str) -> StateNamespace:
        if not isinstance(module_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", module_id):
            raise StateError("module id is invalid")
        return StateNamespace(self.connection, module_id)

    def module_ids(self) -> tuple[str, ...]:
        rows = self.connection.execute("SELECT DISTINCT module_id FROM module_state ORDER BY module_id").fetchall()
        return tuple(row[0] for row in rows)

    def register_account(self, user_id: int, account_number: int, session_dir: str | Path) -> Any:
        from .accounts import AccountProfile

        profile = AccountProfile.create(user_id, account_number, session_dir)
        profile.validate()
        with self.connection:
            self.connection.execute(
                "INSERT INTO accounts(user_id, account_number, vault_name, session_dir, enabled) VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(user_id, account_number) DO UPDATE SET vault_name=excluded.vault_name, session_dir=excluded.session_dir",
                (profile.user_id, profile.account_number, profile.vault_name, str(profile.session_dir)),
            )
        return profile

    def accounts(self) -> tuple[Any, ...]:
        from .accounts import AccountProfile

        rows = self.connection.execute("SELECT user_id, account_number, vault_name, session_dir, enabled FROM accounts ORDER BY user_id, account_number").fetchall()
        return tuple(AccountProfile(row[0], row[1], row[2], Path(row[3]), bool(row[4])) for row in rows)

    def get_setting(self, key: str, default: Any = None) -> Any:
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9:.-]{0,127}", key):
            raise StateError("setting key is invalid")
        row = self.connection.execute("SELECT value FROM runtime_settings WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def set_setting(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9:.-]{0,127}", key):
            raise StateError("setting key is invalid")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self.connection:
            self.connection.execute(
                "INSERT INTO runtime_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, encoded),
            )

    def delete_module(self, module_id: str) -> bool:
        with self.connection:
            result = self.connection.execute("DELETE FROM module_state WHERE module_id = ?", (module_id,))
        return result.rowcount > 0

    def close(self) -> None:
        self.connection.close()
