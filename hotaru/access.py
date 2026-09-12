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
class SecurityGroup:
    name: str
    users: list[int]
    permissions: list[dict[str, Any]]

    def validate(self) -> None:
        if not self.name or not self.name.isalnum():
            raise AccessError("group name must be non-empty alphanumeric")
        if len(self.name) > 32:
            raise AccessError("group name is too long")


@dataclass(slots=True)
class TsecRule:
    target_type: str
    target: int | str
    rule_type: str
    rule: str
    expires: float = 0.0
    label: str = ""

    def validate(self) -> None:
        if self.target_type not in {"user", "chat", "sgroup"}:
            raise AccessError("rule target must be user, chat or sgroup")
        if self.rule_type not in {"command", "module"}:
            raise AccessError("rule must reference a command or a module")
        if not self.rule:
            raise AccessError("rule value is required")
        if self.expires < 0:
            raise AccessError("rule duration must not be negative")


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
    KEY_OWNERS = "access:owners"
    KEY_SGROUPS = "access:sgroups"
    KEY_TSEC = "access:tsec"

    def __init__(self, owner_id: int | None, store: AccessStore) -> None:
        self.owner_id = owner_id
        self.store = store
        self._groups_cache: dict[str, SecurityGroup] | None = None
        self._tsec_cache: list[TsecRule] | None = None

    def set_owner(self, owner_id: int | None) -> None:
        if owner_id is not None and (not isinstance(owner_id, int) or owner_id <= 0):
            raise AccessError("owner id must be a positive integer")
        self.owner_id = owner_id

    @property
    def owners(self) -> tuple[int, ...]:
        raw = self.store.state.get_setting(self.KEY_OWNERS, [])
        extra = tuple(sorted({int(u) for u in raw if isinstance(u, int) and u > 0})) if isinstance(raw, list) else ()
        return (self.owner_id,) + tuple(u for u in extra if u != self.owner_id) if self.owner_id is not None else extra

    def add_owner(self, user_id: int) -> bool:
        if not isinstance(user_id, int) or user_id <= 0:
            raise AccessError("user id must be a positive integer")
        if user_id == self.owner_id or user_id in self.owners:
            return False
        raw = self.store.state.get_setting(self.KEY_OWNERS, [])
        users = [u for u in raw if isinstance(u, int)] if isinstance(raw, list) else []
        users.append(user_id)
        self.store.state.set_setting(self.KEY_OWNERS, users)
        return True

    def remove_owner(self, user_id: int) -> bool:
        if user_id == self.owner_id:
            raise AccessError("account owner cannot be removed")
        raw = self.store.state.get_setting(self.KEY_OWNERS, [])
        users = [u for u in raw if isinstance(u, int)] if isinstance(raw, list) else []
        if user_id not in users:
            return False
        users.remove(user_id)
        self.store.state.set_setting(self.KEY_OWNERS, users)
        return True

    def sgroups(self) -> dict[str, SecurityGroup]:
        if self._groups_cache is not None:
            return self._groups_cache
        raw = self.store.state.get_setting(self.KEY_SGROUPS, {})
        groups: dict[str, SecurityGroup] = {}
        if isinstance(raw, dict):
            for name, body in raw.items():
                if not isinstance(name, str) or not isinstance(body, dict):
                    continue
                users = [int(u) for u in body.get("users", []) if isinstance(u, int)]
                perms = [dict(p) for p in body.get("permissions", []) if isinstance(p, dict)]
                groups[name] = SecurityGroup(name=name, users=users, permissions=perms)
        self._groups_cache = groups
        return groups

    def sgroup(self, name: str) -> SecurityGroup | None:
        return self.sgroups().get(name)

    def save_sgroups(self, groups: dict[str, SecurityGroup]) -> None:
        payload = {
            name: {"users": list(g.users), "permissions": [dict(p) for p in g.permissions]}
            for name, g in groups.items()
        }
        self.store.state.set_setting(self.KEY_SGROUPS, payload)
        self._groups_cache = dict(groups)

    def create_sgroup(self, name: str) -> SecurityGroup:
        group = SecurityGroup(name=name, users=[], permissions=[])
        group.validate()
        groups = self.sgroups()
        if name in groups:
            raise AccessError(f"group {name} already exists")
        groups[name] = group
        self.save_sgroups(groups)
        return group

    def delete_sgroup(self, name: str) -> bool:
        groups = self.sgroups()
        if name not in groups:
            return False
        groups.pop(name)
        self.save_sgroups(groups)
        rules = [r for r in self.tsec_rules() if not (r.target_type == "sgroup" and r.target == name)]
        self.save_tsec(rules)
        return True

    def tsec_rules(self) -> list[TsecRule]:
        if self._tsec_cache is not None:
            return self._tsec_cache
        raw = self.store.state.get_setting(self.KEY_TSEC, [])
        rules: list[TsecRule] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                try:
                    rule = TsecRule(
                        target_type=str(item.get("target_type", "")),
                        target=item.get("target"),
                        rule_type=str(item.get("rule_type", "")),
                        rule=str(item.get("rule", "")),
                        expires=float(item.get("expires", 0.0) or 0.0),
                        label=str(item.get("label", "") or ""),
                    )
                    rule.validate()
                    rules.append(rule)
                except AccessError:
                    continue
        self._tsec_cache = rules
        return rules

    def save_tsec(self, rules: list[TsecRule]) -> None:
        payload = [
            {
                "target_type": r.target_type,
                "target": r.target,
                "rule_type": r.rule_type,
                "rule": r.rule,
                "expires": r.expires,
                "label": r.label,
            }
            for r in rules
        ]
        self.store.state.set_setting(self.KEY_TSEC, payload)
        self._tsec_cache = list(rules)

    def add_tsec(self, rule: TsecRule) -> None:
        rule.validate()
        rules = self.tsec_rules()
        for existing in rules:
            if (
                existing.target_type == rule.target_type
                and existing.target == rule.target
                and existing.rule_type == rule.rule_type
                and existing.rule == rule.rule
            ):
                existing.expires = rule.expires
                self.save_tsec(rules)
                return
        rules.append(rule)
        self.save_tsec(rules)

    def remove_tsec(self, target_type: str, target: int | str, rule: str) -> bool:
        rules = self.tsec_rules()
        kept = []
        removed = False
        for existing in rules:
            if existing.target_type == target_type and existing.target == target and rule in {existing.rule, "*"}:
                removed = True
                continue
            kept.append(existing)
        if removed:
            self.save_tsec(kept)
        return removed

    def remove_tsec_all(self, target_type: str, target: int | str | None) -> bool:
        rules = self.tsec_rules()
        kept = []
        removed = False
        for existing in rules:
            if target is None or target == "*":
                if existing.target_type == target_type:
                    removed = True
                    continue
            elif existing.target_type == target_type and existing.target == target:
                removed = True
                continue
            kept.append(existing)
        if removed:
            self.save_tsec(kept)
        return removed

    def _prune_expired(self) -> None:
        now = time.time()
        rules = self.tsec_rules()
        kept = [r for r in rules if not r.expires or r.expires > now]
        if len(kept) != len(rules):
            self.save_tsec(kept)

    def check_tsec(self, user_id: int | None, module_id: str | None, command: str | None, chat_id: int | str | None) -> bool:
        if user_id is None:
            return False
        self._prune_expired()
        now = time.time()
        rules = self.tsec_rules()
        for group in self.sgroups().values():
            if user_id not in group.users:
                continue
            for perm in group.permissions:
                expires = perm.get("expires")
                if expires and isinstance(expires, (int, float)) and expires <= now:
                    continue
                if self._rule_matches(perm, module_id, command):
                    return True
        for rule in rules:
            if rule.target_type == "user" and rule.target == user_id and self._rule_body_matches(rule, module_id, command):
                return True
        if chat_id is not None:
            for rule in rules:
                if rule.target_type == "chat" and rule.target == chat_id and self._rule_body_matches(rule, module_id, command):
                    return True
        return False

    @staticmethod
    def _rule_body_matches(rule: TsecRule, module_id: str | None, command: str | None) -> bool:
        if rule.rule_type == "command" and command is not None:
            return rule.rule == command
        if rule.rule_type == "module" and module_id is not None:
            return rule.rule == module_id
        return False

    @staticmethod
    def _rule_matches(perm: dict[str, Any], module_id: str | None, command: str | None) -> bool:
        rule_type = perm.get("rule_type")
        rule = perm.get("rule")
        if rule_type == "command" and command is not None:
            return rule == command
        if rule_type == "module" and module_id is not None:
            return rule == module_id
        return False

    def is_owner(self, user_id: int | None) -> bool:
        if self.owner_id is not None and user_id == self.owner_id:
            return True
        if user_id is None:
            return False
        return user_id in self.owners

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
