from __future__ import annotations

import base64
import hashlib
import inspect
import json
import secrets
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

    @staticmethod
    def _has_input_buttons(buttons: Any) -> bool:
        if not isinstance(buttons, list):
            return False
        for row in cast("list[Any]", buttons):
            items: list[Any] = [cast("Any", row)] if isinstance(row, dict) else (cast("list[Any]", row) if isinstance(row, list) else [])
            for btn in items:
                b = cast("dict[str, Any]", btn)
                if isinstance(btn, dict) and isinstance(b.get("input"), str):
                    return True
        return False


    async def edit(self, text: str, **kwargs: Any) -> Any:
        runtime = getattr(self._callback, "_hotaru_runtime", None)
        raw_buttons = kwargs.get("buttons")
        if runtime is not None and self._has_input_buttons(raw_buttons):
            from hotaru.runtime import InputContext
            buttons = cast("list[Any]", kwargs.pop("buttons", []))
            kwargs.pop("kbd", None)
            if not isinstance(getattr(self, "chat_id", None), int):
                if runtime.state is not None:
                    ref_chat = runtime.state.get_setting("inline-reference-chat")
                    if isinstance(ref_chat, int):
                        self.chat_id = ref_chat
                if not isinstance(getattr(self, "chat_id", None), int) and runtime._form_msgs:
                    for c_id, _ in runtime._form_msgs.values():
                        if isinstance(c_id, int):
                            self.chat_id = c_id
                            break
            ctx = InputContext(runtime, self, None, "", None)
            ctx.inline_message_id = getattr(self, "inline_message_id", None)
            ctx.form_nonce = getattr(self, "form_nonce", None)
            assert runtime is not None
            return await runtime._edit_input_form(ctx, text, buttons, kwargs)



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
            raw_buttons = data.pop("buttons", None)
            raw_kbd = data.pop("reply_markup", data.pop("kbd", None))
            if raw_kbd is None and raw_buttons is not None:
                raw_kbd = {"inline_keyboard": raw_buttons}
            if raw_kbd is not None:
                markup = kbd_to_tl(raw_kbd)
                if markup is not None:
                    data["reply_markup"] = markup
            data.pop("parse_mode", None)
            plain, raw_ents = html_to_entities(text)
            ents = [e for e in raw_ents if int(e.get("length", 0)) > 0]
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
            raw_buttons = data.pop("buttons", None)
            kbd = data.pop("kbd", data.pop("reply_markup", None))
            if kbd is None and raw_buttons is not None:
                kbd = {"inline_keyboard": raw_buttons}
            data.pop("parse_mode", None)
            if kbd is not None:
                raw_k: Any = kbd
                markup = kbd_to_tl(raw_k.to_dict() if hasattr(raw_k, "to_dict") else raw_k)
                if markup is not None:
                    data["reply_markup"] = markup
            plain, raw_ents = html_to_entities(text)
            ents = [e for e in raw_ents if int(e.get("length", 0)) > 0]
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
    consumed: bool = False


def derive_key(seed: str) -> bytes:
    return hashlib.sha256(("hotaru-cb:" + seed).encode()).digest()


_derive_key = derive_key


