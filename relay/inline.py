from __future__ import annotations

import asyncio
import random
import re
import secrets
import string
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, cast

from goygram import Session
from goygram.errors import ConnectionClosedError
from relay.firewall import trusted_scope

BOTFATHER = "@BotFather"
BOTFATHER_ID = 93372553
TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{35}")
USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,31}bot$", re.IGNORECASE)
WARM_DEADLINE_SECONDS = 120.0


class InlineError(RuntimeError):
    pass


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
        return None

    async def say(self, text: str) -> int:
        result = await self.app.mt_messages_send_message(
            peer=self._peer,
            message=text,
            random_id=secrets.randbits(63),
        )
        return self._extract_sent_id(result)

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
        # botfather may answer one step with several messages
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
            raise
        parts = reply.get("parts")
        ids = [cast('dict[str, Any]', item).get("id", 0) for item in cast('list[Any]', parts)] if isinstance(parts, list) else [reply.get("id", 0)]
        await self._delete(mine_id, *ids)
        return reply

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
            async with self._provision_lock:
                info = await self._create_bot()
            recovered = True
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
        asyncio.create_task(self._start_bot_chat(info.username), name="hotaru:bot-chat")
        return info

    async def _tune_bot(self, info: InlineBotInfo) -> None:
        from goygram import GoyGram

        app = GoyGram(bot_token=info.token, default_transport="api")
        try:
            await app.get_me()
            for method, payload in (
                ("deleteWebhook", {"drop_pending_updates": True}),
                ("setMyName", {"name": self.bot_name[:64]}),
                ("setMyShortDescription", {"short_description": "Hotaru"}),
                ("setMyDescription", {"description": "Hotaru userbot"}),
            ):
                try:
                    fn = getattr(app, method, None)
                    if callable(fn):
                        await cast(Any, fn)(**payload)
                except Exception:
                    continue
        except Exception:
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("inline", "tune_failed", username=info.username)
        finally:
            try:
                await app.close()
            except Exception:
                pass

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

        app = GoyGram(bot_token=token, default_transport="api")
        try:
            me = await app.get_me()
            if not isinstance(me, dict):
                raise InlineError("inline bot token validation failed")
            me_dict = cast('dict[str, Any]', me)
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
                raise InlineError("BotFather spamban: account cannot create new bots, contact @SpamBot")
            if "too many" in lowered or "up to 20" in lowered or "limit" in lowered:
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
        for step in (
            ("/setinline", at, self.inline_placeholder),
            ("/setinlinefeedback", at, "Enabled"),
        ):
            retried = False
            for message in step:
                try:
                    response = await conv.ask(message)
                except InlineError:
                    if retried:
                        return
                    retried = True
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "configure_retry", step=step[0], message=message)
                    continue
                text = response.get("message", "").lower()
                if "invalid bot selected" in text and not retried:
                    retried = True
                    await conv.ask(message)
                if "invalid bot selected" in text and retried:
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "configure_desync", step=step[0])
                    return

    async def start(self) -> None:
        async with self._start_lock:
            if self._task is not None and not self._task.done():
                return
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

        app = GoyGram(
            bot_token=self.info.token,
            api_id=self.runtime.config.api_id,
            api_hash=self.runtime.config.api_hash,
            session_name=name,
            session=bot_session,
            intake="mtproto",
            default_transport="mtproto",
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
                if self.info is None:
                    await self.ensure_bot(allow_create=True)
                app = self.bot_app = self._new_app()
                await asyncio.wait_for(self._auth_bot(app), timeout=60.0)
                delay = 1.0
                await self._poll(app)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._is_auth_failure(exc):
                    self._forget_bot()
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "poll_error", error=type(exc).__name__, detail=str(exc)[:240], delay=delay)
            finally:
                self.ready.clear()
                if app is not None:
                    try:
                        await app.close()
                    except Exception as exc:
                        if self.runtime.observatory is not None:
                            self.runtime.observatory.emit("inline", "close_error", error=type(exc).__name__)
                self.bot_app = None
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60.0)

    async def _poll(self, app: Any) -> None:
        tasks: list[asyncio.Task[Any]] = []
        try:
            await app.core.fsm.start()
            tasks.append(asyncio.create_task(app.core.disp.consume(), name="hotaru:inline-dispatch"))
            await app.mt.start()
            reader = app.mt.reader_task
            if reader is None:
                raise InlineError("inline bot reader did not start")
            tasks.append(reader)
            await self._probe(app)
            if any(task.done() for task in tasks):
                raise InlineError("inline bot stopped during startup")
            self.ready.set()
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("inline", "ready_set", self_id=app.session.self_id, dc=app.session.dc)
            asyncio.create_task(self._warm_owner_peer(app), name="hotaru:inline-warm")
            tasks.append(asyncio.create_task(self._watch_bot(app), name="hotaru:inline-health"))
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
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe(self, app: Any) -> None:
        deadline = time.monotonic() + 3.0
        while True:
            try:
                await asyncio.wait_for(app.mt.call("updates.getState", api_id=self.runtime.config.api_id), timeout=15.0)
                await self._check_bot(app)
                return
            except (ConnectionError, ConnectionClosedError, TimeoutError, asyncio.TimeoutError):
                if self._stop.is_set() or time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.05)

    async def _check_bot(self, app: Any) -> None:
        await asyncio.wait_for(app.mt_users_get_users(id=[{"_": "inputUserSelf"}], retry=0), timeout=15.0)

    async def _watch_bot(self, app: Any) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30.0)
            await self._check_bot(app)

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
                    runtime.observatory.emit("inline", "warmup_error", error=type(exc).__name__)
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
            result = await mt.call("users.getUsers", id=[{"_": "inputPeerSelf"}])
        except Exception:
            return None
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
        text = str(getattr(event, "query", "") or "").strip()
        actor = getattr(event, "from_id", None)
        if type(actor) is not int or actor <= 0:
            return False
        if text.startswith("hotaru-input:"):
            token = text.partition(" ")[0].split(":", 1)[1]
            requests: dict[str, tuple[Any, ...]] = getattr(self.runtime, "_input_requests", None) or {}
            request = requests.get(token)
            return bool(
                request is not None
                and (request[3] is None or request[3] > time.monotonic())
                and actor == getattr(request[2], "_hotaru_actor_id", getattr(request[2], "from_id", None))
                and token not in self._chosen_pending
            )
        if text.startswith("hotaru-form:"):
            nonce = text.split(":", 1)[1]
            forms: dict[str, Any] = getattr(self.runtime, "_inline_forms", None) or {}
            if actor != self._owner_id() or nonce not in forms:
                return False
            if chosen:
                pending: dict[str, asyncio.Event] = getattr(self.runtime, "_form_chosen", None) or {}
                ready = pending.get(nonce)
                return ready is not None and not ready.is_set() and getattr(event, "msg_id", None) is not None
        return True

    async def _dispatch_inline(self, query: Any) -> None:
        if not self._allows_form(query):
            return
        text = str(getattr(query, "query", ""))
        if self.runtime.observatory is not None:
            self.runtime.observatory.emit("inline", "query_received", src=str(getattr(query, "src", "")), qid=str(getattr(query, "id", "")), length=len(text))
        for handler in tuple(self._handlers):
            try:
                await handler(query)
            except Exception as exc:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "handler_error", error=type(exc).__name__, detail=str(exc)[:240])
                try:
                    await query.answer(results=[], cache_time=0, is_personal=True)
                except Exception:
                    pass

    @staticmethod
    def _form_article(text: str, buttons: list[dict[str, str]]) -> dict[str, Any]:
        from goygram.types import InlineObj

        result = InlineObj.article("hotaru-form", "Hotaru form", text)
        result["reply_markup"] = {"inline_keyboard": [buttons]}
        return result

    async def _dispatch_callback(self, callback: Any) -> None:
        for handler in tuple(self._cb_handlers):
            try:
                await handler(callback)
            except Exception as exc:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "callback_error", error=type(exc).__name__, detail=str(exc)[:240])

    async def _dispatch_chosen(self, update: Any) -> None:
        if getattr(update, "update_type", None) != "updateBotInlineSend":
            return
        if not self._allows_form(update, chosen=True):
            return
        text = str(getattr(update, "query", "") or "").strip()
        screens = getattr(self.runtime, "screens", None)
        if screens is None or not screens.owns(text):
            from hotaru.security import AccessVerdict

            security = getattr(self.runtime, "security", None)
            requests = cast('Mapping[str, tuple[object, ...]]', getattr(self.runtime, "_input_requests", None) or {})
            request = requests.get(text.partition(" ")[0].split(":", 1)[1]) if text.startswith("hotaru-input:") else None
            authorized = request is not None and self.runtime._input_actor_matches(update, request[2])
            if security is None or security.check(update, transport="inline", authorized=authorized) is not AccessVerdict.ALLOW:
                return
        token = text.partition(" ")[0].split(":", 1)[1] if text.startswith("hotaru-input:") else None
        if token is not None:
            self._chosen_pending.add(token)
        try:
            for handler in tuple(self._chosen_handlers):
                try:
                    await handler(update)
                except Exception as exc:
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "chosen_error", error=type(exc).__name__)
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
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "pm_handler_error", error=type(exc).__name__)

    async def stop(self) -> None:
        self._stop.set()
        await self.stop_polling()
