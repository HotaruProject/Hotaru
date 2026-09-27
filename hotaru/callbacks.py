from __future__ import annotations

import base64
import hashlib
import inspect
import json
import secrets
import types
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from .runtime import Runtime

from goygram.errors import EntityBoundsInvalidError
from goygram import ext
from relay.firewall import module_scope
from relay.emoji import to_entities, to_rich
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
    def __init__(self, callback: Any, runtime: Any = None) -> None:
        self._callback = callback
        if runtime is not None:
            self._hotaru_runtime = runtime
        elif hasattr(callback, "_hotaru_runtime"):
            self._hotaru_runtime = getattr(callback, "_hotaru_runtime")
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

        if data.get('reply_markup') == {'_': 'replyInlineMarkup', 'rows': []}:
            data = {key: value for key, value in data.items() if key != 'reply_markup'}
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
        runtime = getattr(self, "_hotaru_runtime", getattr(self._callback, "_hotaru_runtime", None))
        form_id = getattr(self, "_hotaru_form_id", None)
        form = cast('Runtime', runtime).get_form(form_id) if runtime is not None and isinstance(form_id, str) else None
        if form is not None and runtime is not None:
            kwargs.pop("module_id", None)
            return await runtime.edit_form(form[0], text, **kwargs)

        raw_buttons = kwargs.get("buttons")
        if runtime is not None and self._has_input_buttons(raw_buttons):
            from hotaru.runtime import InputContext
            buttons = cast("list[Any]", kwargs.pop("buttons", []))
            kwargs.pop("kbd", None)
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
            plain, raw_ents = to_entities(text)
            ents = [e for e in raw_ents if int(e.get("length", 0)) > 0]
            if ents:
                data["entities"] = ents

            log["plain_len"] = len(plain or "")
            log["plain_u16"] = len((plain or "").encode("utf-16-le")) // 2
            log["ents"] = len(ents)
            log["entities"] = ents
            bot_app = getattr(getattr(runtime, "inline", None), "bot_app", None) if runtime is not None else None
            user_app = app or getattr(runtime, "app", None)
            if inline_mid is not None:
                target_app = bot_app or user_app
                log["bot"] = bool(getattr(target_app, "bot_token", None))
                inline_id: dict[str, Any] | None = cast('dict[str, Any]', inline_mid) if isinstance(inline_mid, dict) else {"_": "inputBotInlineMessageID", "raw": inline_mid} if isinstance(inline_mid, (str, bytes)) else None
                log["branch"] = "editInlineBotMessage"
                log["id_field"] = inline_id
                log["dc"] = inline_id.get("dc_id") if inline_id is not None else None
                _cb_log(log)
                if inline_id is None:
                    return None
                if use_rich:
                    data.pop("entities", None)
                    data["rich_message"] = {"_": "inputRichMessageHTML", **to_rich(text)}
                from relay.firewall import trusted_scope
                with trusted_scope():
                    return await self._edit_inline(target_app, inline_id, "" if use_rich else plain, data)
            log["branch"] = "editMessage"
            log["bot"] = False
            _cb_log(log)
            if isinstance(chat_id, int) and isinstance(msg_id, int) and user_app is not None:
                from relay.firewall import trusted_scope
                with trusted_scope():
                    return await user_app.mt_messages_edit_message(peer=chat_id, id=int(msg_id), message=plain, **data)
            return None
        if inline_mid is not None:
            bot_app = getattr(getattr(runtime, "inline", None), "bot_app", None) if runtime is not None else None
            target_app = bot_app or app
            if target_app is not None:
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
                plain, raw_ents = to_entities(text)
                ents = [e for e in raw_ents if int(e.get("length", 0)) > 0]
                if ents:
                    data["entities"] = ents

                bot_inline_id: dict[str, Any] = cast('dict[str, Any]', inline_mid) if isinstance(inline_mid, dict) else {"_": "inputBotInlineMessageID", "raw": inline_mid}
                log["branch"] = "editInlineBotMessage-bot"
                log["id_field"] = bot_inline_id
                _cb_log(log)
                if use_rich:
                    data.pop("entities", None)
                    data["rich_message"] = {"_": "inputRichMessageHTML", **to_rich(text)}
                from relay.firewall import trusted_scope
                with trusted_scope():
                    return await self._edit_inline(target_app, bot_inline_id, "" if use_rich else plain, data)
        if isinstance(chat_id, int) and isinstance(msg_id, int):
            user_app = app or getattr(runtime, "app", None)
            if user_app is not None:
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
                plain, raw_ents = to_entities(text)
                ents = [e for e in raw_ents if int(e.get("length", 0)) > 0]
                if ents:
                    data["entities"] = ents
                from relay.firewall import trusted_scope
                with trusted_scope():
                    return await user_app.mt_messages_edit_message(peer=chat_id, id=int(msg_id), message=plain, **data)
        log["branch"] = "fallback"
        _cb_log(log)
        return await self._callback.edit(text, **kwargs)

    async def delete(self) -> Any:
        try:
            await self.answer()
        except Exception:
            pass
        runtime = getattr(self, "_hotaru_runtime", None)
        form_id = getattr(self, "_hotaru_form_id", None)
        form = cast('Runtime', runtime).get_form(form_id) if runtime is not None and isinstance(form_id, str) else None
        if form is not None and runtime is not None:
            return await runtime.delete_form(form[0])
        user_app = getattr(runtime, "app", None) if runtime is not None else None
        app = getattr(self, "app", None) or user_app
        chat_id = getattr(self, "chat_id", None)
        msg_id = getattr(self, "msg_id", None)
        if not isinstance(msg_id, int):
            msg_id = None
        if isinstance(chat_id, int) and isinstance(msg_id, int) and app is not None:
            from relay.firewall import trusted_scope
            with trusted_scope():
                return await delete_chat_msg(app, chat_id, msg_id)
        inline_mid = getattr(self, "inline_message_id", None)
        if inline_mid is not None:
            if isinstance(inline_mid, dict):
                target = cast('dict[str, Any]', inline_mid)
                owner = target.get("owner_id")
                message = target.get("id")
                if target.get("_") == "inputBotInlineMessageID64" and type(owner) is int and -1000000000000 < owner < 0 and type(message) is int and 0 < message < 2147483648 and user_app is not None:
                    from relay.firewall import trusted_scope
                    with trusted_scope():
                        return await delete_chat_msg(user_app, owner - 1000000000000, message)
            raise ValueError("inline card has no resolvable deletion target")
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
    def __init__(
        self,
        *,
        max_items: int = 16384,
        secret: bytes | None = None,
        connection: Any = None,
        store: Any = None,
    ) -> None:
        if max_items < 1:
            raise ValueError("invalid callback limits")
        self.max_items = max_items
        self._key = secret or _derive_key(secrets.token_hex(16))
        self._connection = connection
        self._store = store
        self._items: dict[str, _Entry] = {}
        if self.connection is not None:
            self._init_db()

    @property
    def connection(self) -> Any:
        """The live connection: relocate() swaps it on the store, so a captured one would go stale."""
        return self._store.connection if self._store is not None else self._connection

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
        if handle is not None:
            if self._load(handle) is None or not isinstance(self._unseal(handle), bytes):
                raise CallbackDenied("callback is invalid")
            return handle
        self._prune()
        handle = self._seal()
        entry = _Entry(binding, value, consumed=False)
        if self.connection is not None:
            chat_str = str(binding.chat_id) if binding.chat_id is not None else None
            val_str = json.dumps(value, ensure_ascii=False, default=str)
            self.connection.execute(
                "INSERT INTO callback_store(handle, actor, chat_id, message_id, value, consumed) VALUES (?, ?, ?, ?, ?, 0)",
                (handle, str(binding.actor), chat_str, binding.message_id, val_str),
            )
            self.connection.commit()
        self._items[handle] = entry
        self._trim_cache()
        return handle

    def _load(self, handle: str) -> _Entry | None:
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
                self._trim_cache()
        return entry

    def peek(self, handle: str, binding: CallbackBinding) -> dict[str, Any]:
        entry = self._load(handle)
        decoded = self._unseal(handle)
        if entry is None or not isinstance(decoded, bytes):
            raise CallbackDenied("callback is invalid")
        if entry.consumed:
            raise CallbackDenied("callback has already been used")
        chat_ok = entry.binding.chat_id in (None, 0) or str(entry.binding.chat_id) == str(binding.chat_id)
        message_ok = entry.binding.message_id in (None, 0) or entry.binding.message_id == binding.message_id
        actor_ok = str(entry.binding.actor) == str(binding.actor)
        if not actor_ok or not chat_ok or not message_ok:
            raise CallbackDenied("callback is invalid")
        return entry.value

    def consume(self, handle: str, binding: CallbackBinding) -> dict[str, Any]:
        value = self.peek(handle, binding)
        entry = self._items[handle]
        if self.connection is not None:
            cursor = self.connection.execute("UPDATE callback_store SET consumed = 1 WHERE handle = ? AND consumed = 0", (handle,))
            self.connection.commit()
            if cursor.rowcount != 1:
                raise CallbackDenied("callback has already been used")
        entry.consumed = True
        return value

    def unconsume(self, handle: str) -> None:
        entry = self._items.get(handle)
        if entry is not None:
            entry.consumed = False
        if self.connection is not None:
            self.connection.execute("UPDATE callback_store SET consumed = 0 WHERE handle = ?", (handle,))
            self.connection.commit()

    def rebind(self, handle: str, binding: CallbackBinding, *, scope: dict[str, Any] | None = None) -> str:
        entry = self._load(handle)
        if entry is None:
            raise CallbackDenied("callback is invalid")
        value = dict(self.consume(handle, entry.binding))
        if scope is not None:
            value.update(scope)
        return self.issue(binding, value)

    def _prune(self) -> None:
        stale = [key for key, entry in self._items.items() if entry.consumed]
        for key in stale:
            self._items.pop(key, None)
        if self.connection is not None:
            self.connection.execute("DELETE FROM callback_store WHERE consumed = 1")
            self.connection.commit()

    def _trim_cache(self) -> None:
        while self.connection is not None and len(self._items) > self.max_items:
            self._items.pop(next(iter(self._items)))


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

    @property
    def default_close_handler(self) -> Any:
        return self._default_close_handler

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
        value = self.store.peek(data, binding)
        form = None
        if self.runtime is not None:
            from types import SimpleNamespace
            access = getattr(self.runtime, "access", None)
            module_id = value.get("module")
            form_id = value.get("form_id")
            form = cast('Runtime', self.runtime).get_form(form_id) if isinstance(form_id, str) else None
            if form_id and form is None:
                raise CallbackDenied("form is no longer active")
            if form is not None:
                source = form[1]
                expected = getattr(source, "inline_message_id", None)
                actual = getattr(callback, "inline_message_id", None)
                if actual is None:
                    actual = getattr(callback, "msg_id", None)
                if isinstance(expected, dict) and isinstance(actual, dict):
                    expected = {key: cast('dict[str, object]', expected).get(key) for key in ("dc_id", "id", "owner_id")}
                    actual = {key: cast('dict[str, object]', actual).get(key) for key in ("dc_id", "id", "owner_id")}
                if expected is not None and actual != expected:
                    raise CallbackDenied("callback belongs to another form")
                if expected is None and (str(binding.chat_id) != str(source.chat_id) or binding.message_id != source.id):
                    raise CallbackDenied("callback belongs to another message")
                callback._hotaru_form_id = form_id
            if access is None:
                raise CallbackDenied("access is unavailable")
            if not access.is_owner(binding.actor):
                command = value.get("command")
                spec = self.runtime.kernel.registry.resolve_name(command) if isinstance(command, str) else None
                if spec is None or spec.kernel or spec.module_id != module_id or form is None:
                    raise CallbackDenied("callback requires current command access")
                event = SimpleNamespace(from_id=binding.actor, chat_id=form[1].chat_id)
                if not self.runtime.kernel.is_authorized(event, spec):
                    raise CallbackDenied("command access was revoked")
        security = getattr(self.runtime, "security", None)
        if security is not None:
            from .security import AccessVerdict
            if security.check(callback, transport="mt", module_id=value.get("module"), authorized=True) is not AccessVerdict.ALLOW:
                raise CallbackDenied("callback rate limit reached")
        value = self.store.consume(data, binding)
        handler = None
        if isinstance(value.get("module"), str):
            module_id = value["module"]
            handlers = self._module_handlers.get(module_id, {})
            action_id = str(value.get("action_id"))
            handler = handlers.get(action_id)
            if handler is None:
                close_cands = {
                    "close",
                    "ui_close",
                    hashlib.sha256((module_id + ":close").encode()).hexdigest()[:24],
                    hashlib.sha256((module_id + ":UiHelper.close.<locals>.handler").encode()).hexdigest()[:24],
                    hashlib.sha256((module_id + ":UIBuilder.close.<locals>.handler").encode()).hexdigest()[:24],
                    hashlib.sha256((module_id + ":_UiProxy.close.<locals>.handler").encode()).hexdigest()[:24],
                    hashlib.sha256(b"_show_main.<locals>.close").hexdigest()[:16],
                    hashlib.sha256((module_id + ":_show_main.<locals>.close").encode()).hexdigest()[:24],
                }
                if action_id in close_cands or action_id.startswith("close_") or action_id.endswith("_close"):
                    handler = self._default_close_handler
            if handler is None and self.runtime is not None:
                sandbox = getattr(self.runtime, "sandbox", None)
                if sandbox is not None and (
                    (hasattr(sandbox, "has_module") and sandbox.has_module(module_id))
                    or module_id in getattr(sandbox, "_workers", {})
                    or (hasattr(self.runtime, "_is_kernel_module") and not self.runtime._is_kernel_module(module_id))
                ):
                    handler = sandbox._make_sandbox_cb(module_id, action_id)
                    handlers[action_id] = handler
            if handler is None and self.runtime is not None and getattr(self.runtime, "modules", None) is not None:
                active = self.runtime.modules.get(module_id)
                if active is not None:
                    ns = getattr(active, "namespace", None)
                    if not isinstance(ns, dict):
                        ctx_obj = getattr(active, "context", None)
                        ns = getattr(ctx_obj, "namespace", {}) if ctx_obj is not None else {}
                    if isinstance(ns, dict):
                        typed_ns = cast('dict[str, Any]', ns)
                        for item_name, item_fn in typed_ns.items():
                            name_str = str(item_name)
                            if callable(item_fn):
                                qname = str(getattr(item_fn, "__qualname__", getattr(item_fn, "__name__", name_str)))
                                cand1 = hashlib.sha256((module_id + ":" + qname).encode()).hexdigest()[:24]
                                cand2 = hashlib.sha256((module_id + ":" + name_str).encode()).hexdigest()[:24]
                                cand3 = hashlib.sha256((module_id + ":" + getattr(item_fn, "__name__", "")).encode()).hexdigest()[:24]
                                if action_id in (cand1, cand2, cand3):
                                    handler = item_fn
                                    handlers[action_id] = item_fn
                                    break
                        if handler is None:
                            saved_nonlocals: dict[str, Any] = {}
                            if form is not None:
                                form_opts = form[4]
                                actions_list = cast('list[object]', form_opts.get("actions", [])) if isinstance(form_opts.get("actions"), list) else []
                                for act in actions_list:
                                    if isinstance(act, dict):
                                        act_dict = cast('dict[str, object]', act)
                                        if str(act_dict.get("action_id", "")) == action_id:
                                            nl = act_dict.get("nonlocals")
                                            if isinstance(nl, dict):
                                                saved_nonlocals.update(cast('dict[str, object]', nl))
                                            break
                            found_code = None
                            def _search_codes(code: Any) -> Any:
                                for c in getattr(code, "co_consts", ()):
                                    if isinstance(c, types.CodeType):
                                        qn = getattr(c, "co_qualname", getattr(c, "co_name", ""))
                                        cands = (
                                            hashlib.sha256((module_id + ":" + qn).encode()).hexdigest()[:24],
                                            hashlib.sha256((module_id + ":" + c.co_name).encode()).hexdigest()[:24],
                                        )
                                        if action_id in cands:
                                            return c
                                        sub = _search_codes(c)
                                        if sub is not None:
                                            return sub
                                return None

                            for item_fn in typed_ns.values():
                                if callable(item_fn) and hasattr(item_fn, "__code__"):
                                    found_code = _search_codes(item_fn.__code__)
                                    if found_code is not None:
                                        break

                            if found_code is not None:
                                context_factory = getattr(self.runtime, "context_factory", None)
                                mod_ctx = context_factory.create(module_id, callback) if context_factory is not None else None
                                def make_cell(val: Any) -> Any:
                                    return (lambda: val).__closure__[0]  # type: ignore
                                cells: list[Any] = []
                                payload_val = value.get("payload")
                                for var in getattr(found_code, "co_freevars", ()):
                                    if var == "ctx":
                                        cells.append(make_cell(mod_ctx))
                                    elif var == "runtime":
                                        cells.append(make_cell(self.runtime))
                                    elif var in saved_nonlocals:
                                        cells.append(make_cell(saved_nonlocals[var]))
                                    elif isinstance(payload_val, dict) and var in payload_val:
                                        cells.append(make_cell(payload_val[var]))
                                    elif mod_ctx is not None and hasattr(mod_ctx, var):
                                        cells.append(make_cell(getattr(mod_ctx, var)))
                                    elif hasattr(active, var):
                                        cells.append(make_cell(getattr(active, var)))
                                    else:
                                        cells.append(make_cell(None))
                                try:
                                    recreated = types.FunctionType(found_code, typed_ns, found_code.co_name, None, tuple(cells))
                                    handler = recreated
                                    handlers[action_id] = recreated
                                except Exception:
                                    pass
        else:
            action = value.get("action")
            handler = self._handlers.get(action) if isinstance(action, str) else None
        if handler is None:
            self.store.unconsume(data)
            raise CallbackDenied("callback action is unavailable")
        with module_scope(str(value.get("module") or "")):
            result = handler(CallbackContext(callback, self.runtime), value.get("payload"))
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
