from __future__ import annotations

import base64
import hashlib
import inspect
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from goygram.errors import EntityBoundsInvalidError
from goygram import ext
from relay.firewall import module_scope
from goygram.rich import rich_html
from goygram.sugar import html_to_entities
from goygram.types.kbd import kbd_to_tl
from relay.rpc import delete_chat_msg


def _cb_log(data: dict[str, Any]) -> None:
    try:
        path = Path("observatory/runtime/callback_fail.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


class CallbackDenied(PermissionError):
    pass


class CallbackContext:
    def __init__(self, callback: Any) -> None:
        self._callback = callback
        for name in ("src", "raw", "app", "id", "chat_id", "from_id", "msg_id", "data", "text", "inline_message_id"):
            if hasattr(callback, name):
                setattr(self, name, getattr(callback, name))
        if getattr(self, "src", None) == "mt" and getattr(self, "msg_id", None) is not None and not isinstance(getattr(self, "msg_id", None), int):
            self.inline_message_id = self.msg_id
        if getattr(self, "src", None) == "bot" and not getattr(self, "inline_message_id", None):
            raw_value = cast(object, getattr(self, "raw", {}))
            raw = cast('dict[str, object]', raw_value) if isinstance(raw_value, dict) else {}
            query_value = raw.get("callback_query")
            nested_value = raw.get("raw")
            if not isinstance(query_value, dict) and isinstance(nested_value, dict):
                query_value = cast('dict[str, object]', nested_value).get("callback_query")
            if isinstance(query_value, dict):
                inline_message_id = cast('dict[str, object]', query_value).get("inline_message_id")
                if isinstance(inline_message_id, str):
                    self.inline_message_id = inline_message_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._callback, name)

    async def answer(self, text: str | None = None, **kwargs: Any) -> Any:
        kwargs.pop("show_alert", None)
        alert = kwargs.pop("alert", False)
        return await self._callback.answer(text, alert=alert, **kwargs)

    @staticmethod
    async def _edit_inline(app: Any, id_field: dict[str, Any], message: str, data: dict[str, Any]) -> Any:
        from relay.firewall import trusted_scope

        try:
            with trusted_scope():
                return await app.mt_messages_edit_inline_bot_message(id=id_field, message=message, **data)
        except EntityBoundsInvalidError:
            fallback = dict(data)
            entities = fallback.get("entities")
            pre = [cast('dict[str, object]', entity) for entity in cast('list[object]', entities) if isinstance(entity, dict) and cast('dict[str, object]', entity).get("_") == "messageEntityPre"] if isinstance(entities, list) else []
            if pre:
                fallback["entities"] = pre
                try:
                    with trusted_scope():
                        return await app.mt_messages_edit_inline_bot_message(id=id_field, message=message, **fallback)
                except EntityBoundsInvalidError:
                    pass
            fallback.pop("entities", None)
            with trusted_scope():
                return await app.mt_messages_edit_inline_bot_message(id=id_field, message=message, **fallback)

    async def edit(self, text: str, **kwargs: Any) -> Any:
        inline_mid = getattr(self, "inline_message_id", None)
        app = getattr(self, "app", None)
        chat_id = getattr(self, "chat_id", None)
        msg_id = getattr(self, "msg_id", None)
        log = {
            "src": getattr(self, "src", None),
            "chat_id": chat_id,
            "msg_id": msg_id,
            "msg_id_type": type(msg_id).__name__,
            "inline_mid_type": type(inline_mid).__name__,
            "inline_mid": inline_mid if not isinstance(inline_mid, (bytes, bytearray)) else "bytes",
            "text_len": len(text or ""),
            "update_type": getattr(self, "update_type", None),
        }
        if getattr(self, "src", None) == "mt" and app is not None:
            data = dict(kwargs)
            use_rich = bool(data.pop("rich", False))
            raw_kbd = data.pop("reply_markup", data.pop("kbd", None))
            if raw_kbd is not None:
                markup = kbd_to_tl(raw_kbd)
                if markup is not None:
                    data["reply_markup"] = markup
            data.pop("parse_mode", None)
            plain, ents = html_to_entities(text)
            if ents:
                data["entities"] = ents
            log["plain_len"] = len(plain or "")
            log["plain_u16"] = len((plain or "").encode("utf-16-le")) // 2
            log["ents"] = len(ents)
            log["entities"] = ents
            runtime = getattr(self._callback, "_hotaru_runtime", None)
            bot_app = getattr(getattr(runtime, "inline", None), "bot_app", None) if runtime is not None else None
            app = bot_app or app
            log["bot"] = bool(getattr(app, "bot_token", None))
            if inline_mid is not None:
                inline_id: dict[str, Any] | None = cast('dict[str, Any]', inline_mid) if isinstance(inline_mid, dict) else {"_": "inputBotInlineMessageID", "raw": inline_mid} if isinstance(inline_mid, (str, bytes)) else None
                log["branch"] = "editInlineBotMessage"
                log["id_field"] = inline_id
                log["dc"] = inline_id.get("dc_id") if inline_id is not None else None
                _cb_log(log)
                if inline_id is None:
                    return None
                if use_rich:
                    data.pop("entities", None)
                    data["rich_message"] = {"_": "inputRichMessageHTML", **rich_html(text)}
                return await self._edit_inline(app, inline_id, "" if use_rich else plain, data)
            log["branch"] = "editMessage"
            _cb_log(log)
            if isinstance(chat_id, int) and isinstance(msg_id, int):
                return await app.mt_messages_edit_message(peer=chat_id, id=int(msg_id), message=plain, **data)
            return None
        if inline_mid is not None and app is not None:
            data = dict(kwargs)
            use_rich = bool(data.pop("rich", False))
            kbd = data.pop("kbd", None)
            data.pop("parse_mode", None)
            if kbd is not None:
                markup = kbd_to_tl(kbd.to_dict() if hasattr(kbd, "to_dict") else kbd)
                if markup is not None:
                    data["reply_markup"] = markup
            plain, ents = html_to_entities(text)
            if ents:
                data["entities"] = ents
            bot_inline_id: dict[str, Any] = cast('dict[str, Any]', inline_mid) if isinstance(inline_mid, dict) else {"_": "inputBotInlineMessageID", "raw": inline_mid}
            log["branch"] = "editInlineBotMessage-bot"
            log["id_field"] = bot_inline_id
            _cb_log(log)
            if use_rich:
                data.pop("entities", None)
                data["rich_message"] = {"_": "inputRichMessageHTML", **rich_html(text)}
            return await self._edit_inline(app, bot_inline_id, "" if use_rich else plain, data)
        log["branch"] = "fallback"
        _cb_log(log)
        return await self._callback.edit(text, **kwargs)

    async def delete(self) -> Any:
        try:
            await self.answer()
        except Exception:
            pass
        runtime = getattr(self._callback, "_hotaru_runtime", None)
        user_app = getattr(runtime, "app", None) if runtime is not None else None
        app = user_app or getattr(self, "app", None)
        chat_id = getattr(self, "chat_id", None)
        msg_id = getattr(self, "msg_id", None)
        if not isinstance(msg_id, int):
            msg_id = None
        if not (isinstance(chat_id, int) and isinstance(msg_id, int)):
            actor = getattr(self, "from_id", None)
            stored = getattr(runtime, "_form_msgs", None) if runtime is not None else None
            if isinstance(stored, dict) and actor in stored:
                mapping = cast('dict[object, object]', stored)
                chat_id, msg_id = cast('tuple[object, object]', mapping.pop(actor))
            elif isinstance(stored, dict) and len(cast('dict[object, object]', stored)) == 1:
                mapping = cast('dict[object, object]', stored)
                chat_id, msg_id = cast('tuple[object, object]', mapping.pop(next(iter(mapping))))
        if isinstance(chat_id, int) and isinstance(msg_id, int) and app is not None:
            from relay.firewall import trusted_scope
            with trusted_scope():
                return await delete_chat_msg(app, chat_id, msg_id)
        inline_mid = getattr(self, "inline_message_id", None)
        bot_app = getattr(self, "app", None)
        if inline_mid is not None and bot_app is not None:
            id_field: dict[str, Any] | None = cast('dict[str, Any]', inline_mid) if isinstance(inline_mid, dict) else {"_": "inputBotInlineMessageID", "raw": inline_mid} if isinstance(inline_mid, (str, bytes)) else None
            if id_field is not None:
                from relay.firewall import trusted_scope
                with trusted_scope():
                    return await bot_app.mt_messages_edit_inline_bot_message( id=id_field, message="\u200b", reply_markup={"_": "replyInlineMarkup", "rows": []})
        if hasattr(self._callback, "delete") and getattr(self._callback, "delete") is not self.delete:
            return await self._callback.delete()
        return None


