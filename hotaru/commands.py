from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Collection

from .layouts import swap_layout


def valid_prefix(prefix: object) -> bool:
    return isinstance(prefix, str) and 1 <= len(prefix) <= 8 and prefix.isprintable() and not any(c.isspace() for c in prefix)


@dataclass(frozen=True)
class CommandInvocation:
    name: str
    args: tuple[str, ...]
    source: str
    message_id: int
    chat_id: int | str | None
    message: Any = None
    layout_swapped: bool = False
    module_id: str | None = None
    raw_args: str = ""


class CommandParser:
    def __init__(self, prefix: str = "!") -> None:
        self._prefix: str = ""
        self.prefix = prefix

    @property
    def prefix(self) -> str:
        return self._prefix

    @prefix.setter
    def prefix(self, value: str) -> None:
        if not valid_prefix(value):
            raise ValueError("prefix must contain 1–8 visible non-whitespace characters")
        self._prefix = value

    def parse(
        self,
        text: str | None,
        *,
        source: str,
        message_id: int,
        chat_id: int | str | None,
        message: Any = None,
        aliases: Collection[str] | None = None,
    ) -> CommandInvocation | None:
        if not isinstance(text, str) or not text:
            return None
        if not text.startswith(self.prefix):
            return None
        body = text[len(self.prefix):]
        if body.startswith(self.prefix):
            return None
        body = body.lstrip()
        if not body:
            return None
        words = body.split()
        size = 1
        if aliases:
            for candidate in range(len(words), 1, -1):
                if " ".join(words[:candidate]).casefold() in aliases:
                    size = candidate
                    break
        name = " ".join(words[:size]).casefold()
        end = 0
        for word in words[:size]:
            end = body.index(word, end) + len(word)
        raw = body[end:].lstrip()
        return CommandInvocation(
            name=name,
            args=tuple(words[size:]),
            source=source,
            message_id=message_id,
            chat_id=chat_id,
            message=message,
            raw_args=raw,
        )

    def command_name(self, text: str | None) -> str | None:
        invocation = self.parse(text, source="command", message_id=0, chat_id=None)
        return invocation.name if invocation is not None else None

    def swap_invocation(self, invocation: CommandInvocation) -> CommandInvocation | None:
        swapped = swap_layout(invocation.name).casefold()
        if not swapped.isidentifier() or swapped == invocation.name:
            return None
        return replace(invocation, name=swapped, layout_swapped=True)
