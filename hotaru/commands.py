from dataclasses import dataclass
from typing import Any

from .layouts import swap_layout


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    name: str
    args: tuple[str, ...]
    source: str
    message_id: int
    chat_id: int | str | None
    message: Any = None
    layout_swapped: bool = False
    module_id: str | None = None


class CommandParser:
    def __init__(self, prefix: str = "!") -> None:
        if len(prefix) != 1 or prefix.isspace():
            raise ValueError("prefix must be one non-whitespace character")
        self.prefix = prefix

    def parse(
        self,
        text: str | None,
        *,
        source: str,
        message_id: int,
        chat_id: int | str | None,
        message: Any = None,
    ) -> CommandInvocation | None:
        if not text or not text.startswith(self.prefix):
            return None
        parts = text[len(self.prefix) :].split()
        if not parts:
            return None
        name = parts[0].casefold()
        if not name.isidentifier():
            return None
        return CommandInvocation(
            name=name,
            args=tuple(parts[1:]),
            source=source,
            message_id=message_id,
            chat_id=chat_id,
            message=message,
        )

    def swap_invocation(self, invocation: CommandInvocation) -> CommandInvocation | None:
        swapped = swap_layout(invocation.name).casefold()
        if not swapped.isidentifier() or swapped == invocation.name:
            return None
        return CommandInvocation(
            name=swapped,
            args=invocation.args,
            source=invocation.source,
            message_id=invocation.message_id,
            chat_id=invocation.chat_id,
            message=invocation.message,
            layout_swapped=True,
        )