@dataclass(frozen=True)
class CallbackBinding:
    actor: int | str
    chat_id: int | str | None
    message_id: int | None


@dataclass
class _Entry:
    binding: CallbackBinding
    value: dict[str, Any]
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
            if len(blob) <= 12 or len(blob) < 12 + 16:
                return None
            return ext.aes_gcm_decrypt(self._key, blob[:12], blob[12:], b"hotaru-cb")
        except BaseException:
            return None

    def issue(self, binding: CallbackBinding, value: dict[str, Any]) -> str:
        self._purge()
        if len(self._items) >= self.max_items:
            raise CallbackDenied("callback store is full")
        handle = self._seal()
        self._items[handle] = _Entry(binding, value, time.monotonic() + self.ttl)
        return handle

    def consume(self, handle: str, binding: CallbackBinding) -> dict[str, Any]:
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
        mid = self._optional(callback, "msg_id")
        binding = CallbackBinding(
            actor=self._required(callback, "from_id"),
            chat_id=self._optional(callback, "chat_id"),
            message_id=mid if isinstance(mid, int) else None,
        )
        data = getattr(callback, "data", "")
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", "replace")
        if not isinstance(data, str):
            raise CallbackDenied("callback payload is invalid")
        value = self.store.consume(data, binding)
        handler = None
        if isinstance(value.get("module"), str):
            handlers = self._module_handlers.get(value["module"], {})
            handler = handlers.get(str(value.get("action_id")))
        else:
            action = value.get("action")
            handler = self._handlers.get(action) if isinstance(action, str) else None
        if handler is None:
            raise CallbackDenied("callback action is unavailable")
        with module_scope(str(value.get("module") or "")):
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
