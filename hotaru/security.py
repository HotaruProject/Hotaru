from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class AccessVerdict(Enum):
    ALLOW = "allow"
    DENY = "deny"
    SILENT = "silent"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: int | None
    is_me: bool
    chat_id: int | str | None
    transport: str


class SecurityError(RuntimeError):
    pass


@dataclass(slots=True)
class RateWindow:
    hits: list[float] = field(default_factory=list)

    def hit(self, now: float, *, max_hits: int, window: float) -> bool:
        self.hits = [t for t in self.hits if now - t < window]
        if len(self.hits) >= max_hits:
            return False
        self.hits.append(now)
        return True


@dataclass(frozen=True, slots=True)
class ModulePolicy:
    module_id: str
    allow_others: bool = False
    allow_groups: bool = False
    max_hits: int = 30
    window: float = 60.0


class SecurityGate:
    def __init__(self, owner_id: int | None = None, *, seen_limit: int = 4096) -> None:
        if seen_limit < 1:
            raise ValueError("seen_limit must be positive")
        self.owner_id = owner_id
        self._policies: dict[str, ModulePolicy] = {}
        self._windows: dict[tuple[str, int], RateWindow] = {}
        self._seen_limit = seen_limit
        self._recent: list[tuple[str, int]] = []

    def set_owner(self, owner_id: int | None) -> None:
        if owner_id is not None and not isinstance(owner_id, int):
            raise ValueError("owner id must be an integer")
        self.owner_id = owner_id

    def register_policy(self, policy: ModulePolicy) -> None:
        self._policies[policy.module_id] = policy

    def unregister_policy(self, module_id: str) -> bool:
        return self._policies.pop(module_id, None) is not None

    def principal_of(self, event: Any, *, transport: str) -> Principal:
        user_id = getattr(event, "from_id", None)
        chat_id = getattr(event, "chat_id", None)
        is_me = bool(getattr(event, "is_me", False))
        if not isinstance(user_id, int):
            user_id = None
        return Principal(user_id, is_me, chat_id, transport)

    def _is_owner(self, principal: Principal) -> bool:
        if principal.is_me:
            return True
        if self.owner_id is not None and principal.user_id == self.owner_id:
            return True
        return False

    def _rate_ok(self, principal: Principal, policy: ModulePolicy | None, *, owner: bool = False) -> bool:
        if owner:
            return True
        limits = (policy.max_hits, policy.window) if policy is not None else (30, 60.0)
        key = (principal.transport, principal.user_id or 0)
        window = self._windows.get(key)
        if window is None:
            window = RateWindow()
            self._windows[key] = window
            self._remember(key)
        return window.hit(time.monotonic(), max_hits=limits[0], window=limits[1])

    def _remember(self, key: tuple[str, int]) -> None:
        self._recent.append(key)
        while len(self._recent) > self._seen_limit:
            self._recent.pop(0)
            stale = None
            for candidate in list(self._windows):
                if candidate not in self._recent:
                    stale = candidate
                    break
            if stale is not None:
                self._windows.pop(stale, None)

    def check(self, event: Any, *, transport: str, module_id: str | None = None, is_group: bool = False) -> AccessVerdict:
        principal = self.principal_of(event, transport=transport)
        policy = self._policies.get(module_id) if module_id else None
        if self._is_owner(principal):
            if not self._rate_ok(principal, policy, owner=True):
                return AccessVerdict.SILENT
            return AccessVerdict.ALLOW
        if policy is not None and policy.allow_others:
            if is_group and not policy.allow_groups:
                return AccessVerdict.SILENT
            if not self._rate_ok(principal, policy):
                return AccessVerdict.SILENT
            return AccessVerdict.ALLOW
        return AccessVerdict.SILENT

    def check_callback(self, event: Any, *, transport: str, owner_binding: int | str | None = None) -> AccessVerdict:
        principal = self.principal_of(event, transport=transport)
        if principal.is_me:
            return AccessVerdict.ALLOW
        if owner_binding is not None and principal.user_id is not None:
            if str(principal.user_id) == str(owner_binding):
                return AccessVerdict.ALLOW
        if self.owner_id is not None and principal.user_id == self.owner_id:
            return AccessVerdict.ALLOW
        return AccessVerdict.SILENT
