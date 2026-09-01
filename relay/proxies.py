from __future__ import annotations

import secrets
from typing import Any, Awaitable, Callable

from .caps import MT_DESTRUCTIVE, MT_BLOCKED


class Gateway:
    """Single funnel every module-side Telegram action goes through."""

    def __init__(self, host: Any, module_id: str) -> None:
        self._host = host
        self._module_id = module_id

    def _check(self, method: str) -> None:
        lowered = str(method).lower()
        if lowered in MT_DESTRUCTIVE or lowered in MT_BLOCKED:
            raise PermissionError(f"mt method is blocked by policy: {method}")

    async def call(self, method: str, kwargs: dict[str, Any] | None = None) -> Any:
        self._check(method)
        return await self._host.call(self._module_id, "mt", {"method": method, "kwargs": kwargs or {}})

    async def send(self, method: str, **kwargs: Any) -> Any:
        return await self.call(method, kwargs)

    async def get(self, method: str, **kwargs: Any) -> Any:
        return await self.call(method, kwargs)

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        async def _method(**kwargs: Any) -> Any:
            return await self.call(name, kwargs)

        return _method


class InlineHelper:
    """Convenience wrapper around the owner inline bot: answer queries with rich articles."""

    def __init__(self, inline_manager: Any) -> None:
        self._inline = inline_manager

    def article(self, result_id: str, title: str, text: str, **kw: Any) -> dict[str, Any]:
        from goygram.types import InlineObj

        kbd = kw.pop("kbd", kw.pop("reply_markup", None))
        result = InlineObj.article(result_id, title, text, **kw)
        if kbd is not None:
            result["reply_markup"] = kbd.to_dict() if hasattr(kbd, "to_dict") else kbd
        return result

    async def answer(self, query: Any, results: list[dict[str, Any]], **kw: Any) -> Any:
        return await query.answer(results, **kw)

    def command(self, result_id: str, title: str, text: str, **kw: Any) -> dict[str, Any]:
        return self.article(result_id, title, text, **kw)

    async def show(self, query: Any, title: str, text: str, **kw: Any) -> Any:
        return await self.answer(query, [self.article(secrets.token_hex(6), title, text, **kw)])

    async def from_query(self, query: Any, title: str, text: str, **kw: Any) -> Any:
        return await self.show(query, title, text, **kw)


class UiHelper:
    """Build keyboards whose callback data is sealed by the kernel; modules never touch raw handles."""

    def __init__(self, module_id: str, store: Any, owner_id: int | None, chat_id: int | str | None, message_id: int, router: Any = None) -> None:
        self._module_id = module_id
        self._store = store
        self._router = router
        self._owner_id = owner_id
        self._chat_id = chat_id
        self._message_id = message_id
        self._actions: dict[str, Callable[[Any, Any], Any]] = {}

    def on(self, handler: Callable[[Any, Any], Any]) -> str:
        if self._router is None:
            raise RuntimeError("callback router is not available")
        action_id = self._router.register_module_action(self._module_id, handler)
        self._actions[action_id] = handler
        return action_id

    def button(self, text: str, action: str | Callable[[Any, Any], Any], payload: Any = None) -> dict[str, str]:
        from hotaru.callbacks import CallbackBinding

        if callable(action):
            action_id = self.on(action)
        else:
            action_id = str(action)
            if self._router is None or not self._router.module_action_exists(self._module_id, action_id):
                raise RuntimeError("unknown module callback; register it with ui.on(handler)")
        if self._router is None:
            raise RuntimeError("callback router is not available")
        handle = self._router.issue_module(
            self._module_id,
            action_id,
            CallbackBinding(self._owner_id or 0, self._chat_id or 0, self._message_id),
            payload,
        )
        return {"text": text, "callback_data": handle}

    def row(self, *buttons: dict[str, str]) -> list[dict[str, str]]:
        return [b for b in buttons]

    def rows(self, *rows: list[dict[str, str]]) -> list[list[dict[str, str]]]:
        return [list(row) for row in rows]

    def button_url(self, text: str, url: str) -> dict[str, str]:
        if not url.startswith(("https://", "tg://")):
            raise ValueError("button URL must use https or tg scheme")
        return {"text": text, "url": url}

    def close(self, text: str = "Close") -> dict[str, str]:
        async def handler(callback: Any, payload: Any) -> Any:
            return await callback.delete()

        return self.button(text, handler)

    def actions(self) -> dict[str, Callable[[Any, Any], Any]]:
        return dict(self._actions)


async def _noop(callback: Any, payload: Any) -> None:
    return None
