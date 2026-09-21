from __future__ import annotations

import asyncio
import random
import re
import secrets
import string
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

from goygram import GoyGram, Session
from relay.firewall import trusted_scope

BOTFATHER = "@BotFather"
BOTFATHER_ID = 93372553
TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{35}")
USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,31}bot$", re.IGNORECASE)


class InlineError(RuntimeError):
    pass


@dataclass(slots=True)
class InlineBotInfo:
    token: str
    username: str
    bot_id: int


class BotFatherConversation:
    def __init__(self, app: Any, *, timeout: float = 30.0) -> None:
        self.app = app
        self.timeout = timeout
        self._peer: Any = None
        self._last_id = 0
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
        body = state.get("result") if isinstance(state, dict) and isinstance(state.get("result"), dict) else state
        messages = body.get("messages") if isinstance(body, dict) else None
        if messages:
            self._last_id = max((m.get("id", 0) for m in messages if isinstance(m, dict)), default=0)
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
        body = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else result
        if isinstance(body, dict):
            updates = body.get("updates")
            if isinstance(updates, list):
                for update in updates:
                    if not isinstance(update, dict):
                        continue
                    kind = update.get("_")
                    if kind in ("updateNewMessage", "updateMessageID"):
                        message = update.get("message")
                        if isinstance(message, dict) and isinstance(message.get("id"), int):
                            return message["id"]
                    if kind == "updateMessageID" and isinstance(update.get("id"), int):
                        return update["id"]
        return 0

    async def _delete(self, *ids: int) -> None:
        valid = [i for i in ids if isinstance(i, int) and i > 0]
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
            user_id = sender.get("user_id")
            return isinstance(user_id, int) and user_id == self._self_id
        return False

    async def response(self, *, since: int | None = None) -> dict[str, Any]:
        floor = since if since is not None else self._last_id
        deadline = time.monotonic() + self.timeout
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
            body = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else result
            messages = body.get("messages") if isinstance(body, dict) else None
            for message in messages or []:
                if not isinstance(message, dict):
                    continue
                if message.get("id", 0) <= floor:
                    continue
                if self._is_mine(message):
                    self._last_id = max(self._last_id, message["id"])
                    continue
                self._last_id = max(self._last_id, message["id"])
                return message
            await asyncio.sleep(1.0)
        raise InlineError("BotFather response timeout")

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
            body = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else result
            messages = body.get("messages") if isinstance(body, dict) else None
            recent = [m for m in messages or [] if isinstance(m, dict) and m.get("id", 0) > self._last_id]
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
        await self._delete(mine_id, reply.get("id", 0))
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
        body = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else result
        for dialog in (body.get("dialogs") or []) if isinstance(body, dict) else []:
            if isinstance(dialog, dict):
                return dialog
        return None

    async def read_state(self) -> dict[str, Any]:
        dialog = await self._dialog()
        if dialog is None:
            return {"archived": False, "muted": False}
        settings_raw = dialog.get("notify_settings")
        settings = settings_raw if isinstance(settings_raw, dict) else {}
        mute_until = settings.get("mute_until") or 0
        return {
            "archived": isinstance(dialog.get("folder_id"), int) and dialog["folder_id"] != 0,
            "muted": isinstance(mute_until, int) and mute_until > int(time.time()),
        }

    async def _folder_peer(self, folder_id: int) -> dict[str, Any]:
        await self._resolve()
        entity = self.app.mt.entity_usernames.get("botfather") or self.app.mt.entities.get(("user", BOTFATHER_ID)) or {}
        access_hash = entity.get("access_hash") if isinstance(entity, dict) else 0
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
        self._reauth_task: asyncio.Task[None] | None = None
        self._handlers: list[Callable[[Any], Awaitable[Any]]] = []
        self._cb_handlers: list[Callable[[Any], Awaitable[Any]]] = []
        self._pm_handlers: list[Callable[[Any], Awaitable[Any]]] = []
        self._chosen_handlers: list[Callable[[Any], Awaitable[Any]]] = []
        self._stop = asyncio.Event()
        self.ready = asyncio.Event()
        self._session_failure = asyncio.Event()
        self._create_attempts: list[float] = []
        self._provision_lock = asyncio.Lock()
        self._reauth_lock = asyncio.Lock()

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
        if owner is not None:
            state.set_setting("owner-id", owner)
        self.info = info

    async def ensure_bot(self, *, allow_create: bool = True) -> InlineBotInfo:
        state = self.runtime.state
        if state is None:
            raise InlineError("state store is not ready")
        owner = self._owner_id()
        if owner is not None:
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
        await self._start_bot_chat(info.username)
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

    async def _start_bot_chat(self, username: str) -> bool:
        app = self.runtime.app
        if app is None or app.mt is None:
            return False
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
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "chat_unblocked", username=username)
                    return True
                except Exception as retry_exc:
                    exc = retry_exc
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("inline", "start_chat_skipped", error=type(exc).__name__, detail=str(exc)[:160])
            return False
        return True

    async def reopen_owner_chat(self, chat_id: int | str | None) -> bool:
        if not isinstance(chat_id, int) or self.info is None:
            return False
        try:
            return await self._start_bot_chat(self.info.username)
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
            botid = me.get("id")
            username = me.get("username")
            if not isinstance(botid, int) or botid <= 0 or not isinstance(username, str) or not username or me.get("is_bot") is not True:
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
        candidates: list[tuple[str, str]] = []
        try:
            async with BotFatherGuard(self.runtime.app), BotFatherConversation(self.runtime.app) as conv:
                response = await conv.ask("/mybots")
                markup = response.get("reply_markup")
                rows = markup.get("rows") if isinstance(markup, dict) else None
                if not rows:
                    return None
                for row in rows:
                    for button in row.get("buttons", []):
                        text = button.get("text", "")
                        candidate = text.lstrip("@")
                        if not USERNAME_RE.match(candidate):
                            continue
                        if wanted and candidate.casefold() == str(wanted).casefold():
                            candidates.append((text, candidate))
                for button_text, candidate in candidates:
                    token = await self._fetch_token(conv, button_text)
                    if token is None:
                        continue
                    bot_id = int(token.split(":", 1)[0])
                    return InlineBotInfo(token, candidate, bot_id)
        except InlineError:
            return None
        except Exception:
            return None
        return None

    async def _fetch_token(self, conv: BotFatherConversation, username_button: str) -> str | None:
        try:
            await conv.ask("/token")
            answer = await conv.ask(username_button)
            text = answer.get("message", "")
            match = TOKEN_RE.search(text)
            return match.group(0) if match else None
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
            text = response.get("message", "")
            lowered = text.lower()
            if "sorry" in lowered or "taken" in lowered or "invalid" in lowered or "occupied" in lowered:
                continue
            match = TOKEN_RE.search(text)
            if match is None:
                continue
            return username, match.group(0)
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
        if self._task is not None and not self._task.done():
            return
        if self.info is None:
            await self.ensure_bot(allow_create=True)
        assert self.info is not None
        from goygram import GoyGram

        main = self.runtime.app
        if main is not None and main.core.bot_token == self.info.token:
            raise InlineError("inline polling requires a token separate from the primary bot")
        self._stop.clear()
        self.ready.clear()
        self._session_failure.clear()
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
            from goygram.security import _read_vault
            bot_session_data = _read_vault(vault, vault.with_suffix("").name) or {} if vault.exists() else {}
            bot_session = Session(name=name, path=vault, data=bot_session_data)
        else:
            name = str(self.runtime.config.session_dir / "hotaru-inline")
            bot_session = Session(name=name)

        self._mark_bot_session(bot_session)

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

        self.bot_app = GoyGram(
            bot_token=self.info.token,
            api_id=self.runtime.config.api_id,
            api_hash=self.runtime.config.api_hash,
            session_name=name,
            session=bot_session,
            intake="mtproto",
            default_transport="mtproto",
        )
        self.bot_app.on_inline(self._dispatch_inline)
        self.bot_app.on_cb(self._dispatch_callback)
        self.bot_app.on_msg(self._dispatch_bot_pm)
        self.bot_app.on_update(self._dispatch_chosen)
        await self._auth_bot()
        self._mark_bot_session(self.bot_app.session)
        self.bot_app.self_id = self.info.bot_id
        if self.bot_app.mt is not None:
            self.bot_app.mt.self_id = self.info.bot_id
        self.bot_app.session.save()
        self._task = asyncio.create_task(self._run(), name="hotaru:inline-bot")
        if self.runtime.observatory is not None:
            self.runtime.observatory.emit("inline", "run_scheduled", username=self.info.username)

    def _mark_bot_session(self, session: Any) -> None:
        info = self.info
        if info is None:
            return
        user = session.data.get("user")
        if not isinstance(user, dict):
            user = {}
        user.update({"id": info.bot_id, "bot": True, "username": info.username})
        session.data.update({"user": user, "self_id": info.bot_id, "is_bot": True})

    async def _auth_bot(self) -> None:
        from goygram.security import _mt_bot_auth_flow, bootstrap_session

        app = self.bot_app
        info = self.info
        if app is None or info is None:
            raise InlineError("inline bot client is missing")
        last: Exception | None = None
        for attempt in range(6):
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("inline", "auth_begin", attempt=attempt + 1, username=info.username)
            try:
                result = await bootstrap_session(
                    app.core,
                    api_id=self.runtime.config.api_id,
                    api_hash=self.runtime.config.api_hash,
                    session_name=app.core.session_name,
                    bot_token=info.token,
                    session=app.session,
                )
            except Exception as exc:
                last = exc
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "auth_error", attempt=attempt + 1, error=type(exc).__name__, detail=str(exc)[:240])
                msg = str(exc).lower()
                if "no response" in msg or "timeout" in msg:
                    mt = getattr(app, "mt", None)
                    if mt is not None:
                        try:
                            await mt.reconnect()
                        except Exception:
                            pass
                await asyncio.sleep(2.0 * (attempt + 1))
                continue

            source = result.get("source") if isinstance(result, dict) else None
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("inline", "auth_result", attempt=attempt + 1, source=source or "none", result=bool(result))
            if source == "vault":
                try:
                    await app.mt_users_get_users(id=[{"_": "inputUserSelf"}])
                except Exception as exc:
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "auth_vault_probe_error", attempt=attempt + 1, error=type(exc).__name__, detail=str(exc)[:240])
                    if not self._is_session_auth_failure(exc):
                        last = exc
                        await asyncio.sleep(2.0 * (attempt + 1))
                        continue
                    path = getattr(app.session, "path", None)
                    if not isinstance(path, Path):
                        path = Path(f"{app.session.name}.vault")
                    path.unlink(missing_ok=True)
                    result = await _mt_bot_auth_flow(
                        app.core,
                        path,
                        session_name=app.session.name,
                        api_id=int(self.runtime.config.api_id),
                        api_hash=str(self.runtime.config.api_hash),
                        bot_token=info.token,
                    )
                    source = result.get("source") if isinstance(result, dict) else None
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "auth_import_result", attempt=attempt + 1, source=source or "none", result=bool(result))
                else:
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "auth_vault_probe_ok", attempt=attempt + 1)
                    return

            key = getattr(app.session, "auth_key", None)
            mt_key = getattr(getattr(app, "mt", None), "auth_key", None)
            if key is None and isinstance(mt_key, (bytes, bytearray)) and mt_key:
                app.session.data["auth_key"] = bytes(mt_key).hex()
                key = app.session.auth_key
            if result and key is not None:
                return
            last = InlineError("inline bot authorization did not complete")
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("inline", "auth_incomplete", attempt=attempt + 1, source=source or "none", session_key=key is not None, mt_key=mt_key is not None)
            await asyncio.sleep(2.0 * (attempt + 1))
        raise InlineError(f"inline bot authorization failed: {last}")

    async def _warm_owner_peer(self) -> None:
        app = self.bot_app
        runtime = self.runtime
        if app is None or runtime is None:
            return
        kernel = getattr(runtime, "kernel", None)
        owner = getattr(kernel, "owner_id", None) if kernel is not None else None
        if not isinstance(owner, int) or owner <= 0:
            return
        mt = getattr(app, "mt", None)
        if mt is None:
            return
        # Prefer username from already-resolved bot info — it's available
        # immediately without any network round-trip and avoids the startup
        # timeout that occurred when querying the userbot MT before its
        # connection was fully established.
        username = getattr(self.info, "username", None) if self.info is not None else None
        if not isinstance(username, str) or not username:
            # Fall back to fetching via userbot MT only if info has no username.
            main = getattr(runtime, "app", None)
            main_mt = getattr(main, "mt", None) if main is not None else None
            if main_mt is not None:
                try:
                    result = await main_mt.call("users.getUsers", id=[{"_": "inputPeerSelf"}])
                    body = result.get("result", result) if isinstance(result, dict) else result
                    users = body.get("users") if isinstance(body, dict) else body
                    if isinstance(users, list) and users and isinstance(users[0], dict):
                        username = users[0].get("username")
                    elif isinstance(body, dict) and isinstance(body.get("username"), str):
                        username = body.get("username")
                except Exception as exc:
                    if runtime.observatory is not None:
                        runtime.observatory.emit("inline", "warmup_self_lookup_error", error=type(exc).__name__)
        if not isinstance(username, str) or not username:
            if runtime.observatory is not None:
                runtime.observatory.emit("inline", "warmup_no_username")
            return
        for attempt in range(3):
            if self._stop.is_set():
                return
            try:
                await mt.resolve_peer("@" + username)
            except Exception as exc:
                if runtime.observatory is not None:
                    runtime.observatory.emit("inline", "warmup_error", attempt=attempt, error=type(exc).__name__)
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            entity = mt.entities.get(("user", int(owner)))
            if entity is not None and entity.get("access_hash"):
                if runtime.observatory is not None:
                    runtime.observatory.emit("inline", "warmup_ok", username=username)
                return

    async def _run(self) -> None:
        app = self.bot_app
        assert app is not None
        delay = 1.0
        if self.runtime.observatory is not None:
            self.runtime.observatory.emit("inline", "run_begin")
        while not self._stop.is_set():
            ready_task = None
            try:
                ready_task = asyncio.create_task(self._await_ready(app), name="hotaru:inline-ready")
                await app.run()
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "run_return", session_failure=self._session_failure.is_set(), stopped=self._stop.is_set())
                if self._session_failure.is_set():
                    await self._restart_bot_session(app)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._is_session_auth_failure(exc):
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "session_reauth", error=type(exc).__name__)
                    await self._restart_bot_session(app)
                    return
                if self._is_auth_failure(exc):
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "token_revoked_midflight")
                    self._forget_bot()
                    await self.stop_polling()
                    try:
                        await self.ensure_bot()
                        await self.start()
                    except Exception as retry_exc:
                        if self.runtime.observatory is not None:
                            self.runtime.observatory.emit("inline", "reprovision_failed", error=type(retry_exc).__name__)
                    return
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "poll_error", error=type(exc).__name__, detail=str(exc)[:240], delay=delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 60.0)
            finally:
                if ready_task is not None and not ready_task.done():
                    ready_task.cancel()

    async def _await_ready(self, app: Any) -> None:
        while not self._stop.is_set():
            session = getattr(app, "session", None)
            mt = getattr(app, "mt", None)
            if mt is not None and session is not None and session.is_bot:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "health_begin", session_key=session.auth_key is not None, mt_key=getattr(mt, "auth_key", None) is not None)
                try:
                    await app.mt_users_get_users(id=[{"_": "inputUserSelf"}])
                except Exception as exc:
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "health_error", error=type(exc).__name__, detail=str(exc)[:240], auth_failure=self._is_session_auth_failure(exc))
                    if self._is_session_auth_failure(exc):
                        self._session_failure.set()
                        if self.runtime.observatory is not None:
                            self.runtime.observatory.emit("inline", "session_invalid", error=type(exc).__name__)
                        app.stop()
                        if self._reauth_task is None or self._reauth_task.done():
                            self._reauth_task = asyncio.create_task(self._restart_bot_session(app), name="hotaru:inline-reauth")
                        return
                    await asyncio.sleep(0.5)
                    continue
                self.ready.set()
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "ready_set", self_id=getattr(app.session, "self_id", None), dc=getattr(app.session, "dc", None))
                asyncio.create_task(self._warm_owner_peer())
                return
            await asyncio.sleep(0.2)

    @staticmethod
    def _is_session_auth_failure(exc: Exception) -> bool:
        text = str(exc).upper()
        return any(value in text for value in ("AUTH_KEY_UNREGISTERED", "AUTH_KEY_INVALID", "AUTH_KEY_PERM_EMPTY", "AUTH_KEY_DUPLICATED", "SESSION_REVOKED", "SESSION_EXPIRED"))

    async def _restart_bot_session(self, app: Any) -> None:
        async with self._reauth_lock:
            if self.bot_app is not app:
                return
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("inline", "recovery_begin", username=getattr(self.info, "username", None))
            self.ready.clear()
            try:
                app.stop()
                await app.close()
            except Exception:
                pass
            session = getattr(app, "session", None)
            path = getattr(session, "path", None)
            if path is not None:
                path.unlink(missing_ok=True)
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("inline", "recovery_vault_removed", path=str(path) if path is not None else "memory")
            info = self.info
            if info is not None:
                try:
                    self.info = await self.getbot(info.token)
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "recovery_token_valid", username=self.info.username)
                except InlineError:
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "recovery_token_invalid", username=info.username)
                    self._forget_bot()
                    await self.ensure_bot(allow_create=True)
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("inline", "recovery_bot_replaced", username=getattr(self.info, "username", None))
            self.bot_app = None
            self._task = None
            try:
                await self.start()
            except Exception as exc:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "recovery_failed", error=type(exc).__name__, detail=str(exc)[:240])
                raise
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("inline", "recovery_restarted", username=getattr(self.info, "username", None))

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

    async def _dispatch_inline(self, query: Any) -> None:
        if self.runtime.observatory is not None:
            self.runtime.observatory.emit("inline", "query_received", src=str(getattr(query, "src", "")), qid=str(getattr(query, "id", "")), text=str(getattr(query, "query", ""))[:64])
        for handler in tuple(self._handlers):
            try:
                await handler(query)
            except Exception as exc:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "handler_error", error=type(exc).__name__, detail=str(exc)[:240])

    @staticmethod
    def _form_article(text: str, buttons: list[dict[str, str]]) -> dict[str, Any]:
        from goygram.types import InlineObj

        result = InlineObj.article("hotaru-form", "Hotaru form", text)
        result["reply_markup"] = {"inline_keyboard": [buttons]}
        return result

    async def _dispatch_callback(self, callback: Any) -> None:
        security = getattr(self.runtime, "security", None)
        if security is not None:
            from hotaru.security import AccessVerdict

            verdict = security.check_callback(callback, transport="inline")
            if verdict is not AccessVerdict.ALLOW:
                return
        for handler in tuple(self._cb_handlers):
            try:
                await handler(callback)
            except Exception as exc:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "callback_error", error=type(exc).__name__, detail=str(exc)[:240])

    async def _dispatch_chosen(self, update: Any) -> None:
        if getattr(update, "update_type", None) != "updateBotInlineSend":
            return
        for handler in tuple(self._chosen_handlers):
            try:
                await handler(update)
            except Exception as exc:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "chosen_error", error=type(exc).__name__, detail=str(exc)[:240])

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
