from __future__ import annotations

import asyncio
import random
import re
import secrets
import string
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

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
        state = await self.app.mt_req(
            "messages.getHistory",
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
        result = await self.app.mt_req(
            "messages.sendMessage",
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
            await self.app.mt_req("messages.deleteMessages", id=valid, revoke=True)
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
            result = await self.app.mt_req(
                "messages.getHistory",
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
            result = await self.app.mt_req(
                "messages.getHistory",
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
        self._stop = asyncio.Event()

    def on_inline(self, handler: Callable[[Any], Awaitable[Any]]) -> Callable[[Any], Awaitable[Any]]:
        self._handlers.append(handler)
        return handler

    def on_callback(self, handler: Callable[[Any], Awaitable[Any]]) -> Callable[[Any], Awaitable[Any]]:
        self._cb_handlers.append(handler)
        return handler

    async def ensure_bot(self) -> InlineBotInfo:
        state = self.runtime.state
        if state is None:
            raise InlineError("state store is not ready")
        token = state.get_setting("inline-bot-token")
        username = state.get_setting("inline-bot-username")
        bot_id = state.get_setting("inline-bot-id")
        if token and username and bot_id:
            self.info = InlineBotInfo(str(token), str(username), int(bot_id))
            return self.info
        info = await self._find_existing_bot() or await self._create_bot()
        state.set_setting("inline-bot-token", info.token)
        state.set_setting("inline-bot-username", info.username)
        state.set_setting("inline-bot-id", info.bot_id)
        self.info = info
        return info

    async def _find_existing_bot(self) -> InlineBotInfo | None:
        state = self.runtime.state
        wanted = state.get_setting("inline-bot-username") if state else None
        try:
            async with BotFatherConversation(self.runtime.app) as conv:
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
                        if wanted and candidate.casefold() != str(wanted).casefold():
                            continue
                        token = await self._fetch_token(conv, text)
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
        async with BotFatherConversation(self.runtime.app) as conv:
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
        for _ in range(8):
            suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
            username = f"hotaru_{suffix}_bot"
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
            for message in step:
                try:
                    await conv.ask(message)
                except InlineError:
                    return

    async def start(self) -> None:
        if self.info is None:
            await self.ensure_bot()
        assert self.info is not None
        from goygram import GoyGram

        self._stop.clear()
        self.bot_app = GoyGram(
            bot_token=self.info.token,
            bot_timeout=self.poll_timeout,
        )
        self.bot_app.on_inline(self._dispatch_inline)
        self.bot_app.on_cb(self._dispatch_callback)
        self._task = asyncio.create_task(self._run(), name="hotaru:inline-bot")

    async def _run(self) -> None:
        app = self.bot_app
        assert app is not None
        delay = 1.0
        while not self._stop.is_set():
            try:
                await app.core.bot.boot()
                await app.bot_req("deleteWebhook", drop_pending_updates=False)
                await app.core.bot.spin()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "poll_error", error=type(exc).__name__)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 60.0)

    async def _dispatch_inline(self, query: Any) -> None:
        for handler in tuple(self._handlers):
            try:
                await handler(query)
            except Exception as exc:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "handler_error", error=type(exc).__name__)

    async def _dispatch_callback(self, callback: Any) -> None:
        for handler in tuple(self._cb_handlers):
            try:
                await handler(callback)
            except Exception as exc:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("inline", "callback_error", error=type(exc).__name__)

    async def stop(self) -> None:
        self._stop.set()
        if self.bot_app is not None:
            try:
                self.bot_app.stop()
                await self.bot_app.core.bot.close()
            except Exception:
                pass
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
