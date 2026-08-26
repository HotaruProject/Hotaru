from __future__ import annotations

import inspect
import secrets
import time
from dataclasses import dataclass
from typing import Any


class CallbackDenied(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class CallbackBinding:
    actor: int | str
    chat_id: int | str
    message_id: int


@dataclass(slots=True)
class _Entry:
    binding: CallbackBinding
    value: Any
    expires: float


class CallbackStore:
    def __init__(self, *, ttl: float = 300.0, max_items: int = 4096) -> None:
        if ttl <= 0 or max_items < 1:
            raise ValueError("invalid callback store limits")
        self.ttl = ttl
        self.max_items = max_items
        self._items: dict[str, _Entry] = {}

    def issue(self, binding: CallbackBinding, value: Any) -> str:
        self._purge()
        if len(self._items) >= self.max_items:
            raise CallbackDenied("callback store is full")
        handle = secrets.token_urlsafe(9)
        while handle in self._items:
            handle = secrets.token_urlsafe(9)
        self._items[handle] = _Entry(binding, value, time.monotonic() + self.ttl)
        return handle

    def consume(self, handle: str, binding: CallbackBinding) -> Any:
        self._purge()
        entry = self._items.pop(handle, None)
        if entry is None or entry.binding != binding:
            raise CallbackDenied("callback is invalid or expired")
        return entry.value

    def _purge(self) -> None:
        now = time.monotonic()
        for handle, entry in tuple(self._items.items()):
            if entry.expires <= now:
                del self._items[handle]


class CallbackRouter:
    def __init__(self, store: CallbackStore | None = None) -> None:
        self.store = store or CallbackStore()
        self._handlers: dict[str, Any] = {}

    def register(self, action: str, handler: Any) -> None:
        if not action or action in self._handlers:
            raise ValueError("callback action is already registered")
        self._handlers[action] = handler

    async def dispatch(self, callback: Any) -> object:
        binding = CallbackBinding(
            actor=self._required(callback, "from_id"),
            chat_id=self._required(callback, "chat_id"),
            message_id=self._required(callback, "msg_id"),
        )
        value = self.store.consume(getattr(callback, "data", ""), binding)
        if not isinstance(value, dict) or not isinstance(value.get("action"), str):
            raise CallbackDenied("callback payload is invalid")
        handler = self._handlers.get(value["action"])
        if handler is None:
            raise CallbackDenied("callback action is unavailable")
        result = handler(callback, value.get("payload"))
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _required(callback: Any, name: str) -> int | str:
        value = getattr(callback, name, None)
        if value is None:
            raise CallbackDenied("callback identity is incomplete")
        return value
