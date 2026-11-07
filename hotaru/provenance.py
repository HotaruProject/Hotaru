from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, cast

from relay.firewall import current_module


def peer_id(value: Any) -> str:
    if isinstance(value, dict):
        value = cast(dict[str, Any], value)
        for name in ("channel_id", "chat_id", "user_id"):
            if name in value:
                number = int(value[name])
                if name == "channel_id":
                    number = -1000000000000 - number
                elif name == "chat_id":
                    number = -number
                return str(number)
    return str(value)


class MessageProvenance:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        connection.execute("CREATE TABLE IF NOT EXISTS module_messages (peer TEXT NOT NULL, message_id INTEGER NOT NULL, module_id TEXT NOT NULL, PRIMARY KEY(peer, message_id))")
        connection.commit()
        self.pending: set[asyncio.Event] = set()

    def remember(self, peer: Any, message_id: Any, module_id: str) -> None:
        if peer is None or not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            return
        self.connection.execute("INSERT OR REPLACE INTO module_messages VALUES (?, ?, ?)", (peer_id(peer), message_id, module_id))
        self.connection.commit()

    async def generated(self, message: Any) -> bool:
        waiting = tuple(self.pending)
        if waiting:
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in waiting)), 5)
            except asyncio.TimeoutError:
                return True
        peer = getattr(message, "chat_id", None)
        message_id = getattr(message, "id", None) or getattr(message, "message_id", None)
        return self.connection.execute("SELECT 1 FROM module_messages WHERE peer = ? AND message_id = ?", (peer_id(peer), message_id)).fetchone() is not None

    def record_result(self, result: Any, fallback: Any, module_id: str) -> None:
        if isinstance(result, dict):
            result = cast(dict[str, Any], result)
            kind = str(result.get("_") or "").lower()
            if "message" in kind and isinstance(result.get("id"), int):
                self.remember(result.get("peer_id") or fallback, result["id"], module_id)
            for value in result.values():
                if isinstance(value, (dict, list, tuple)):
                    self.record_result(value, fallback, module_id)
        elif isinstance(result, (list, tuple)):
            for value in cast(list[Any], result):
                self.record_result(value, fallback, module_id)

    def install(self, app: Any) -> None:
        original = cast(Callable[..., Awaitable[Any]], app._mt_rpc)
        if not callable(original):
            return

        async def tracked(method: str, **kwargs: Any) -> Any:
            owner = current_module()
            name = method.lower()
            writes = name.startswith(("messages.send", "messages.forward", "messages.edit"))
            if not owner or not writes:
                return await original(method, **kwargs)
            peer = kwargs.get("peer") or kwargs.get("to_peer")
            if isinstance(peer, str) and getattr(app, "mt", None) is not None:
                peer = await app.mt.resolve_peer(peer)
            self.remember(peer, kwargs.get("id") if isinstance(kwargs.get("id"), int) else None, owner)
            event = asyncio.Event()
            self.pending.add(event)
            try:
                result = await original(method, **kwargs)
                self.record_result(result, peer, owner)
                return result
            finally:
                self.pending.discard(event)
                event.set()

        app._mt_rpc = tracked
        if getattr(app, "mt", None) is not None:
            app.mt.call = tracked
