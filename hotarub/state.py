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
        self.connection.commit()
        self.path.chmod(0o600)

    def namespace(self, module_id: str) -> StateNamespace:
        if not isinstance(module_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", module_id):
            raise StateError("module id is invalid")
        return StateNamespace(self.connection, module_id)

    def close(self) -> None:
        self.connection.close()
