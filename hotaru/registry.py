from dataclasses import dataclass
from typing import Awaitable, Callable

from .commands import CommandInvocation

Handler = Callable[..., Awaitable[object] | object]

KERNEL_MODULES: set[str] = set()


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    handler: Handler
    module_id: str
    kernel: bool = False
    sandbox: bool = False


@dataclass(frozen=True, slots=True)
class WatcherSpec:
    name: str
    handler: Handler
    module_id: str
    sandbox: bool = False

@dataclass(frozen=True, slots=True)
class TaskSpec:
    name: str
    handler: Handler
    module_id: str
    interval: float
    autostart: bool

class CommandRegistry:
    def __init__(self) -> None:
        self._items: dict[str, CommandSpec] = {}
        self._watchers: list[WatcherSpec] = []
        self._aliases: dict[str, str] = {}

    def register(
        self,
        name: str,
        handler: Handler,
        *,
        kernel: bool = False,
        module_id: str | None = None,
        sandbox: bool = False,
    ) -> None:
        if not name.isidentifier():
            raise ValueError("command name must be an identifier")
        if not isinstance(module_id, str) or not module_id:
            raise ValueError("every command must belong to a module")
        key = name.casefold()
        current = self._items.get(key)
        if current is not None and (current.kernel or current.module_id in KERNEL_MODULES):
            raise ValueError(f"kernel command is reserved: {key}")
        if current is not None:
            raise ValueError(f"command already registered: {key}")
        self._items[key] = CommandSpec(name=key, handler=handler, kernel=kernel, module_id=module_id, sandbox=sandbox)

    def register_kernel_module(self, module_id: str) -> None:
        KERNEL_MODULES.add(module_id.casefold())

    def register_watcher(self, name: str, handler: Handler, module_id: str, sandbox: bool = False) -> None:
        self._watchers.append(WatcherSpec(name, handler, module_id, sandbox))

    def unregister_watcher(self, name: str, module_id: str) -> bool:
        original_len = len(self._watchers)
        self._watchers = [w for w in self._watchers if not (w.name == name and w.module_id == module_id)]
        return len(self._watchers) < original_len

    def get_watchers(self) -> tuple[WatcherSpec, ...]:
        return tuple(self._watchers)

    def register_alias(self, alias: str, command: str) -> None:
        self._aliases[alias.casefold()] = command.casefold()

    def unregister_alias(self, alias: str) -> bool:
        return self._aliases.pop(alias.casefold(), None) is not None

    def unregister(self, name: str, *, module_id: str | None = None) -> bool:
        key = name.casefold()
        current = self._items.get(key)
        if current is None or current.kernel or current.module_id in KERNEL_MODULES or current.module_id != module_id:
            return False
        del self._items[key]
        return True

    def unregister_kernel(self, name: str, *, module_id: str | None = None) -> bool:
        key = name.casefold()
        current = self._items.get(key)
        if current is None or current.module_id != module_id:
            return False
        del self._items[key]
        return True

    def resolve(self, invocation: CommandInvocation) -> CommandSpec | None:
        name = invocation.name.casefold()
        if name in self._aliases:
            name = self._aliases[name]
        return self._items.get(name)

    def resolve_name(self, name: str) -> CommandSpec | None:
        name = name.casefold()
        if name in self._aliases:
            name = self._aliases[name]
        return self._items.get(name)

    def spec_of(self, module_id: str) -> tuple[CommandSpec, ...]:
        return tuple(spec for spec in self._items.values() if spec.module_id == module_id)

    def names(self) -> tuple[str, ...]:
        return tuple(self._items)
