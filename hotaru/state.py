from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)

SECRET_PREFIX = "enc:v1:"


def apply_vault_key_env() -> None:
    if os.environ.get("GOYGRAM_VAULT_KEY", "").strip():
        return
    raw = os.environ.get("HOTARU_VAULT_KEY", "").strip()
    if not raw:
        key_file = os.environ.get("HOTARU_VAULT_KEY_FILE", "").strip()
        if key_file:
            try:
                raw = Path(key_file).read_text().strip()
            except OSError as exc:
                raise SystemExit(f"HOTARU_VAULT_KEY_FILE is unreadable: {exc}") from exc
    if not raw:
        return
    key = normalize_vault_key(raw)
    if key is None:
        raise SystemExit("HOTARU_VAULT_KEY must be 32 bytes as base64 or hex (generate: openssl rand -base64 32)")
    os.environ["GOYGRAM_VAULT_KEY"] = key


def normalize_vault_key(raw: str) -> str | None:
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        return base64.b64encode(bytes.fromhex(raw)).decode()
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:
        return None
    return base64.b64encode(decoded).decode() if len(decoded) == 32 else None


@lru_cache(maxsize=1)
def _master() -> bytes:
    apply_vault_key_env()
    from goygram.security import vault_master

    return vault_master()


@lru_cache(maxsize=8)
def _derive_state_key(salt_hex: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", _master(), bytes.fromhex(salt_hex), 600000, dklen=32)


class StateError(ValueError):
    pass


class StatePointer:
    def __init__(self, namespace: "StateNamespace", key: str, default: Any = None) -> None:
        self.namespace = namespace
        self.key = key
        self.default = default

    def get(self) -> Any:
        return self.namespace.get(self.key, self.default)

    def set(self, value: Any) -> None:
        self.namespace.set(self.key, value)

    @property
    def value(self) -> Any:
        return self.get()

    @value.setter
    def value(self, val: Any) -> None:
        self.set(val)


class StateNamespace:
    def __init__(self, connection: sqlite3.Connection, module_id: str) -> None:
        self._connection = connection
        self.module_id = module_id

    def pointer(self, key: str, default: Any = None) -> StatePointer:
        return StatePointer(self, key, default)

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

    def all(self) -> dict[str, Any]:
        rows = self._connection.execute(
            "SELECT key, value FROM module_state WHERE module_id = ? ORDER BY key",
            (self.module_id,),
        ).fetchall()
        return {row[0]: json.loads(row[1]) for row in rows}

    @staticmethod
    def _validate_key(key: str) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_:.-]{0,127}", key):
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
            "CREATE TABLE IF NOT EXISTS form_state ("
            "form_id TEXT PRIMARY KEY, module_id TEXT NOT NULL, source TEXT NOT NULL, "
            "chat_id TEXT, message_id INTEGER, inline_message_id TEXT, text TEXT NOT NULL, "
            "buttons TEXT NOT NULL, options TEXT NOT NULL, expires REAL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS state_meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS accounts ("
            "user_id INTEGER NOT NULL, account_number INTEGER NOT NULL, vault_name TEXT NOT NULL, "
            "session_dir TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, pid INTEGER, "
            "PRIMARY KEY(user_id, account_number), UNIQUE(vault_name))"
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(accounts)").fetchall()}
        if "pid" not in columns:
            self.connection.execute("ALTER TABLE accounts ADD COLUMN pid INTEGER")
        self.connection.commit()
        self.path.chmod(0o600)
        self._key = _derive_state_key(self._load_salt())
        self._foreign = not self._claim_master()
        if self._foreign:
            log.error(
                "state %s was sealed with a different vault key; reads return defaults and writes are refused. "
                "Restore the previous HOTARU_VAULT_KEY or re-enter the credentials.",
                self.path.name,
            )
        else:
            self._migrate_settings()

    def _claim_master(self) -> bool:
        current = hashlib.sha256(b"hotaru-state-master:" + _master()).hexdigest()[:16]
        row = self.connection.execute("SELECT value FROM state_meta WHERE key = 'master'").fetchone()
        if row is not None:
            return str(row[0]) == current
        with self.connection:
            self.connection.execute("INSERT INTO state_meta(key, value) VALUES ('master', ?)", (current,))
        return True

    def _load_salt(self) -> str:
        row = self.connection.execute("SELECT value FROM state_meta WHERE key = 'salt'").fetchone()
        if row is not None:
            return str(row[0])
        salt = secrets.token_bytes(16)
        with self.connection:
            self.connection.execute("INSERT INTO state_meta(key, value) VALUES ('salt', ?)", (salt.hex(),))
        return salt.hex()

    def _seal(self, plaintext: str) -> str:
        from goygram import ext

        nonce = secrets.token_bytes(12)
        ciphertext = ext.aes_gcm_encrypt(self._key, nonce, plaintext.encode(), b"")
        return SECRET_PREFIX + base64.b64encode(nonce + bytes(ciphertext)).decode()

    def _unseal(self, stored: str) -> str | None:
        if not stored.startswith(SECRET_PREFIX):
            return stored
        from goygram import ext

        try:
            blob = base64.b64decode(stored[len(SECRET_PREFIX):])
            return ext.aes_gcm_decrypt(self._key, blob[:12], blob[12:], b"").decode()
        except Exception:
            return None

    def _migrate_settings(self) -> int:
        rows = self.connection.execute("SELECT key, value FROM runtime_settings").fetchall()
        pending = [(self._seal(str(value)), str(key)) for key, value in rows if not str(value).startswith(SECRET_PREFIX)]
        if pending:
            with self.connection:
                self.connection.executemany("UPDATE runtime_settings SET value = ? WHERE key = ?", pending)
        return len(pending)

    def namespace(self, module_id: str) -> StateNamespace:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", module_id):
            raise StateError("module id is invalid")
        return StateNamespace(self.connection, module_id)

    def module_ids(self) -> tuple[str, ...]:
        rows = self.connection.execute("SELECT DISTINCT module_id FROM module_state ORDER BY module_id").fetchall()
        return tuple(row[0] for row in rows)

    def register_account(self, user_id: int, account_number: int, session_dir: str | Path, session_name: str | None = None) -> Any:
        from .accounts import AccountProfile

        session_name = session_name or f"user-{user_id}"
        profile = AccountProfile(user_id, account_number, session_name, Path(session_dir))
        profile.validate()
        with self.connection:
            self.connection.execute(
                "INSERT INTO accounts(user_id, account_number, vault_name, session_dir, enabled) VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(user_id, account_number) DO UPDATE SET vault_name=excluded.vault_name, session_dir=excluded.session_dir",
                (profile.user_id, profile.account_number, profile.vault_name, str(profile.session_dir)),
            )
        return profile

    def accounts(self) -> tuple[Any, ...]:
        from .accounts import AccountManager

        manager = AccountManager(self, ".")
        return manager.items()

    def get_setting(self, key: str, default: Any = None) -> Any:
        if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_:.-]{0,127}", key):
            raise StateError("setting key is invalid")
        row = self.connection.execute("SELECT value FROM runtime_settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        plaintext = self._unseal(str(row[0]))
        if plaintext is None:
            raise StateError(f"setting {key!r} is sealed with a different vault key; restore the key or re-enter the value")
        return json.loads(plaintext)

    def set_setting(self, key: str, value: Any) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_:.-]{0,127}", key):
            raise StateError("setting key is invalid")
        if self._foreign:
            raise StateError("state was sealed with a different vault key; refusing to overwrite it")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self.connection:
            self.connection.execute(
                "INSERT INTO runtime_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, self._seal(encoded)),
            )

    def all_settings(self) -> dict[str, Any]:
        rows = self.connection.execute("SELECT key, value FROM runtime_settings ORDER BY key").fetchall()
        settings: dict[str, Any] = {}
        for key, value in rows:
            plaintext = self._unseal(str(value))
            if plaintext is not None:
                settings[str(key)] = json.loads(plaintext)
        return settings

    def delete_module(self, module_id: str) -> bool:
        with self.connection:
            r1 = self.connection.execute("DELETE FROM module_state WHERE module_id = ?", (module_id,))
            r2 = self.connection.execute("DELETE FROM form_state WHERE module_id = ?", (module_id,))
        return (r1.rowcount > 0) or (r2.rowcount > 0)

    def relocate(self, path: str | Path) -> None:
        dest = Path(path)
        if self.path.resolve() == dest.resolve():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.parent.chmod(0o700)
        target = sqlite3.connect(dest)
        try:
            self.connection.commit()
            self.connection.backup(target)
        finally:
            target.close()
        self.connection.close()
        self.path = dest
        self.connection = sqlite3.connect(dest)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        dest.chmod(0o600)

    def close(self) -> None:
        self.connection.close()
