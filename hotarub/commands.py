from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    name: str
    args: tuple[str, ...]
    source: str
    message_id: int
    chat_id: int | str | None
    message: Any = None


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
        if not parts or not parts[0].isidentifier():
            return None
        return CommandInvocation(
            name=parts[0].casefold(),
            args=tuple(parts[1:]),
            source=source,
            message_id=message_id,
            chat_id=chat_id,
            message=message,
        )
