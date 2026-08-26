from __future__ import annotations

import inspect
from collections import OrderedDict
from typing import Any

from .commands import CommandInvocation, CommandParser
from .registry import CommandRegistry


class Kernel:
    def __init__(
        self,
        registry: CommandRegistry | None = None,
        *,
        parser: CommandParser | None = None,
        owner_id: int | None = None,
        seen_limit: int = 4096,
    ) -> None:
        if seen_limit < 1:
            raise ValueError("seen_limit must be positive")
        self.registry = registry or CommandRegistry()
        self.parser = parser or CommandParser()
        self.owner_id = owner_id
        self._seen: OrderedDict[tuple[str, int | str | None, int], None] = OrderedDict()
        self._seen_limit = seen_limit

    def attach(self, app: Any) -> None:
        app.on_edit(self._on_edit)
        app.on_msg(self._on_msg)

    async def _on_edit(self, message: Any) -> object | None:
        return await self.dispatch(message, source="edit")

    async def _on_msg(self, message: Any) -> object | None:
        return await self.dispatch(message, source="new")

    async def dispatch(self, message: Any, *, source: str) -> object | None:
        if not self._is_owner(message):
            return None
        message_id = self._message_id(message)
        chat_id = getattr(message, "chat_id", None)
        key = (source, chat_id, message_id)
        if key in self._seen:
            return None
        invocation = self.parser.parse(
            getattr(message, "text", None),
            source=source,
            message_id=message_id,
            chat_id=chat_id,
        )
        if invocation is None:
            return None
        self._remember(key)
        spec = self.registry.resolve(invocation)
        if spec is None:
            return None
        result = spec.handler(invocation)
        if inspect.isawaitable(result):
            return await result
        return result

    def _is_owner(self, message: Any) -> bool:
        if self.owner_id is not None:
            return getattr(message, "from_id", None) == self.owner_id
        return bool(getattr(message, "is_me", False))

    @staticmethod
    def _message_id(message: Any) -> int:
        value = getattr(message, "id", None)
        if value is None:
            value = getattr(message, "message_id", None)
        if not isinstance(value, int):
            raise ValueError("message must expose an integer id")
        return value

    def _remember(self, key: tuple[str, int | str | None, int]) -> None:
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > self._seen_limit:
            self._seen.popitem(last=False)

