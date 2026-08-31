from dataclasses import dataclass
from typing import Awaitable, Callable

from .commands import CommandInvocation

Handler = Callable[..., Awaitable[object] | object]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    handler: Handler
    module_id: str
    kernel: bool = False


class CommandRegistry:
    def __init__(self) -> None:
        self._items: dict[str, CommandSpec] = {}

    def register(
        self,
        name: str,
        handler: Handler,
        *,
        kernel: bool = False,
        module_id: str | None = None,
    ) -> None:
        if not name.isidentifier():
            raise ValueError("command name must be an identifier")
        if not isinstance(module_id, str) or not module_id:
            raise ValueError("every command must belong to a module")
        key = name.casefold()
        current = self._items.get(key)
        if current is not None and current.kernel and not kernel:
            raise ValueError(f"kernel command is reserved: {key}")
        if current is not None:
            raise ValueError(f"command already registered: {key}")
        self._items[key] = CommandSpec(name=key, handler=handler, kernel=kernel, module_id=module_id)

    def unregister(self, name: str, *, module_id: str | None = None) -> bool:
        key = name.casefold()
        current = self._items.get(key)
        if current is None or current.kernel or current.module_id != module_id:
            return False
        del self._items[key]
        return True

    def resolve(self, invocation: CommandInvocation) -> CommandSpec | None:
        return self._items.get(invocation.name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._items)
