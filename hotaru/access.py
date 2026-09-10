from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import IntFlag
from typing import Any

from .state import StateStore


class AccessError(RuntimeError):
    pass


class Permission(IntFlag):
    OWNER = 1 << 0
    ADMIN = 1 << 1
    TRUSTED = 1 << 2
    EVERYONE = 1 << 3


DEFAULT_COMMAND_PERMISSION = Permission.OWNER
PUBLIC_COMMAND_PERMISSION = Permission.EVERYONE


@dataclass(slots=True)
class AccessEntry:
    user_id: int
    permissions: Permission = Permission(0)
    label: str = ""
    added_at: float = 0.0
    added_by: int | None = None


@dataclass(slots=True)
class AccessStore:
    state: StateStore
    _cache: dict[int, AccessEntry] = field(default_factory=dict)

    KEY = "access:acl"

    def __post_init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        raw = self.state.get_setting(self.KEY, [])
        entries: dict[int, AccessEntry] = {}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                user_id = item.get("user_id")
                if not isinstance(user_id, int) or user_id <= 0:
                    continue
                bits = item.get("permissions", 0)
                entries[user_id] = AccessEntry(
                    user_id=user_id,
                    permissions=Permission(bits) if isinstance(bits, int) else Permission(0),
                    label=str(item.get("label", "") or ""),
                    added_at=float(item.get("added_at", 0.0) or 0.0),
                    added_by=item.get("added_by"),
                )
        self._cache = entries

    def _flush(self) -> None:
        payload = [
            {
                "user_id": entry.user_id,
                "permissions": int(entry.permissions),
                "label": entry.label,
                "added_at": entry.added_at,
                "added_by": entry.added_by,
            }
            for entry in self._cache.values()
        ]
        self.state.set_setting(self.KEY, payload)

    def entry(self, user_id: int) -> AccessEntry | None:
        return self._cache.get(user_id)

    def grant(self, user_id: int, permissions: Permission, *, label: str = "", added_by: int | None = None) -> AccessEntry:
        if not isinstance(user_id, int) or user_id <= 0:
            raise AccessError("user id must be a positive integer")
        entry = self._cache.get(user_id)
        if entry is None:
            entry = AccessEntry(user_id=user_id, permissions=Permission(0), added_at=time.time(), added_by=added_by)
            self._cache[user_id] = entry
        entry.permissions |= permissions
        if label:
            entry.label = label
        if added_by is not None:
            entry.added_by = added_by
        self._flush()
        return entry

    def revoke(self, user_id: int, permissions: Permission) -> bool:
        entry = self._cache.get(user_id)
        if entry is None or not (entry.permissions & permissions):
            return False
        entry.permissions &= ~permissions
        if not entry.permissions:
            self._cache.pop(user_id, None)
        self._flush()
        return True

    def remove(self, user_id: int) -> bool:
        if user_id not in self._cache:
            return False
        self._cache.pop(user_id, None)
        self._flush()
        return True

    def has(self, user_id: int, permission: Permission) -> bool:
        entry = self._cache.get(user_id)
        return entry is not None and bool(entry.permissions & permission)

    def items(self) -> tuple[AccessEntry, ...]:
        return tuple(sorted(self._cache.values(), key=lambda item: item.user_id))

    def is_empty(self) -> bool:
        return not self._cache


PERMISSION_LABELS = {
    Permission.OWNER: "owner",
    Permission.ADMIN: "admin",
    Permission.TRUSTED: "trusted",
    Permission.EVERYONE: "everyone",
}


