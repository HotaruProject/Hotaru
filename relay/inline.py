from __future__ import annotations

import asyncio
import logging
import random
import re
import secrets
import string
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

from goygram import Session
from goygram.errors import UnauthorizedError
from relay.firewall import trusted_scope
from relay.jobs import route_bot_api

log = logging.getLogger(__name__)

BOTFATHER = "@BotFather"
BOTFATHER_ID = 93372553
TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{35}")
USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,31}bot$", re.IGNORECASE)
WARM_DEADLINE_SECONDS = 120.0


_ROOTS = ("nova", "luna", "aero", "flux", "zeno", "pulse", "echo", "spark", "sol", "iris", "drift", "blaze", "star", "glow", "prism", "cyber", "silk", "mist", "frost", "hyper", "neon", "nexus", "orbit", "quantum", "stellar", "umbra", "astral", "zenith")
_STEMS = ("ray", "flow", "dash", "link", "arc", "wave", "sync", "byte", "mesh", "gate", "run", "sky", "core", "node", "shift", "beam")


class InlineError(RuntimeError):
    pass


def _is_readonly_peer(value: Any) -> bool:
    from relay.denylist import is_readonly_peer

    return is_readonly_peer(value)


def _result_body(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    payload = cast('dict[str, Any]', value)
    result = payload.get("result")
    return cast('dict[str, Any]', result) if isinstance(result, dict) else payload


@dataclass
class InlineBotInfo:
    token: str
    username: str
    bot_id: int


def _token_in(message: dict[str, Any]) -> str | None:
    import json

    parts = message.get("parts")
    for part in cast('list[Any]', parts) if isinstance(parts, list) else [message]:
        if not isinstance(part, dict):
            continue
        item = cast('dict[str, Any]', part)
        texts = [str(item.get("message") or "")]
        entities = item.get("entities")
        for entity in cast('list[Any]', entities) if isinstance(entities, list) else []:
            if isinstance(entity, dict) and isinstance(cast('dict[str, Any]', entity).get("url"), str):
                texts.append(cast('dict[str, Any]', entity)["url"])
        markup = item.get("reply_markup")
        rows = cast('dict[str, Any]', markup).get("rows") if isinstance(markup, dict) else None
        for row in cast('list[Any]', rows) if isinstance(rows, list) else []:
            buttons = cast('dict[str, Any]', row).get("buttons") if isinstance(row, dict) else None
            for button in cast('list[Any]', buttons) if isinstance(buttons, list) else []:
                texts.append(json.dumps(button, default=str))
        for text in texts:
            match = TOKEN_RE.search(text)
            if match is not None:
                return match.group(0)
    return None


def _merge(messages: list[dict[str, Any]]) -> dict[str, Any]:
    merged = dict(messages[-1])
    merged["message"] = "\n".join(str(item.get("message") or "") for item in messages)
    merged["reply_markup"] = next((item.get("reply_markup") for item in reversed(messages) if item.get("reply_markup")), None)
    merged["parts"] = messages
    return merged


class BotFatherConversation:
    def __init__(self, app: Any, *, timeout: float = 30.0) -> None:
        self.app = app
        self.timeout = timeout
        self._peer: Any = None
        self._last_id: int = 0
        self._self_id = getattr(getattr(app, "mt", None), "self_id", None) or getattr(app, "self_id", None) or 0
        self._tracked_ids: set[int] = set()

    async def __aenter__(self) -> "BotFatherConversation":
        self._peer = await self.app.mt.resolve_peer(BOTFATHER)
        state = await self.app.mt_messages_get_history(
            peer=self._peer,
            offset_id=0,
            offset_date=0,
            add_offset=0,
            limit=1,
            max_id=0,
            min_id=0,
            hash=0,
        )
        body = _result_body(state)
        messages = body.get("messages")
        if messages:
            self._last_id = max((cast('dict[str, Any]', m).get("id", 0) for m in cast('list[Any]', messages) if isinstance(m, dict)), default=0)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._tracked_ids:
            await self._delete(*self._tracked_ids)
            self._tracked_ids.clear()
        return None

    async def say(self, text: str) -> int:
        result = await self.app.mt_messages_send_message(
            peer=self._peer,
            message=text,
            random_id=secrets.randbits(63),
        )
        sent_id = self._extract_sent_id(result)
        if sent_id:
            self._tracked_ids.add(sent_id)
        return sent_id

    def _extract_sent_id(self, result: Any) -> int:
        body = _result_body(result)
        if body:
            updates = body.get("updates")
            if isinstance(updates, list):
                for update in cast('list[Any]', updates):
                    if not isinstance(update, dict):
                        continue
                    update_dict = cast('dict[str, Any]', update)
                    kind = update_dict.get("_")
                    if kind in ("updateNewMessage", "updateMessageID"):
                        message = update_dict.get("message")
                        if isinstance(message, dict):
                            message_data = cast('dict[str, Any]', message)
                            if isinstance(message_data.get("id"), int):
                                return cast(int, message_data["id"])
                    if kind == "updateMessageID" and isinstance(update_dict.get("id"), int):
                        return cast(int, update_dict["id"])
        return 0

    async def _delete(self, *ids: int) -> None:
        valid = [i for i in ids if i > 0]
        if not valid:
            return
        try:
            await self.app.mt_messages_delete_messages( id=valid, revoke=True)
        except Exception:
            pass

    def _is_mine(self, message: dict[str, Any]) -> bool:
        if message.get("out"):
            return True
        sender = message.get("from_id")
        if isinstance(sender, int):
            return sender == self._self_id
        if isinstance(sender, dict):
            user_id = cast('dict[str, Any]', sender).get("user_id")
            return isinstance(user_id, int) and user_id == self._self_id
        return False

    async def response(self, *, since: int | None = None, settle: float = 1.5) -> dict[str, Any]:
        floor = since if since is not None else self._last_id
        deadline = time.monotonic() + self.timeout
        found: dict[int, dict[str, Any]] = {}
        quiet_at: float | None = None
        while time.monotonic() < deadline:
            result = await self.app.mt_messages_get_history(
                peer=self._peer,
                offset_id=0,
                offset_date=0,
                add_offset=0,
                limit=10,
                max_id=0,
                min_id=0,
                hash=0,
            )
            body = _result_body(result)
            messages = body.get("messages")
            for message in cast('list[Any]', messages) if isinstance(messages, list) else []:
                if not isinstance(message, dict):
                    continue
                message_dict = cast('dict[str, Any]', message)
                message_id = message_dict.get("id", 0)
                if not isinstance(message_id, int) or message_id <= floor:
                    continue
                self._last_id = max(self._last_id, message_id)
                if self._is_mine(message_dict) or message_id in found:
                    continue
                found[message_id] = message_dict
                self._tracked_ids.add(message_id)
                quiet_at = time.monotonic() + settle
            if found and quiet_at is not None and time.monotonic() >= quiet_at:
                break
            await asyncio.sleep(0.5)
        if not found:
            raise InlineError("BotFather response timeout")
        return _merge([found[key] for key in sorted(found)])

    async def drain(self) -> None:
        for _ in range(4):
            result = await self.app.mt_messages_get_history(
                peer=self._peer,
                offset_id=0,
                offset_date=0,
                add_offset=0,
                limit=10,
                max_id=0,
                min_id=0,
                hash=0,
            )
            body = _result_body(result)
            messages = body.get("messages")
            recent: list[dict[str, Any]] = []
            for item in cast('list[Any]', messages) if isinstance(messages, list) else []:
                if isinstance(item, dict):
                    message = cast('dict[str, Any]', item)
                    if isinstance(message.get("id"), int) and message["id"] > self._last_id:
                        recent.append(message)
            if not recent:
                return
            for message in recent:
                self._last_id = max(self._last_id, message.get("id", 0))
            await self._delete(*(m.get("id", 0) for m in recent))
            await asyncio.sleep(0.5)

    async def ask(self, text: str) -> dict[str, Any]:
        await self.drain()
        mine_id = await self.say(text)
        try:
            reply = await self.response()
        except InlineError:
            await self._delete(mine_id)
            self._tracked_ids.discard(mine_id)
            raise
        parts = reply.get("parts")
        ids = [cast('dict[str, Any]', item).get("id", 0) for item in cast('list[Any]', parts)] if isinstance(parts, list) else [reply.get("id", 0)]
        await self._delete(mine_id, *ids)
        self._tracked_ids.discard(mine_id)
        for i in ids:
            self._tracked_ids.discard(i)
        return reply

    async def click_button(self, reply: dict[str, Any], *needles: str) -> dict[str, Any] | None:
        markup: Any = reply.get("reply_markup")
        if not isinstance(markup, dict):
            return None
        markup_dict = cast('dict[str, Any]', markup)
        raw_rows = markup_dict.get("rows")
        if not isinstance(raw_rows, list):
            return None
        rows = cast('list[Any]', raw_rows)
        floor = reply.get("id", self._last_id)
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_dict = cast('dict[str, Any]', row)
            raw_buttons = row_dict.get("buttons")
            if not isinstance(raw_buttons, list):
                continue
            buttons = cast('list[Any]', raw_buttons)
            for btn in buttons:
                if not isinstance(btn, dict):
                    continue
                btn_dict = cast('dict[str, Any]', btn)
                btn_text = str(btn_dict.get("text") or "")
                data = btn_dict.get("data")
                matches = not needles or any(n.casefold() in btn_text.casefold() for n in needles)
                if matches and isinstance(data, (bytes, bytearray)):
                    try:
                        await self.app.mt_messages_get_bot_callback_answer(
                            peer=self._peer,
                            msg_id=reply["id"],
                            data=bytes(data),
                        )
                        return await self.response(since=floor)
                    except Exception:
                        pass
        return None

class BotFatherGuard:
    def __init__(self, app: Any) -> None:
        self.app = app
        self._peer: bytes | None = None
        self._was_archived: bool | None = None
        self._was_muted: bool | None = None

    async def _resolve(self) -> bytes:
        if self._peer is None:
            peer = await self.app.mt.resolve_peer(BOTFATHER)
            if not isinstance(peer, (bytes, bytearray)):
                raise RuntimeError("botfather peer is missing")
            self._peer = bytes(peer)
        return self._peer

    async def _dialog(self) -> dict[str, Any] | None:
        peer = await self._resolve()
        result = await self.app.mt_messages_get_peer_dialogs(
            peers=[{"_": "inputDialogPeer", "peer": peer}],
        )
        body = _result_body(result)
        dialogs = body.get("dialogs")
        for dialog in cast('list[Any]', dialogs) if isinstance(dialogs, list) else []:
            if isinstance(dialog, dict):
                return cast('dict[str, Any]', dialog)
        return None

    async def read_state(self) -> dict[str, Any]:
        dialog = await self._dialog()
        if dialog is None:
            return {"archived": False, "muted": False}
        settings_raw = dialog.get("notify_settings")
        settings = cast('dict[str, Any]', settings_raw) if isinstance(settings_raw, dict) else {}
        mute_until = settings.get("mute_until") or 0
        return {
            "archived": isinstance(dialog.get("folder_id"), int) and dialog["folder_id"] != 0,
            "muted": isinstance(mute_until, int) and mute_until > int(time.time()),
        }

    async def _folder_peer(self, folder_id: int) -> dict[str, Any]:
        await self._resolve()
        entity: Any = self.app.mt.entity_usernames.get("botfather") or self.app.mt.entities.get(("user", BOTFATHER_ID)) or {}
        access_hash = cast('dict[str, Any]', entity).get("access_hash") if isinstance(entity, dict) else 0
        return {
            "_": "inputFolderPeer",
            "peer": {"_": "inputPeerUser", "user_id": BOTFATHER_ID, "access_hash": int(access_hash or 0)},
            "folder_id": folder_id,
        }

    async def set_archived(self, archived: bool) -> None:
        await self.app.mt_folders_edit_peer_folders(
            folder_peers=[await self._folder_peer(1 if archived else 0)],
        )

    async def set_muted(self, muted: bool) -> None:
        peer = await self._resolve()
        await self.app.mt_account_update_notify_settings(
            peer={"_": "inputNotifyPeer", "peer": peer},
            settings={"_": "inputPeerNotifySettings", "mute_until": 2147483647 if muted else 0},
        )

    async def __aenter__(self) -> "BotFatherGuard":
        try:
            state = await self.read_state()
            self._was_archived = state["archived"]
            self._was_muted = state["muted"]
            if not state["archived"]:
                await self.set_archived(True)
            if not state["muted"]:
                await self.set_muted(True)
        except Exception:
            self._was_archived = None
            self._was_muted = None
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            if self._was_archived is False:
                await self.set_archived(False)
            if self._was_muted is False:
                await self.set_muted(False)
        except Exception:
            pass


class InlineManager:
    def __init__(
        self,
        runtime: Any,
        *,
        bot_name: str = "Hotaru userbot",
        inline_placeholder: str = "hotaru:~$",
        poll_timeout: int = 25,
    ) -> None:
        self.runtime = runtime
        self.bot_name = bot_name
        self.inline_placeholder = inline_placeholder
        self.poll_timeout = poll_timeout
        self.bot_app: Any = None
        self.info: InlineBotInfo | None = None
        self._task: asyncio.Task[None] | None = None
        self._handlers: list[Callable[[Any], Awaitable[Any]]] = []
        self._cb_handlers: list[Callable[[Any], Awaitable[Any]]] = []
        self._pm_handlers: list[Callable[[Any], Awaitable[Any]]] = []
        self._chosen_handlers: list[Callable[[Any], Awaitable[Any]]] = []
        self._chosen_pending: set[str] = set()
        self._stop = asyncio.Event()
        self.ready = asyncio.Event()
        self._create_attempts: list[float] = []
        self._provision_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()

    def on_inline(self, handler: Callable[[Any], Awaitable[Any]]) -> Callable[[Any], Awaitable[Any]]:
        self._handlers.append(handler)
        return handler

    def on_callback(self, handler: Callable[[Any], Awaitable[Any]]) -> Callable[[Any], Awaitable[Any]]:
        self._cb_handlers.append(handler)
        return handler

    def on_bot_pm(self, handler: Callable[[Any], Awaitable[Any]]) -> Callable[[Any], Awaitable[Any]]:
        self._pm_handlers.append(handler)
        return handler

    def on_chosen(self, handler: Callable[[Any], Awaitable[Any]]) -> Callable[[Any], Awaitable[Any]]:
        self._chosen_handlers.append(handler)
        return handler

    def _owner_id(self) -> int | None:
        session = getattr(self.runtime.app, "session", None)
        uid = getattr(session, "self_id", None)
        if isinstance(uid, int) and uid > 0:
            return uid
        kernel = getattr(self.runtime, "kernel", None)
        oid = getattr(kernel, "owner_id", None)
        if isinstance(oid, int) and oid > 0:
            return oid
        cfg = getattr(self.runtime.config, "owner_id", None)
        return cfg if isinstance(cfg, int) and cfg > 0 else None

    def _persist(self, info: InlineBotInfo) -> None:
        state = self.runtime.state
        if state is None:
            self.info = info
            return
        state.set_setting("inline-bot-token", info.token)
        state.set_setting("inline-bot-username", info.username)
        state.set_setting("inline-bot-id", info.bot_id)
        owner = self._owner_id()
        if owner is not None and state.get_setting("owner-id") is None:
            state.set_setting("owner-id", owner)
        self.info = info

    async def ensure_bot(self, *, allow_create: bool = True) -> InlineBotInfo:
        state = self.runtime.state
        if state is None:
            raise InlineError("state store is not ready")
        owner = self._owner_id()
        if owner is not None and state.get_setting("owner-id") is None:
            state.set_setting("owner-id", owner)
        async with self._provision_lock:
            token = state.get_setting("inline-bot-token")
            stored_id = state.get_setting("inline-bot-id")
            info = None
            recovered = False
            if token:
                try:
                    info = await self.getbot(str(token))
                    if stored_id is not None and info.bot_id != int(stored_id):
                        info = None
                        recovered = True
                except InlineError:
                    info = None
                    recovered = True
                    state.set_setting("inline-bot-token", None)
            if info is None:
                found = await self._find_existing_bot()
                if found is not None:
                    info = found
                    recovered = True
            if info is None and allow_create:
                self._create_gate()
                info = await self._create_bot()
            if info is None:
                raise InlineError("no inline bot available: nothing stored, nothing found, creation disabled")
            self._persist(info)
        if recovered:
            try:
                await asyncio.wait_for(self._tune_bot(info), timeout=10.0)
            except Exception:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "tune_failed", username=info.username)
            try:
                async with BotFatherGuard(self.runtime.app), BotFatherConversation(self.runtime.app) as conv:
                    await self._configure(conv, info.username)
            except Exception:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "configure_failed", username=info.username)
        asyncio.create_task(self._tune_bot(info), name="hotaru:tune-bot")
        if hasattr(self.runtime, "sync_web_app"):
            asyncio.create_task(self.runtime.sync_web_app(clean_bot=info.username), name="hotaru:sync-webapp")
        return info

    async def _tune_bot(self, info: InlineBotInfo) -> None:
        from goygram import GoyGram

        proxy = self.runtime.config.tg_proxy
        app = GoyGram(bot_token=info.token, default_transport="api", proxy=proxy)
        route_bot_api(app, proxy)
        try:
            await app.get_me()
            await self._inspect_webhook(app)
            cmd_payload = {
                "commands": [
                    {"command": "start", "description": "Hotaru Userbot"},
                    {"command": "github", "description": "GitHub Repository"},
                ],
                "scope": {"type": "all_private_chats"},
            }
            default_cmd_payload = {
                "commands": [
                    {"command": "start", "description": "Hotaru Userbot"},
                    {"command": "github", "description": "GitHub Repository"},
                ],
            }
            for method, payload in (
                ("deleteWebhook", {"drop_pending_updates": True}),
                ("setMyName", {"name": "Hotaru Userbot"}),
                ("setMyShortDescription", {"short_description": "Hotaru Userbot - security-first modular userbot"}),
                ("setMyDescription", {"description": "Hotaru Userbot - security-first modular Telegram userbot with capability sandboxing."}),
                ("setMyCommands", cmd_payload),
                ("setMyCommands", default_cmd_payload),
            ):
                try:
                    await getattr(app, "bot_req")(method, **payload)
                except Exception:
                    continue
            await self._apply_bot_avatar(info)
        except Exception:
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("inline", "tune_failed", username=info.username)
        finally:
            try:
                await app.close()
            except Exception:
                pass

    async def _apply_bot_avatar(self, info: InlineBotInfo) -> None:
        state = getattr(self.runtime, "state", None)
        app = getattr(self.runtime, "app", None)
        if app is None or getattr(app, "mt", None) is None:
            return
        root = Path(__file__).resolve().parent.parent
        candidates: list[Path] = [root / "hotaru/assets/logo.png", root / "hotaru/assets/avatar.png"]
        logo_path: Path | None = next((p for p in candidates if p.is_file()), None)
        if logo_path is None:
            return
        import hashlib
        digest = hashlib.sha256(logo_path.read_bytes()).hexdigest()
        key = f"brand-avatar-bot-{info.bot_id}"
        if state is not None and state.get_setting(key) == digest:
            return
        uploaded_file = None
        from relay.files import document, put
        try:
            with trusted_scope():
                bot = None
                if hasattr(app, "_typed_peer"):
                    try:
                        bot = await app._typed_peer("@" + info.username, "user")
                    except Exception:
                        bot = None
                if bot is None:
                    resolve_un = getattr(app, "mt_contacts_resolve_username", None)
                    if callable(resolve_un):
                        try:
                            resolved = await cast(Callable[..., Awaitable[Any]], resolve_un)(username=info.username.lstrip("@"))
                            resolved_dict = cast('dict[str, Any]', resolved) if isinstance(resolved, dict) else None
                            body_val: Any = resolved_dict.get("result") if resolved_dict is not None and "result" in resolved_dict else resolved_dict
                            body_dict: dict[str, Any] | None = cast('dict[str, Any]', body_val) if isinstance(body_val, dict) else None
                            users = cast('list[Any]', body_dict.get("users", [])) if body_dict is not None else []
                            if users and isinstance(users[0], dict):
                                u_dict = cast('dict[str, Any]', users[0])
                                bot = {"_": "inputUser", "user_id": int(cast(int | str, u_dict.get("id", info.bot_id))), "access_hash": int(cast(int | str, u_dict.get("access_hash", 0)))}
                        except Exception:
                            bot = None
                if bot is None:
                    from goygram import ext
                    peer: Any = await app.mt.resolve_peer("@" + info.username)
                    if isinstance(peer, (bytes, bytearray, memoryview)):
                        peer_dict: dict[str, Any] = cast('dict[str, Any]', ext.deserialize_constructor(bytes(cast(Any, peer))))
                    elif isinstance(peer, dict):
                        peer_dict = cast('dict[str, Any]', peer)
                    elif hasattr(peer, "to_dict"):
                        peer_dict = cast('dict[str, Any]', getattr(peer, "to_dict")())
                    else:
                        peer_dict = {}
                    access_val: Any = peer_dict.get("access_hash")
                    access_hash: int = int(access_val) if access_val else 0
                    bot = {"_": "inputUser", "user_id": int(info.bot_id), "access_hash": access_hash}
                up = await put(app, str(logo_path), file_name=str(logo_path.name))
                uploaded_file = document(up)["file"]
                await app.mt_photos_upload_profile_photo(bot=bot, file=uploaded_file)
            if state is not None:
                state.set_setting(key, digest)
            return
        except Exception as exc:
            bf_ok = False
            try:
                with trusted_scope():
                    if uploaded_file is None:
                        up = await put(app, str(logo_path), file_name=str(logo_path.name))
                        uploaded_file = document(up)["file"]
                    async with BotFatherGuard(app), BotFatherConversation(app) as conv:
                        try:
                            await conv.say("/cancel")
                            await conv.response()
                        except Exception:
                            pass
                        await conv.say("/setuserpic")
                        await conv.response()
                        await conv.say("@" + info.username.lstrip("@"))
                        await conv.response()
                        peer = await app.mt.resolve_peer(BOTFATHER)
                        await app.mt_messages_send_media(
                            peer=peer,
                            media={"_": "inputMediaUploadedPhoto", "file": uploaded_file},
                            message="",
                            random_id=secrets.randbits(63),
                        )
                        await conv.response()
                        bf_ok = True
                if bf_ok and state is not None:
                    state.set_setting(key, digest)
                return
            except Exception:
                pass
            if not bf_ok and self.runtime.observatory is not None:
                self.runtime.observatory.emit("inline", "avatar_failed", error=type(exc).__name__, detail=str(exc)[:140])

    async def _start_bot_chat(self, username: str, *, force: bool = False) -> bool:
        app = self.runtime.app
        if app is None or app.mt is None:
            return False
        if self._chat_hold() > time.time():
            return False
        state = self.runtime.state
        if not force and state is not None and state.get_setting("inline-chat-ok") is True:
            return True
        peer = None
        try:
            with trusted_scope():
                peer = await app.mt.resolve_peer("@" + username)
                await app.mt_messages_send_message(
                    peer=peer,
                    message="/start",
                    random_id=secrets.randbits(63),
                )
        except Exception as exc:
            if "YOU_BLOCKED_USER" in str(exc).upper() and peer is not None:
                try:
                    with trusted_scope():
                        await app.mt_contacts_unblock(id=peer)
                        await app.mt_messages_send_message(peer=peer, message="/start", random_id=secrets.randbits(63))
                    self._mark_chat_started()
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "chat_unblocked", username=username)
                    return True
                except Exception as retry_exc:
                    exc = retry_exc
            wait = getattr(exc, "seconds", None)
            if isinstance(wait, int) and wait > 0:
                self._mark_chat_hold(float(wait))
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("inline", "start_chat_skipped", error=type(exc).__name__, detail=str(exc)[:160])
            return False
        self._mark_chat_started()
        return True

    def _chat_hold(self) -> float:
        state = self.runtime.state
        if state is None:
            return 0.0
        hold = state.get_setting("inline-chat-hold-until")
        return float(hold) if isinstance(hold, (int, float)) else 0.0

    def _mark_chat_hold(self, wait: float) -> None:
        state = self.runtime.state
        if state is not None:
            state.set_setting("inline-chat-hold-until", time.time() + wait)

    def _mark_chat_started(self) -> None:
        state = self.runtime.state
        if state is not None:
            state.set_setting("inline-chat-ok", True)
            state.set_setting("inline-chat-hold-until", 0)

    async def reopen_owner_chat(self, chat_id: int | str | None) -> bool:
        if not isinstance(chat_id, int) or self.info is None:
            return False
        try:
            return await self._start_bot_chat(self.info.username, force=True)
        except Exception:
            return False

    def _create_gate(self, *, max_attempts: int = 3, window: float = 3600.0, cooldown: float = 900.0) -> None:
        now = time.monotonic()
        self._create_attempts = [t for t in self._create_attempts if now - t < window]
        if len(self._create_attempts) >= max_attempts:
            last = self._create_attempts[-1]
            waited = now - last
            if waited < cooldown:
                raise InlineError(
                    f"provisioning cooldown: {max_attempts} creations in the last hour, retry in {int(cooldown - waited)}s"
                )
        self._create_attempts.append(now)

    async def getbot(self, token: str) -> InlineBotInfo:
        from goygram import GoyGram

        proxy = self.runtime.config.tg_proxy
        app = GoyGram(bot_token=token, default_transport="api", proxy=proxy)
        route_bot_api(app, proxy)
        try:
            me = await app.get_me()
            if not isinstance(me, dict):
                raise InlineError("inline bot token validation failed")
            me_dict = me
            botid = me_dict.get("id")
            username = me_dict.get("username")
            if not isinstance(botid, int) or botid <= 0 or not isinstance(username, str) or not username or me_dict.get("is_bot") is not True:
                raise InlineError("inline bot identity is incomplete")
            username = username.lstrip("@")
            state = self.runtime.state
            if state is not None:
                state.set_setting("inline-bot-token", token)
                state.set_setting("inline-bot-username", username)
                state.set_setting("inline-bot-id", botid)
            return InlineBotInfo(token, username, botid)
        except InlineError:
            raise
        except Exception:
            raise InlineError("inline bot token validation failed") from None
        finally:
            await app.close()

    def _forget_bot(self) -> None:
        state = self.runtime.state
        if state is None:
            return
        state.set_setting("inline-bot-token", None)
        self.info = None

    async def _find_existing_bot(self) -> InlineBotInfo | None:
        state = self.runtime.state
        wanted = None
        if state is not None:
            wanted = state.get_setting("inline-bot-username") or state.get_setting("inline-bot-username-wanted")
        if isinstance(wanted, str) and wanted.strip():
            wanted = wanted.strip().lstrip("@")
            if not wanted.lower().endswith("bot"):
                wanted += "_bot"
        else:
            wanted = None
        candidates: list[tuple[str, str]] = []
        try:
            async with BotFatherGuard(self.runtime.app), BotFatherConversation(self.runtime.app) as conv:
                response = await conv.ask("/mybots")
                markup = response.get("reply_markup")
                rows = cast('dict[str, Any]', markup).get("rows") if isinstance(markup, dict) else None
                if not isinstance(rows, list) or not rows:
                    return None
                for row in cast('list[Any]', rows):
                    if not isinstance(row, dict):
                        continue
                    buttons = cast('dict[str, Any]', row).get("buttons")
                    for button in cast('list[Any]', buttons) if isinstance(buttons, list) else []:
                        if not isinstance(button, dict):
                            continue
                        text = cast('dict[str, Any]', button).get("text", "")
                        if not isinstance(text, str):
                            continue
                        candidate = text.lstrip("@")
                        if not USERNAME_RE.match(candidate):
                            continue
                        if wanted and candidate.casefold() == str(wanted).casefold():
                            candidates.append((text, candidate))
                        elif not wanted and candidate.casefold().startswith("hotaru_"):
                            candidates.append((text, candidate))
                for button_text, candidate in candidates:
                    token = await self._fetch_token(conv, button_text)
                    if token is None:
                        continue
                    bot_id = int(token.split(":", 1)[0])
                    return InlineBotInfo(token, candidate, bot_id)
                if candidates:
                    raise InlineError("existing bot found but its token could not be read")
        except InlineError as exc:
            raise InlineError(f"bot discovery failed: {exc}") from exc
        except Exception as exc:
            raise InlineError(f"bot discovery failed: {type(exc).__name__}") from exc
        return None

    async def _fetch_token(self, conv: BotFatherConversation, username_button: str) -> str | None:
        try:
            await conv.ask("/token")
            answer = await conv.ask(username_button)
            return _token_in(answer)
        except Exception:
            return None

    async def _create_bot(self) -> InlineBotInfo:
        async with BotFatherGuard(self.runtime.app), BotFatherConversation(self.runtime.app) as conv:
            response = await conv.ask("/newbot")
            text = response.get("message", "")
            lowered = text.lower()
            if "cannot create new bots" in lowered or "contact @spambot" in lowered or "cannot create" in lowered:
                from hotaru.accounts import handle_account_critical_failure
                handle_account_critical_failure(self.runtime.config.session_dir, self.runtime.config.owner_id, "BotFather spamban: account cannot create new bots, contact @SpamBot", self.runtime)
                raise InlineError("BotFather spamban: account cannot create new bots, contact @SpamBot")
            if "too many" in lowered or "up to 20" in lowered or "limit" in lowered:
                from hotaru.accounts import handle_account_critical_failure
                handle_account_critical_failure(self.runtime.config.session_dir, self.runtime.config.owner_id, "BotFather limit reached: max 20 bots per account", self.runtime)
                raise InlineError("BotFather limit reached: max 20 bots per account")
            if "a new bot" not in lowered:
                raise InlineError("BotFather refused: " + text.splitlines()[0][:120] if text else "BotFather refused")
            await conv.ask(self.bot_name[:64])
            username, token = await self._pick_username(conv)
            bot_id = int(token.split(":", 1)[0])
            await self._configure(conv, username)
            return InlineBotInfo(token, username, bot_id)

    async def _pick_username(self, conv: BotFatherConversation) -> tuple[str, str]:
        wanted: list[str] = []
        raw = self.runtime.state.get_setting("inline-bot-username-wanted") if self.runtime.state is not None else None
        if isinstance(raw, str) and raw.strip():
            name = raw.strip().lstrip("@")
            if not name.lower().endswith("bot"):
                name += "_bot"
            wanted.append(name)

        shuffled_roots = list(_ROOTS)
        shuffled_stems = list(_STEMS)
        random.shuffle(shuffled_roots)
        random.shuffle(shuffled_stems)

        for r, s in zip(shuffled_roots, shuffled_stems):
            wanted.append(f"{r}{s}bot")
            wanted.append(f"{r}_{s}_bot")
            wanted.append(f"{r}{secrets.randbelow(90) + 10}bot")
            wanted.append(f"{r}_{s}_{secrets.token_hex(2)}_bot")

        for _ in range(8):
            suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
            wanted.append(f"hotaru_{suffix}_bot")
        seen: set[str] = set()
        for username in wanted:
            if username in seen:
                continue
            seen.add(username)
            response = await conv.ask(username)
            token = _token_in(response)
            if token is not None:
                return username, token
            lowered = str(response.get("message", "")).lower()
            if "done" in lowered or "congratulations" in lowered:
                token = await self._fetch_token(conv, "@" + username)
                if token is not None:
                    return username, token
                raise InlineError(f"bot @{username} was created but its token could not be read")
            if "sorry" in lowered or "taken" in lowered or "invalid" in lowered or "occupied" in lowered:
                continue
            raise InlineError("unexpected BotFather reply while choosing a username: " + lowered.splitlines()[0][:120] if lowered else "empty BotFather reply")
        raise InlineError("could not allocate a bot username")

    async def _configure(self, conv: BotFatherConversation, username: str) -> None:
        at = f"@{username}"
        try:
            r_inl = await conv.ask("/setinline")
            clicked = await conv.click_button(r_inl, at, username)
            if not clicked:
                await conv.ask(at)
            await conv.ask(self.inline_placeholder)
        except Exception:
            pass

        try:
            r_fb = await conv.ask("/setinlinefeedback")
            clicked = await conv.click_button(r_fb, at, username)
            if not clicked:
                r_fb = await conv.ask(at)
            c_en = await conv.click_button(r_fb, "100%", "Enabled", "Enabled (100%)")
            if not c_en:
                await conv.ask("100%")
        except Exception:
            pass

        try:
            r_about = await conv.ask("/setabouttext")
            clicked = await conv.click_button(r_about, at, username)
            if not clicked:
                await conv.ask(at)
            await conv.ask("Hotaru Userbot - security-first modular userbot")
        except Exception:
            pass

        try:
            r_desc = await conv.ask("/setdescription")
            clicked = await conv.click_button(r_desc, at, username)
            if not clicked:
                await conv.ask(at)
            await conv.ask("Hotaru Userbot - security-first modular Telegram userbot with capability sandboxing.")
        except Exception:
            pass

    async def start(self) -> None:
        async with self._start_lock:
            if self._task is not None and not self._task.done():
                return
            main = getattr(self.runtime, "app", None)
            main_mt = getattr(main, "mt", None) if main is not None else None
            auth_ready = getattr(main_mt, "auth_ready", None)
            if main is None or main_mt is None or auth_ready is None or not auth_ready.is_set():
                raise RuntimeError("cannot start inline bot without active MTProto user session")
            if self.info is None:
                await self.ensure_bot(allow_create=True)
            self._stop.clear()
            self.ready.clear()
            self._task = asyncio.create_task(self._run(), name="hotaru:inline-bot")

    def _new_app(self) -> Any:
        assert self.info is not None
        from goygram import GoyGram

        main = self.runtime.app
        if main is not None and main.core.bot_token == self.info.token:
            raise InlineError("inline polling requires a token separate from the primary bot")
        from hotaru.accounts import bot_vault_path
        uid = None
        session = getattr(self.runtime.app, "session", None)
        if session is not None:
            uid = getattr(session, "self_id", None)
        if not isinstance(uid, int) or uid <= 0:
            uid = getattr(getattr(self.runtime, "kernel", None), "owner_id", None)
        if isinstance(uid, int) and uid > 0:
            vault = bot_vault_path(self.runtime.config.session_dir, uid)
            vault.parent.mkdir(parents=True, exist_ok=True)
            name = vault.with_suffix("").name
            bot_session = Session(name=name, path=vault)
        else:
            vault = self.runtime.config.session_dir / "hotaru-inline.vault"
            name = "hotaru-inline"
            bot_session = Session(name=name, path=vault)

        if self.runtime.observatory is not None:
            path = getattr(bot_session, "path", None)
            self.runtime.observatory.emit(
                "inline", "start_prepare", username=self.info.username,
                path=str(path) if path is not None else "memory",
                vault_exists=bool(path is not None and path.exists()),
                vault_size=path.stat().st_size if path is not None and path.exists() else 0,
                session_bot=bot_session.is_bot, vault_key=bot_session.auth_key is not None,
                dc=bot_session.dc,
            )

        main = getattr(self.runtime, "app", None)
        if main and getattr(main, "mt", None):
            bot_user = main.mt.entities.get(("user", self.info.bot_id))
            if bot_user and bot_user.get("dc_id"):
                bot_session.data["dc"] = bot_user["dc_id"]

        import platform
        from hotaru.update import current_commit

        app = GoyGram(
            device_model="Hotaru",
            system_version=f"{platform.system()} {platform.release()}",
            app_version=f"Hotaru {current_commit()}",
            bot_token=self.info.token,
            api_id=self.runtime.config.api_id,
            api_hash=self.runtime.config.api_hash,
            session_name=name,
            session=bot_session,
            intake="mtproto",
            default_transport="mtproto",
            proxy=self.runtime.config.tg_proxy,
        )
        app.on_inline(self._dispatch_inline)
        app.on_cb(self._dispatch_callback)
        app.on_msg(self._dispatch_bot_pm)
        app.on_update(self._dispatch_chosen)
        return app

    async def _auth_bot(self, app: Any) -> None:
        from goygram.security import bootstrap_session

        info = self.info
        if info is None:
            raise InlineError("inline bot client is missing")
        result = await bootstrap_session(
            app.core,
            api_id=self.runtime.config.api_id,
            api_hash=self.runtime.config.api_hash,
            session_name=app.core.session_name,
            bot_token=info.token,
            session=app.session,
        )
        if not result or app.session.auth_key is None or not app.session.is_bot:
            raise InlineError("inline bot authorization did not complete")

    async def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            app = None
            try:
                main = getattr(self.runtime, "app", None)
                main_mt = getattr(main, "mt", None) if main is not None else None
                auth_ready = getattr(main_mt, "auth_ready", None)
                if main is None or main_mt is None or auth_ready is None or not auth_ready.is_set():
                    break
                if self.info is None:
                    await self.ensure_bot(allow_create=True)
                app = self.bot_app = self._new_app()
                await asyncio.wait_for(self._auth_bot(app), timeout=60.0)
                delay = 1.0
                await self._poll(app)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("inline poll failed: %s: %s\n%s", type(exc).__name__, exc, traceback.format_exc())
                if self._is_auth_failure(exc):
                    self._forget_bot()
                if self.runtime.observatory is not None:
                    event = "auth_error" if isinstance(exc, UnauthorizedError) or self._is_auth_failure(exc) else "poll_error"
                    self.runtime.observatory.emit("inline", event, "error", error=type(exc).__name__, detail=str(exc)[:240], delay=delay)
            finally:
                self.ready.clear()
                if app is not None:
                    try:
                        await app.close()
                    except Exception as exc:
                        log.error("inline app close failed: %s: %s", type(exc).__name__, exc)
                        if self.runtime.observatory is not None:
                            self.runtime.observatory.emit("inline", "close_error", error=exc)
                self.bot_app = None
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60.0)

    async def _poll(self, app: Any) -> None:
        from hotaru.supervisor import ConnectionRecovery

        tasks: list[asyncio.Task[Any]] = []
        recovery: ConnectionRecovery | None = None
        warm: asyncio.Task[None] | None = None
        try:
            await app.core.fsm.start()
            tasks.append(asyncio.create_task(app.core.disp.consume(), name="hotaru:inline-dispatch"))
            await app.mt.start()
            reader = app.mt.reader_task
            if reader is None:
                raise InlineError("inline bot reader did not start")
            tasks.append(reader)
            recovery = ConnectionRecovery(
                app.mt,
                self.runtime.observatory,
                "inline",
                ready=self.ready,
                on_fatal=self.runtime.intrusion.inspect if self.runtime.intrusion is not None else None,
            )
            await self._probe(app)
            if any(task.done() for task in tasks):
                raise InlineError("inline bot stopped during startup")
            self.ready.set()
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("inline", "ready_set", self_id=app.session.self_id, dc=app.session.dc)
            warm = asyncio.create_task(self._warm_owner_peer(app), name="hotaru:inline-warm")
            tasks.append(asyncio.create_task(self._watch_bot(app), name="hotaru:inline-health"))
            async def _watch_user_session() -> None:
                while not self._stop.is_set():
                    await asyncio.sleep(2.0)
                    main_ref = getattr(self.runtime, "app", None)
                    main_mt = getattr(main_ref, "mt", None) if main_ref is not None else None
                    if main_ref is None or main_mt is None:
                        break
                    auth_ready = getattr(main_mt, "auth_ready", None)
                    stop_ev = getattr(main_mt, "stop_ev", None)
                    if auth_ready is None or not auth_ready.is_set() or (stop_ev is not None and stop_ev.is_set()):
                        break
                self._stop.set()
            tasks.append(asyncio.create_task(_watch_user_session(), name="hotaru:inline-user-guard"))
            stop = asyncio.create_task(self._stop.wait(), name="hotaru:inline-stop")
            tasks.append(stop)
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if stop not in done:
                for task in done:
                    if task.cancelled():
                        raise InlineError("inline bot polling stopped")
                    task.result()
                raise InlineError("inline bot polling stopped")
        finally:
            self.ready.clear()
            if recovery is not None:
                await recovery.close()
            if warm is not None:
                tasks.append(warm)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe(self, app: Any) -> None:
        await app.mt.call("updates.getState", api_id=self.runtime.config.api_id, retry=0)
        await self._check_bot(app)

    async def _check_bot(self, app: Any) -> None:
        await app.mt_users_get_users(id=[{"_": "inputUserSelf"}], retry=0)
        await self._inspect_webhook(app)

    async def _inspect_webhook(self, app: Any) -> None:
        try:
            info = await app.getWebhookInfo()
        except (TimeoutError, ConnectionError, OSError) as exc:
            log.debug("inline webhook check network issue: %s: %s", type(exc).__name__, exc)
            return
        except Exception as exc:
            name = type(exc).__name__
            msg = str(exc).lower()
            if "client" in name.lower() or "connect" in name.lower() or "reset by peer" in msg or re.search(r"\bhttp 5\d\d\b", msg):
                log.debug("inline webhook check network issue: %s: %s", name, exc)
                return
            log.warning("inline webhook check failed: %s: %s", name, exc)
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit(
                    "inline", "webhook_check_failed", "error", error=name, detail=str(exc)[:240]
                )
            return
        guard = getattr(self.runtime, "intrusion", None)
        if guard is not None:
            guard.webhook(_result_body(info).get("url"))

    async def _watch_bot(self, app: Any) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(300.0)
            try:
                await self._check_bot(app)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("inline watch check skipped: %s: %s", type(exc).__name__, exc)

    async def _warm_owner_peer(self, app: Any) -> None:
        runtime = self.runtime
        owner = getattr(getattr(runtime, "kernel", None), "owner_id", None)
        mt = getattr(app, "mt", None)
        if not isinstance(owner, int) or owner <= 0 or mt is None:
            return
        deadline = time.monotonic() + WARM_DEADLINE_SECONDS
        username: str | None = None
        reported = False
        while not self._stop.is_set() and time.monotonic() < deadline:
            username = await self._owner_username()
            if username is None:
                await asyncio.sleep(3.0)
                continue
            try:
                await mt.resolve_peer("@" + username)
            except Exception as exc:
                if not reported and runtime.observatory is not None:
                    reported = True
                    runtime.observatory.emit("inline", "warmup_error", error=exc)
                await asyncio.sleep(3.0)
                continue
            entity = mt.entities.get(("user", owner))
            if entity is not None and entity.get("access_hash"):
                if runtime.observatory is not None:
                    runtime.observatory.emit("inline", "warmup_ok", username=username)
                return
            await asyncio.sleep(3.0)
        if runtime.observatory is not None and not self._stop.is_set():
            runtime.observatory.emit("inline", "warmup_missed", owner=owner, username=username)

    async def _owner_username(self) -> str | None:
        main = getattr(self.runtime, "app", None)
        mt = getattr(main, "mt", None)
        if mt is None:
            return None
        reader = mt.reader_task
        if reader is None or reader.done():
            return None
        try:
            result = await mt.call("users.getUsers", id=[{"_": "inputUserSelf"}])
        except Exception:
            return None
        if isinstance(result, dict):
            result = cast('dict[str, Any]', result).get("result", result)
        users: list[Any] = []
        if isinstance(result, list):
            users = cast('list[Any]', result)
        else:
            body = _result_body(result)
            raw = body.get("users", body)
            if isinstance(raw, list):
                users = cast('list[Any]', raw)
        if not users or not isinstance(users[0], dict):
            return None
        username = cast('dict[str, Any]', users[0]).get("username")
        return username if isinstance(username, str) and username else None

    @staticmethod
    def _is_auth_failure(exc: Exception) -> bool:
        text = str(exc).lower()
        if "http 401" in text or "unauthorized" in text:
            return True
        return "token" in text and ("invalid" in text or "revoked" in text)

    async def stop_polling(self) -> None:
        self.ready.clear()
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.bot_app is not None:
            try:
                self.bot_app.stop()
                await self.bot_app.close()
            except Exception:
                pass

    def _allows_form(self, event: Any, *, chosen: bool = False) -> bool:
        if isinstance(event, dict):
            d_event: dict[str, Any] = cast('dict[str, Any]', event)
            text: str = str(d_event.get("query") or "").strip()
            actor: Any = d_event.get("from_id")
            if actor is None:
                from_u: Any = d_event.get("from")
                if isinstance(from_u, dict):
                    actor = cast('dict[str, Any]', from_u).get("id")
            if actor is None:
                actor = cast(Any, d_event.get("user_id")) or cast(Any, d_event.get("sender_id"))
            event_msg_id: Any = d_event.get("msg_id")
        else:
            text = str(getattr(event, "query", "") or "").strip()
            actor = getattr(event, "from_id", None)
            if actor is None:
                from_user: Any = getattr(event, "from_user", None)
                if from_user is not None:
                    actor = getattr(from_user, "id", None)
            if actor is None:
                actor = getattr(event, "user_id", None) or getattr(event, "sender_id", None)
            event_msg_id = getattr(event, "msg_id", None)

        if not isinstance(actor, int) or actor <= 0:
            return False
        if text.startswith("hotaru-input:"):
            token = text.partition(" ")[0].split(":", 1)[1]
            request = self.runtime._input_request(token)
            req_item: Any = request[2] if request is not None and len(request) > 2 else None
            return bool(
                request is not None
                and (request[3] is None or request[3] > time.monotonic())
                and actor == getattr(req_item, "_hotaru_actor_id", getattr(req_item, "from_id", None))
                and token not in self._chosen_pending
            )
        if text.startswith("hotaru-form:"):
            nonce = text.split(":", 1)[1]
            forms: dict[str, Any] = getattr(self.runtime, "_inline_forms", None) or {}
            owner = self._owner_id()
            if (owner is not None and actor != owner) or nonce not in forms:
                return False
            if chosen:
                pending: dict[str, asyncio.Event] = getattr(self.runtime, "_form_chosen", None) or {}
                ready = pending.get(nonce)
                return ready is not None and not ready.is_set() and event_msg_id is not None
        return True

    async def _dispatch_inline(self, query: Any) -> None:
        if not self._allows_form(query):
            try:
                await query.answer(results=[], cache_time=0, is_personal=True)
            except Exception:
                pass
            return
        text = str(getattr(query, "query", ""))
        if self.runtime.observatory is not None:
            self.runtime.observatory.emit("inline", "query_received", src=str(getattr(query, "src", "")), qid=str(getattr(query, "id", "")), length=len(text))
        for handler in tuple(self._handlers):
            try:
                await handler(query)
            except Exception as exc:
                log.error("inline query handler failed: %s: %s\n%s", type(exc).__name__, exc, traceback.format_exc())
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "handler_error", error=type(exc).__name__, detail=str(exc)[:240])
                try:
                    await query.answer(results=[], cache_time=0, is_personal=True)
                except Exception as inner:
                    log.error("inline query answer failed: %s: %s", type(inner).__name__, inner)

    @staticmethod
    def _form_article(text: str, buttons: list[dict[str, str]]) -> dict[str, Any]:
        from goygram.types import InlineObj

        result = InlineObj.article("hotaru-form", "Hotaru form", text)
        result["reply_markup"] = {"inline_keyboard": [buttons]}
        return result

    async def _dispatch_callback(self, callback: Any) -> None:
        data = getattr(callback, "data", None)
        log.info(
            "inline callback data=%r inline_id=%s msg_id=%s from=%s",
            data,
            str(getattr(callback, "inline_message_id", None))[:120],
            str(getattr(callback, "msg_id", None))[:120],
            getattr(callback, "from_id", None),
        )
        for handler in tuple(self._cb_handlers):
            try:
                await handler(callback)
            except Exception as exc:
                log.error("inline callback handler failed: %s: %s\n%s", type(exc).__name__, exc, traceback.format_exc())
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "callback_error", error=type(exc).__name__, detail=str(exc)[:240], tb=traceback.format_exc()[-4000:])

    async def _dispatch_chosen(self, update: Any) -> None:
        if getattr(update, "update_type", None) != "updateBotInlineSend":
            return
        if not self._allows_form(update, chosen=True):
            return
        text = str(getattr(update, "query", "") or "").strip()
        from hotaru.security import AccessVerdict

        security = getattr(self.runtime, "security", None)
        request = self.runtime._input_request(text.partition(" ")[0].split(":", 1)[1]) if text.startswith("hotaru-input:") else None
        authorized = request is not None and self.runtime._input_actor_matches(update, request[2])
        if security is None or security.check(update, transport="inline", authorized=authorized) is not AccessVerdict.ALLOW:
            return
        if request is None and _is_readonly_peer(getattr(update, "from_id", None)):
            return
        token = text.partition(" ")[0].split(":", 1)[1] if text.startswith("hotaru-input:") else None
        if token is not None:
            self._chosen_pending.add(token)
        try:
            for handler in tuple(self._chosen_handlers):
                try:
                    await handler(update)
                except Exception as exc:
                    log.error("inline chosen handler failed: %s: %s\n%s", type(exc).__name__, exc, traceback.format_exc())
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "chosen_error", error=exc)
        finally:
            if token is not None:
                requests = getattr(self.runtime, "_input_requests", None)
                if requests is not None:
                    requests.pop(token, None)
                self._chosen_pending.discard(token)

    async def _dispatch_bot_pm(self, message: Any) -> None:
        security = getattr(self.runtime, "security", None)
        if security is not None:
            from hotaru.security import AccessVerdict

            verdict = security.check(message, transport="bot-pm")
            if verdict is not AccessVerdict.ALLOW:
                return
        for handler in tuple(self._pm_handlers):
            try:
                await handler(message)
            except Exception as exc:
                log.error("inline pm handler failed: %s: %s\n%s", type(exc).__name__, exc, traceback.format_exc())
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "pm_handler_error", error=exc)

    async def stop(self) -> None:
        self._stop.set()
        await self.stop_polling()
