from __future__ import annotations

import asyncio
import html
import http.client
import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_DUPLICATED = "AUTH_KEY_DUPLICATED"
_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")


class IntrusionGuard:

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._lock = asyncio.Lock()
        self.fired = False

    def inspect(self, exc: BaseException) -> None:
        raw = getattr(exc, "message", None) or str(exc)
        if _DUPLICATED in raw:
            self._schedule("session", str(raw))

    def webhook(self, url: Any) -> None:
        value = str(url or "").strip()
        if value:
            self._schedule("webhook", value)

    async def trigger(self, source: str, detail: str) -> None:
        async with self._lock:
            if self.fired:
                return
            self.fired = True
        detail = self._redact(detail)
        log.critical("hotaru session used elsewhere source=%s detail=%s", source, detail)
        if self.runtime.observatory is not None:
            self.runtime.observatory.emit("security", "intrusion", "critical", source=source, detail=detail)
        await self._notify(source, detail)
        self.runtime.stop()

    def _schedule(self, source: str, detail: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.critical("hotaru session used elsewhere source=%s detail=%s", source, self._redact(detail))
            return
        loop.create_task(self.trigger(source, detail), name="hotaru:intrusion")

    def _token(self) -> str | None:
        state = getattr(self.runtime, "state", None)
        token = state.get_setting("inline-bot-token") if state is not None else None
        if isinstance(token, str) and token:
            return token
        info = getattr(getattr(self.runtime, "inline", None), "info", None)
        token = getattr(info, "token", None)
        return token if isinstance(token, str) and token else None

    def _owner(self) -> int | None:
        for value in (
            getattr(getattr(self.runtime, "inline", None), "_owner_id", None),
            getattr(getattr(self.runtime, "kernel", None), "owner_id", None),
            getattr(getattr(self.runtime, "config", None), "owner_id", None),
        ):
            owner = value() if callable(value) else value
            if isinstance(owner, int) and owner > 0:
                return owner
        return None

    def _redact(self, detail: str) -> str:
        token = self._token()
        if token:
            detail = detail.replace(token, "<token>")
        return _TOKEN.sub("bot<token>", detail)

    async def _notify(self, source: str, detail: str) -> None:
        token = self._token()
        owner = self._owner()
        if not token or owner is None:
            log.error("intrusion alert skipped: token=%s owner=%s", bool(token), owner)
            return
        text = self.runtime.t(
            "kernel.intrusion.alert",
            source=self.runtime.t(f"kernel.intrusion.{source}"),
            detail=html.escape(self._redact(detail), quote=False),
        )
        try:
            await asyncio.to_thread(self._send, token, owner, text)
        except Exception as exc:
            log.error("intrusion alert failed: %s: %s", type(exc).__name__, exc)

    @staticmethod
    def _send(token: str, owner: int, text: str) -> None:
        data = json.dumps(
            {"chat_id": owner, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            ensure_ascii=False,
        ).encode("utf-8")
        conn_factory = getattr(http.client, "HTTPSConnection")
        conn = conn_factory("api.telegram.org", timeout=10.0)
        try:
            conn.request(
                "POST",
                f"/bot{token}/sendMessage",
                body=data,
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            body = response.read().decode("utf-8", "replace")
        finally:
            conn.close()
        payload = json.loads(body)
        if not payload.get("ok"):
            raise RuntimeError(body[:240])