class CallbackStore:
    def __init__(self, *, ttl: float = 0.0, max_items: int = 16384, secret: bytes | None = None, connection: Any = None) -> None:
        self.ttl = ttl
        self.max_items = max_items
        self._key = secret or _derive_key(secrets.token_hex(16))
        self.connection = connection
        self._items: dict[str, _Entry] = {}
        if self.connection is not None:
            self._init_db()

    def _init_db(self) -> None:
        if self.connection is None:
            return
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS callback_store ("
            "handle TEXT PRIMARY KEY, "
            "actor TEXT NOT NULL, "
            "chat_id TEXT, "
            "message_id INTEGER, "
            "value TEXT NOT NULL, "
            "consumed INTEGER NOT NULL DEFAULT 0)"
        )
        self.connection.commit()

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

    def issue(self, binding: CallbackBinding, value: dict[str, Any], handle: str | None = None) -> str:
        self._prune()
        if handle is None:
            handle = self._seal()
        entry = _Entry(binding, value, consumed=False)
        self._items[handle] = entry
        if self.connection is not None:
            chat_str = str(binding.chat_id) if binding.chat_id is not None else None
            val_str = json.dumps(value, ensure_ascii=False, default=str)
            self.connection.execute(
                "INSERT OR REPLACE INTO callback_store(handle, actor, chat_id, message_id, value, consumed) VALUES (?, ?, ?, ?, ?, 0)",
                (handle, str(binding.actor), chat_str, binding.message_id, val_str),
            )
            self.connection.commit()
        return handle

    def consume(self, handle: str, binding: CallbackBinding) -> dict[str, Any]:
        entry = self._items.get(handle)
        if entry is None and self.connection is not None:
            row = self.connection.execute(
                "SELECT actor, chat_id, message_id, value, consumed FROM callback_store WHERE handle = ?",
                (handle,),
            ).fetchone()
            if row is not None:
                actor_val: str = str(row[0])
                chat_raw = row[1]
                chat_val: int | str | None = int(chat_raw) if (isinstance(chat_raw, str) and chat_raw.lstrip("-").isdigit()) else chat_raw
                msg_val: int | None = int(row[2]) if isinstance(row[2], (int, str)) and str(row[2]).isdigit() else None
                val_data = cast('dict[str, Any]', json.loads(row[3]))
                is_consumed = bool(row[4])
                entry = _Entry(
                    CallbackBinding(
                        int(actor_val) if actor_val.isdigit() else actor_val,
                        chat_val,
                        msg_val,
                    ),
                    val_data,
                    consumed=is_consumed,
                )
                self._items[handle] = entry
        decoded = self._unseal(handle)
        if entry is None or not isinstance(decoded, bytes):
            raise CallbackDenied("callback is invalid")
        if entry.consumed:
            raise CallbackDenied("callback has already been used")
        chat_ok = entry.binding.chat_id in (None, 0) or binding.chat_id in (None, 0) or str(entry.binding.chat_id) == str(binding.chat_id)
        message_ok = entry.binding.message_id in (None, 0) or binding.message_id in (None, 0) or entry.binding.message_id == binding.message_id
        actor_ok = str(entry.binding.actor) == str(binding.actor)
        if not actor_ok or not chat_ok or not message_ok:
            raise CallbackDenied("callback is invalid")
        entry.consumed = True
        if self.connection is not None:
            self.connection.execute("UPDATE callback_store SET consumed = 1 WHERE handle = ?", (handle,))
            self.connection.commit()
        return entry.value

    def rebind(self, handle: str, binding: CallbackBinding) -> str:
        entry = self._items.get(handle)
        if entry is None and self.connection is not None:
            row = self.connection.execute(
                "SELECT actor, chat_id, message_id, value, consumed FROM callback_store WHERE handle = ?",
                (handle,),
            ).fetchone()
            if row is not None:
                val_data = cast('dict[str, Any]', json.loads(row[3]))
                entry = _Entry(binding, val_data, consumed=bool(row[4]))
                self._items[handle] = entry
        if entry is None or not isinstance(self._unseal(handle), bytes):
            raise CallbackDenied("callback is invalid")
        if entry.consumed:
            raise CallbackDenied("callback has already been used")
        entry.consumed = True
        if self.connection is not None:
            self.connection.execute("UPDATE callback_store SET consumed = 1 WHERE handle = ?", (handle,))
            self.connection.commit()
        return self.issue(binding, entry.value)

    def _prune(self) -> None:
        if len(self._items) > self.max_items:
            consumed_keys = [k for k, v in self._items.items() if v.consumed]
            for k in consumed_keys[:len(self._items) - self.max_items]:
                del self._items[k]


class CallbackRouter:
    def __init__(self, store: CallbackStore | None = None, runtime: Any = None) -> None:
        self.store = store or CallbackStore()
        self.runtime = runtime
        self._handlers: dict[str, Any] = {}
        self._module_handlers: dict[str, dict[str, Any]] = {}
        self._module_seq = 0

    @staticmethod
    async def _default_close_handler(callback: Any, payload: Any = None) -> Any:
        try:
            await callback.answer()
        except Exception:
            pass
        deleter = getattr(callback, "delete", None)
        if callable(deleter):
            result = deleter()
            if inspect.isawaitable(result):
                return await result
            return result
        return None

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
            module_id = value["module"]
            handlers = self._module_handlers.get(module_id, {})
            action_id = str(value.get("action_id"))
            handler = handlers.get(action_id)
            if handler is None:
                close_cand = hashlib.sha256((module_id + ":UIBuilder.close.<locals>.handler").encode()).hexdigest()[:24]
                if action_id == close_cand:
                    handler = self._default_close_handler
            if handler is None and self.runtime is not None and getattr(self.runtime, "modules", None) is not None:
                active = self.runtime.modules.get(module_id)
                if active is not None:
                    ctx_obj = getattr(active, "context", None)
                    ns = getattr(ctx_obj, "namespace", {}) if ctx_obj is not None else {}
                    if isinstance(ns, dict):
                        typed_ns = cast('dict[str, Any]', ns)
                        for item_name, item_fn in typed_ns.items():
                            name_str = str(item_name)
                            fn_obj = item_fn
                            if callable(fn_obj):
                                qname = str(getattr(fn_obj, "__qualname__", getattr(fn_obj, "__name__", name_str)))
                                cand1 = hashlib.sha256((module_id + ":" + qname).encode()).hexdigest()[:24]
                                cand2 = hashlib.sha256((module_id + ":" + name_str).encode()).hexdigest()[:24]
                                if action_id in (cand1, cand2):
                                    handler = fn_obj
                                    handlers[action_id] = fn_obj
                                    break
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
