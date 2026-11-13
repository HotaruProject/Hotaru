from __future__ import annotations

import asyncio
import logging
from typing import Any

from goygram.errors import FloodWaitError

from relay.emoji import to_entities

log = logging.getLogger(__name__)

FLOOD_WAIT_LIMIT = 5.0
NOTICE_TEXT_LIMIT = 3000


class FloodFallback:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def _clients(self, src: str | None) -> list[tuple[str, Any]]:
        inline = getattr(self.runtime, "inline", None)
        pairs = [
            ("userbot", getattr(self.runtime, "app", None)),
            ("bot", getattr(inline, "bot_app", None)),
        ]
        if src == "bot":
            pairs.reverse()
        return [(name, app) for name, app in pairs if app is not None]

    async def send(
        self,
        chat_id: Any,
        html: str,
        *,
        src: str | None = None,
        reply_to: int | None = None,
        topic_id: int | None = None,
    ) -> Any:
        for name, app in self._clients(src):
            try:
                return await self._deliver(app, name, chat_id, html, reply_to=reply_to, topic_id=topic_id)
            except FloodWaitError as exc:
                log.warning(
                    "flood fallback: %s sender flooded for chat %s (%ss): %s",
                    name,
                    chat_id,
                    getattr(exc, "seconds", "?"),
                    exc,
                )
            except Exception as exc:
                log.warning(
                    "flood fallback: %s sender failed for chat %s: %s: %s",
                    name,
                    chat_id,
                    type(exc).__name__,
                    exc,
                )
        log.error("flood fallback: message for chat %s was not delivered by any sender", chat_id)
        await self.notify(chat_id, html, src=src)
        return None

    async def _deliver(
        self,
        app: Any,
        name: str,
        chat_id: Any,
        html: str,
        *,
        reply_to: int | None,
        topic_id: int | None,
    ) -> Any:
        attempt = 0
        while True:
            payload: dict[str, Any] = {}
            if reply_to is not None:
                payload["reply_to"] = reply_to
            if topic_id is not None:
                payload["topic_id"] = topic_id
            if name == "userbot":
                text, entities = to_entities(html)
                payload["entities"] = entities
            else:
                text = html
                payload["parse_mode"] = "HTML"
            try:
                return await app.send_msg(chat_id, text, **payload)
            except FloodWaitError as exc:
                if attempt or getattr(exc, "seconds", 0) > FLOOD_WAIT_LIMIT:
                    raise
                attempt += 1
                await asyncio.sleep(exc.seconds)
                log.warning(
                    "flood fallback: %s sender retry for chat %s after %ss",
                    name,
                    chat_id,
                    exc.seconds,
                )
            except Exception as exc:
                if name == "bot" and attempt == 0:
                    reopen = getattr(getattr(self.runtime, "inline", None), "reopen_owner_chat", None)
                    blocked = any(item in str(exc).lower() for item in ("chat not found", "bot was blocked", "forbidden: bot", "peer_id_invalid", "input_user_deactivated"))
                    if blocked and callable(reopen) and await reopen(chat_id):
                        attempt += 1
                        return await app.send_msg(chat_id, text, **payload)
                raise

    async def notify(self, chat_id: Any, html: str, *, src: str | None = None) -> None:
        config = getattr(self.runtime, "config", None)
        target = getattr(config, "log_chat", None)
        if not isinstance(target, int) or target == chat_id:
            owner = getattr(config, "owner_id", None)
            target = owner if isinstance(owner, int) and owner != chat_id else None
        if target is None:
            log.error("flood fallback: no log chat configured, message for chat %s lost", chat_id)
            return
        body = html if len(html) <= NOTICE_TEXT_LIMIT else html[:NOTICE_TEXT_LIMIT] + "…"
        try:
            note = self.runtime.t(
                "kernel.flood.notice",
                chat=chat_id,
                source=src or "mt",
                text=body,
            )
        except Exception as exc:
            log.error("flood fallback: notice template failed: %s: %s", type(exc).__name__, exc)
            note = f"Message delivery failed for chat {chat_id}\n\n{body}"
        for name, app in self._clients(None):
            try:
                await self._deliver(app, name, target, note, reply_to=None, topic_id=None)
                return
            except Exception as exc:
                log.warning(
                    "flood fallback: notice via %s to %s failed: %s: %s",
                    name,
                    target,
                    type(exc).__name__,
                    exc,
                )
        log.error("flood fallback: notice for chat %s could not be delivered to %s", chat_id, target)
