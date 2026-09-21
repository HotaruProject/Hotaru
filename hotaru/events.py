from __future__ import annotations

import inspect
from typing import Awaitable, Callable, Protocol, Union

EventHandler = Callable[[object], Union[Awaitable[object], object]]
ErrorHandler = Callable[[Exception], Union[Awaitable[object], object]]


class EventSource(Protocol):
    def on_poll(self, handler: EventHandler) -> object: ...
    def on_member(self, handler: EventHandler) -> object: ...
    def on_update(self, handler: EventHandler) -> object: ...
    def on_msg(self, handler: EventHandler) -> object: ...
    def on_edit(self, handler: EventHandler) -> object: ...
    def on_cb(self, handler: EventHandler) -> object: ...


class EventRouter:
    def __init__(self, error_sink: ErrorHandler | None = None) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self.error_sink = error_sink

    def register(self, family: str, handler: EventHandler) -> None:
        if not family or not callable(handler):
            raise ValueError("event handler is invalid")
        self._handlers.setdefault(family, []).append(handler)

    async def dispatch(self, family: str, event: object) -> tuple[object, ...]:
        handlers = tuple(self._handlers.get(family, ()))
        if family != "update":
            handlers += tuple(self._handlers.get("update", ()))
        results: list[object] = []
        for handler in handlers:
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    result = await result
                results.append(result)
            except Exception as exc:
                if self.error_sink is not None:
                    error = self.error_sink(exc)
                    if inspect.isawaitable(error):
                        await error
        return tuple(results)

    def attach_aux(self, app: EventSource) -> None:
        app.on_poll(lambda event: self.dispatch("poll", event))
        app.on_member(lambda event: self.dispatch("member", event))
        app.on_update(lambda event: self.dispatch("update", event))

    def attach(self, app: EventSource) -> None:
        app.on_msg(lambda event: self.dispatch("new", event))
        app.on_edit(lambda event: self.dispatch("edit", event))
        app.on_cb(lambda event: self.dispatch("callback", event))
        app.on_poll(lambda event: self.dispatch("poll", event))
        app.on_member(lambda event: self.dispatch("member", event))
        app.on_update(lambda event: self.dispatch("update", event))
