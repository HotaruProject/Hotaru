from dataclasses import dataclass
from typing import Any, Awaitable, Callable

InlineHandler = Callable[..., Awaitable[list[dict[str, Any]] | dict[str, Any] | None] | list[dict[str, Any] | None] | dict[str, Any] | None]


@dataclass(frozen=True, slots=True)
class InlineCommandSpec:
    name: str
    handler: InlineHandler
    module_id: str
    sandbox: bool = False


class InlineRegistry:
    def __init__(self) -> None:
        self._items: dict[str, InlineCommandSpec] = {}

    def register(self, name: str, handler: InlineHandler, *, module_id: str | None = None, sandbox: bool = False) -> None:
        if not name.isidentifier():
            raise ValueError("inline command name must be an identifier")
        if not isinstance(module_id, str) or not module_id:
            raise ValueError("every inline command must belong to a module")
        key = name.casefold()
        if key in self._items:
            raise ValueError(f"inline command already registered: {key}")
        self._items[key] = InlineCommandSpec(name=key, handler=handler, module_id=module_id, sandbox=sandbox)

    def unregister(self, name: str, *, module_id: str | None = None) -> bool:
        key = name.casefold()
        current = self._items.get(key)
        if current is None or current.module_id != module_id:
            return False
        del self._items[key]
        return True

    def resolve(self, name: str) -> InlineCommandSpec | None:
        return self._items.get(name.casefold())

    def names(self) -> tuple[str, ...]:
        return tuple(self._items)

    def items(self) -> tuple[InlineCommandSpec, ...]:
        return tuple(self._items.values())

    def specs_of(self, module_id: str) -> tuple[InlineCommandSpec, ...]:
        return tuple(spec for spec in self._items.values() if spec.module_id == module_id)