class AccessManager:
    def __init__(self, owner_id: int | None, store: AccessStore) -> None:
        self.owner_id = owner_id
        self.store = store

    def set_owner(self, owner_id: int | None) -> None:
        if owner_id is not None and (not isinstance(owner_id, int) or owner_id <= 0):
            raise AccessError("owner id must be a positive integer")
        self.owner_id = owner_id

    def is_owner(self, user_id: int | None) -> bool:
        return self.owner_id is not None and user_id == self.owner_id

    def rank_of(self, user_id: int | None) -> int:
        if self.is_owner(user_id):
            return 3
        if user_id is None:
            return 0
        entry = self.store.entry(user_id)
        if entry is None:
            return 0
        if entry.permissions & Permission.ADMIN:
            return 2
        if entry.permissions & Permission.TRUSTED:
            return 1
        return 0

    def permissions_of(self, user_id: int | None) -> Permission:
        if self.is_owner(user_id):
            return Permission.OWNER | Permission.ADMIN | Permission.TRUSTED | Permission.EVERYONE
        entry = self.store.entry(user_id) if user_id is not None else None
        if entry is None:
            return Permission(0)
        return entry.permissions

    def allows(self, user_id: int | None, permission: Permission) -> bool:
        if self.is_owner(user_id):
            return True
        return bool(self.permissions_of(user_id) & permission)

    def user_labels(self, user_id: int | None) -> tuple[str, ...]:
        permissions = self.permissions_of(user_id)
        return tuple(
            name
            for perm, name in PERMISSION_LABELS.items()
            if permissions & perm and not (perm is Permission.EVERYONE and permissions != Permission.EVERYONE)
        )

    def set_default_permission(self, module_id: str, command: str, permission: Permission) -> None:
        if command in RESERVED_COMMANDS:
            raise AccessError(f"kernel command is reserved: {command}")
        raw = self.store.state.get_setting("access:defaults", {})
        if not isinstance(raw, dict):
            raw = {}
        raw[f"{module_id}:{command}"] = int(permission)
        self.store.state.set_setting("access:defaults", raw)

    def default_permission(self, module_id: str, command: str) -> Permission:
        raw = self.store.state.get_setting("access:defaults", {})
        if isinstance(raw, dict):
            bits = raw.get(f"{module_id}:{command}")
            if isinstance(bits, int):
                return Permission(bits)
        return DEFAULT_COMMAND_PERMISSION

    def clear_default(self, module_id: str, command: str) -> bool:
        raw = self.store.state.get_setting("access:defaults", {})
        if not isinstance(raw, dict):
            return False
        key = f"{module_id}:{command}"
        if key not in raw:
            return False
        raw.pop(key)
        self.store.state.set_setting("access:defaults", raw)
        return True

    def defaults_items(self) -> tuple[tuple[str, str, Permission], ...]:
        raw = self.store.state.get_setting("access:defaults", {})
        if not isinstance(raw, dict):
            return ()
        result = []
        for key, bits in sorted(raw.items()):
            module_id, _, command = key.partition(":")
            if command:
                result.append((module_id, command, Permission(bits) if isinstance(bits, int) else Permission(0)))
        return tuple(result)


RESERVED_COMMANDS = frozenset({
    "ver", "st", "ls", "mi", "ld", "ul", "rl", "rm", "bk", "bot", "trust", "untrust", "alias", "unalias",
    "hlp", "help", "restart", "stop", "start", "upd", "updlog", "updoff", "updon",
    "conf", "config", "acc", "acca", "accs", "perms", "perm", "whois", "grant", "revoke",
})


def permission_from_label(label: str) -> Permission | None:
    normalized = (label or "").strip().casefold()
    mapping = {
        "owner": Permission.OWNER,
        "владелец": Permission.OWNER,
        "admin": Permission.ADMIN,
        "админ": Permission.ADMIN,
        "администратор": Permission.ADMIN,
        "trusted": Permission.TRUSTED,
        "доверенный": Permission.TRUSTED,
        "everyone": Permission.EVERYONE,
        "все": Permission.EVERYONE,
    }
    return mapping.get(normalized)


def permission_label(permission: Permission) -> str:
    if permission == Permission.EVERYONE:
        return "everyone"
    names = []
    if permission & Permission.OWNER:
        names.append("owner")
    if permission & Permission.ADMIN:
        names.append("admin")
    if permission & Permission.TRUSTED:
        names.append("trusted")
    return "+".join(names) or "none"
