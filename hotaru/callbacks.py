from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from goygram import ext
from relay.firewall import module_scope


class CallbackDenied(PermissionError):
    pass


class CallbackContext:
    def __init__(self, callback: Any) -> None:
        self._callback = callback
        for name in ("src", "raw", "app", "id", "chat_id", "from_id", "msg_id", "data", "text", "inline_message_id"):
            if hasattr(callback, name):
                setattr(self, name, getattr(callback, name))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._callback, name)

    async def answer(self, text: str | None = None, **kwargs: Any) -> Any:
        return await self._callback.answer(text, **kwargs)

    async def edit(self, text: str, **kwargs: Any) -> Any:
        if getattr(self, "inline_message_id", None) and getattr(self, "app", None) is not None:
            data = dict(kwargs)
            kbd = data.pop("kbd", None)
            if kbd is not None:
                data["reply_markup"] = kbd.to_dict() if hasattr(kbd, "to_dict") else kbd
            return await self.app.bot_req("editMessageText", inline_message_id=self.inline_message_id, text=text, **data)
        return await self._callback.edit(text, **kwargs)

    async def delete(self) -> Any:
        if getattr(self, "inline_message_id", None):
            if getattr(self, "app", None) is not None:
                return await self.app.bot_req("editMessageText", inline_message_id=self.inline_message_id, text="<i>Closed</i>", parse_mode="HTML")
            return None
        if hasattr(self._callback, "delete"):
            return await self._callback.delete()
        return None


@dataclass(frozen=True, slots=True)
class CallbackBinding:
    actor: int | str
    chat_id: int | str | None
    message_id: int | None


@dataclass(slots=True)
class _Entry:
    binding: CallbackBinding
    value: Any
    expires: float


def _derive_key(seed: str) -> bytes:
    return hashlib.sha256(("hotaru-cb:" + seed).encode()).digest()


class CallbackStore:
    def __init__(self, *, ttl: float = 300.0, max_items: int = 4096, secret: bytes | None = None) -> None:
        if ttl <= 0 or max_items < 1:
            raise ValueError("invalid callback store limits")
        self.ttl = ttl
        self.max_items = max_items
        self._key = secret or _derive_key(secrets.token_hex(16))
        self._items: dict[str, _Entry] = {}

    def _seal(self) -> str:
        nonce = secrets.token_bytes(12)
        marker = secrets.token_bytes(16)
        blob = ext.aes_gcm_encrypt(self._key, nonce, marker, b"hotaru-cb")
        return base64.urlsafe_b64encode(nonce + blob).decode("ascii").rstrip("=")

    def _unseal(self, handle: str) -> bytes | None:
        try:
            padded = handle + "=" * (-len(handle) % 4)
            blob = base64.urlsafe_b64decode(padded.encode("ascii"))
            return ext.aes_gcm_decrypt(self._key, blob[:12], blob[12:], b"hotaru-cb")
        except Exception:
            return None

    def issue(self, binding: CallbackBinding, value: dict[str, Any]) -> str:
        self._purge()
        if len(self._items) >= self.max_items:
            raise CallbackDenied("callback store is full")
        handle = self._seal()
        self._items[handle] = _Entry(binding, value, time.monotonic() + self.ttl)
        return handle

    def consume(self, handle: str, binding: CallbackBinding) -> Any:
        self._purge()
        entry = self._items.get(handle)
        decoded = self._unseal(handle)
        chat_ok = entry is not None and (entry.binding.chat_id in (None, 0) or entry.binding.chat_id == binding.chat_id)
        message_ok = entry is not None and (entry.binding.message_id in (None, 0) or entry.binding.message_id == binding.message_id)
        if entry is None or entry.binding.actor != binding.actor or not chat_ok or not message_ok or not isinstance(decoded, bytes):
            raise CallbackDenied("callback is invalid or expired")
        del self._items[handle]
        return entry.value

    def rebind(self, handle: str, binding: CallbackBinding) -> str:
        self._purge()
        entry = self._items.get(handle)
        if entry is None or not isinstance(self._unseal(handle), bytes):
            raise CallbackDenied("callback is invalid or expired")
        return self.issue(binding, entry.value)

    def _purge(self) -> None:
        now = time.monotonic()
        for handle, entry in tuple(self._items.items()):
            if entry.expires <= now:
                del self._items[handle]


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(text: str) -> Any:
    import json

    return json.loads(text)


class CallbackRouter:
    def __init__(self, store: CallbackStore | None = None) -> None:
        self.store = store or CallbackStore()
        self._handlers: dict[str, Any] = {}
        self._module_handlers: dict[str, dict[str, Any]] = {}
        self._module_seq = 0

    def register(self, action: str, handler: Any) -> None:
        if not action or action in self._handlers:
            raise ValueError("callback action is already registered")
        self._handlers[action] = handler

    def register_module_action(self, module_id: str, handler: Any) -> str:
        if not module_id or not callable(handler):
            raise ValueError("module callback is invalid")
        name = getattr(handler, "__qualname__", getattr(handler, "__name__", "handler"))
        action_id = hashlib.sha256((module_id + ":" + name).encode()).hexdigest()[:24]
        handlers = self._module_handlers.setdefault(module_id, {})
        if action_id in handlers and handlers[action_id] is not handler:
            action_id = hashlib.sha256((module_id + ":" + name + ":" + str(id(handler))).encode()).hexdigest()[:24]
        handlers[action_id] = handler
        return action_id

    def unregister_module(self, module_id: str) -> None:
        self._module_handlers.pop(module_id, None)

    def module_action_exists(self, module_id: str, action_id: str) -> bool:
        return action_id in self._module_handlers.get(module_id, {})

    def register_module_action_id(self, module_id: str, action_id: str, handler: Any) -> str:
        if not module_id or not action_id or not callable(handler):
            raise ValueError("module callback is invalid")
        self._module_handlers.setdefault(module_id, {})[action_id] = handler
        return action_id

    def issue_module(self, module_id: str, action_id: str, binding: CallbackBinding, payload: Any = None) -> str:
        handlers = self._module_handlers.get(module_id, {})
        if action_id not in handlers:
            raise CallbackDenied("module callback is unavailable")
        return self.store.issue(binding, {"module": module_id, "action_id": action_id, "payload": payload})

    async def dispatch(self, callback: Any) -> object:
        binding = CallbackBinding(
            actor=self._required(callback, "from_id"),
            chat_id=self._optional(callback, "chat_id"),
            message_id=self._optional(callback, "msg_id"),
        )
        value = self.store.consume(getattr(callback, "data", ""), binding)
        if not isinstance(value, dict):
            raise CallbackDenied("callback payload is invalid")
        if isinstance(value.get("module"), str):
            handlers = self._module_handlers.get(value["module"], {})
            handler = handlers.get(str(value.get("action_id")))
        else:
            handler = self._handlers.get(value.get("action")) if isinstance(value.get("action"), str) else None
        if handler is None:
            raise CallbackDenied("callback action is unavailable")
        with module_scope():
            result = handler(CallbackContext(callback), value.get("payload"))
            if inspect.isawaitable(result):
                return await result
            return result

    @staticmethod
    def _required(callback: Any, name: str) -> int | str:
        value = getattr(callback, name, None)
        if value is None:
            raise CallbackDenied("callback identity is incomplete")
        return value

    @staticmethod
    def _optional(callback: Any, name: str) -> int | str | None:
        value = getattr(callback, name, None)
        return value if isinstance(value, (int, str)) else None
