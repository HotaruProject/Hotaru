from __future__ import annotations

import html
import secrets
from typing import Any, Awaitable, Callable

from .caps import MT_DESTRUCTIVE, MT_BLOCKED


class HtmlHelper:
    def escape(self, value: Any) -> str:
        return html.escape(str(value), quote=False)

    def bold(self, value: Any) -> str:
        return f"<b>{self.escape(value)}</b>"

    def italic(self, value: Any) -> str:
        return f"<i>{self.escape(value)}</i>"

    def code(self, value: Any) -> str:
        return f"<code>{self.escape(value)}</code>"

    def underline(self, value: Any) -> str:
        return f"<u>{self.escape(value)}</u>"

    def quote(self, value: Any) -> str:
        return f"<blockquote>{self.escape(value)}</blockquote>"

    def link(self, label: Any, url: str) -> str:
        return f'<a href="{html.escape(url, quote=True)}">{self.escape(label)}</a>'

    def pre(self, value: Any, language: str | None = None) -> str:
        attr = f' class="language-{html.escape(language, quote=True)}"' if language else ""
        return f"<pre{attr}>{self.escape(value)}</pre>"


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

    async def send_message(self, text: str, **kwargs: Any) -> Any:
        return await self.call("messages.sendMessage", {"message": text, **kwargs})

    async def send_media(self, media: Any, caption: str = "", **kwargs: Any) -> Any:
        return await self.call("messages.sendMedia", {"media": media, "message": caption, **kwargs})

    async def send_rich(self, html_text: str | dict[str, Any], **kwargs: Any) -> Any:
        rich = {"_": "inputRichMessageHTML", "html": html_text} if isinstance(html_text, str) else html_text
        return await self.call("messages.sendMessage", {"rich_message": rich, **kwargs})

    async def edit_message(self, message_id: int, text: str, **kwargs: Any) -> Any:
        return await self.call("messages.editMessage", {"id": message_id, "message": text, **kwargs})

    async def edit_rich(self, message_id: int, html_text: str | dict[str, Any], **kwargs: Any) -> Any:
        rich = {"_": "inputRichMessageHTML", "html": html_text} if isinstance(html_text, str) else html_text
        return await self.call("messages.editMessage", {"id": message_id, "rich_message": rich, **kwargs})

    async def delete_message(self, message_id: int, **kwargs: Any) -> Any:
        return await self.call("messages.deleteMessages", {"id": [message_id], **kwargs})

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        async def _method(**kwargs: Any) -> Any:
            return await self.call(name, kwargs)

        return _method


class RichGateway:
    def __init__(self, tg: Gateway, source: Any = None) -> None:
        self._tg = tg
        self._source = source

    @staticmethod
    def html(value: str, *, rtl: bool = False, noautolink: bool = False, files: Any = None) -> dict[str, Any]:
        result: dict[str, Any] = {"_": "inputRichMessageHTML", "html": str(value)}
        if rtl:
            result["rtl"] = True
        if noautolink:
            result["noautolink"] = True
        if files is not None:
            result["files"] = files
        return result

    @staticmethod
    def media(media_id: str, media: Any) -> dict[str, Any]:
        return {"id": media_id, "media": media}

    def input(self, value: str | dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else self.html(value, **kwargs)

    async def send(self, html_text: str | dict[str, Any], *, peer: Any = None, reply_to: Any = None, buttons: Any = None, **kwargs: Any) -> Any:
        data = {"message": "", "rich_message": self.input(html_text), **kwargs}
        target = peer if peer is not None else getattr(self._source, "chat_id", None)
        if target is not None:
            data["peer"] = target
        if reply_to is not None:
            data["reply_to"] = reply_to
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self._tg.call("messages.sendMessage", data)

    async def send_draft(self, html_text: str | dict[str, Any], *, peer: Any = None, draft_id: int, **kwargs: Any) -> Any:
        return await self.draft(html_text, peer=peer, draft_id=draft_id, **kwargs)

    async def save(self, html_text: str | dict[str, Any], *, peer: Any = None, reply_to: Any = None, **kwargs: Any) -> Any:
        return await self.save_draft(html_text, peer=peer, reply_to=reply_to, **kwargs)

    async def edit(self, html_text: str | dict[str, Any], *, message_id: int | None = None, peer: Any = None, buttons: Any = None, **kwargs: Any) -> Any:
        mid = message_id if message_id is not None else getattr(self._source, "id", None)
        target = peer if peer is not None else getattr(self._source, "chat_id", None)
        if mid is None or target is None:
            raise ValueError("rich edit requires message_id and peer")
        data = {"peer": target, "id": int(mid), "rich_message": self.input(html_text), **kwargs}
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self._tg.call("messages.editMessage", data)

    async def update(self, html_text: str | dict[str, Any], **kwargs: Any) -> Any:
        return await self.edit(html_text, **kwargs)

    async def send_media(self, media: Any, html_text: str | dict[str, Any] = "", *, peer: Any = None, **kwargs: Any) -> Any:
        target = peer if peer is not None else getattr(self._source, "chat_id", None)
        data = {"peer": target, "media": media, "message": "", "rich_message": self.input(html_text), **kwargs}
        return await self._tg.call("messages.sendMedia", data)

    async def draft(self, html_text: str | dict[str, Any], *, peer: Any = None, draft_id: int, **kwargs: Any) -> Any:
        target = peer if peer is not None else getattr(self._source, "chat_id", None)
        return await self._tg.call("messages.sendMessageDraft", {"peer": target, "draft_id": draft_id, "rich_message": self.input(html_text), **kwargs})

    async def save_draft(self, html_text: str | dict[str, Any], *, peer: Any = None, reply_to: Any = None, **kwargs: Any) -> Any:
        target = peer if peer is not None else getattr(self._source, "chat_id", None)
        data = {"peer": target, "rich_message": self.input(html_text), **kwargs}
        if reply_to is not None:
            data["reply_to"] = reply_to
        return await self._tg.call("messages.saveDraft", data)

    async def get(self, message_id: int | None = None, *, peer: Any = None) -> Any:
        target = peer if peer is not None else getattr(self._source, "chat_id", None)
        mid = message_id if message_id is not None else getattr(self._source, "id", None)
        return await self._tg.call("messages.getRichMessage", {"peer": target, "id": mid})

    async def translate(self, language: str, message_ids: list[int] | None = None, *, peer: Any = None, **kwargs: Any) -> Any:
        target = peer if peer is not None else getattr(self._source, "chat_id", None)
        ids = message_ids or [getattr(self._source, "id", 0)]
        return await self._tg.call("messages.translateRichMessage", {"peer": target, "id": ids, "to_lang": language, **kwargs})

    async def compose(self, html_text: str, **kwargs: Any) -> Any:
        return await self._tg.call("messages.composeRichMessageWithAI", {"text": self.input(html_text), **kwargs})

    async def typing(self, *, peer: Any = None, draft_id: int | None = None, **kwargs: Any) -> Any:
        target = peer if peer is not None else getattr(self._source, "chat_id", None)
        action = {"_": "inputSendMessageRichMessageDraftAction", "random_id": draft_id or secrets.randbits(63), "rich_message": self.html("<tg-thinking>Thinking...</tg-thinking>")}
        return await self._tg.call("messages.setTyping", {"peer": target, "action": action, **kwargs})

    async def ephemeral(self, html_text: str, *, peer: Any = None, receiver_id: Any = None, **kwargs: Any) -> Any:
        target = peer if peer is not None else getattr(self._source, "chat_id", None)
        return await self._tg.call("ephemeral.sendMessage", {"peer": target, "receiver_id": receiver_id or target, "message": "", "rich_message": self.html(html_text), "random_id": secrets.randbits(63), **kwargs})

    async def edit_inline(self, inline_id: Any, html_text: str | dict[str, Any], *, buttons: Any = None, **kwargs: Any) -> Any:
        data = {"id": inline_id, "rich_message": self.input(html_text), **kwargs}
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self._tg.call("messages.editInlineBotMessage", data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tg, name)


class BotGateway:
    def __init__(self, inline_manager: Any) -> None:
        self._manager = inline_manager

    async def call(self, method: str, **kwargs: Any) -> Any:
        app = getattr(self._manager, "bot_app", None)
        if app is None:
            raise RuntimeError("inline bot is not ready")
        return await app.bot_req(method, **kwargs)

    async def send(self, method: str, **kwargs: Any) -> Any:
        return await self.call(method, **kwargs)

    async def rich_send(self, chat_id: int | str, html_text: str, *, buttons: Any = None, **kwargs: Any) -> Any:
        data = {"chat_id": chat_id, "rich_message": {"_": "inputRichMessageHTML", "html": html_text}, **kwargs}
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self.call("sendRichMessage", **data)

    async def rich_edit(self, chat_id: int | str, message_id: int, html_text: str, *, buttons: Any = None, **kwargs: Any) -> Any:
        data = {"chat_id": chat_id, "message_id": message_id, "rich_message": {"_": "inputRichMessageHTML", "html": html_text}, **kwargs}
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self.call("editMessageText", **data)

    async def rich_draft(self, chat_id: int | str, html_text: str, *, draft_id: int, **kwargs: Any) -> Any:
        return await self.call("sendRichMessageDraft", chat_id=chat_id, draft_id=draft_id, rich_message={"_": "inputRichMessageHTML", "html": html_text}, **kwargs)

    async def inline_answer(self, query: Any, results: list[dict[str, Any]], **kwargs: Any) -> Any:
        return await query.answer(results, **kwargs)

    async def inline_edit(self, inline_message_id: str, html_text: str, *, buttons: Any = None, **kwargs: Any) -> Any:
        data = {"inline_message_id": inline_message_id, "rich_message": {"_": "inputRichMessageHTML", "html": html_text}, **kwargs}
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self.call("editMessageText", **data)

    def reply(self, chat_id: int | str, message_id: int, text: str, **kwargs: Any) -> Any:
        return self.send("sendMessage", chat_id=chat_id, text=text, **kwargs)

    def __getattr__(self, name: str) -> Any:
        async def method(**kwargs: Any) -> Any:
            return await self.call(name, **kwargs)
        return method


class InlineHelper:
    """Convenience wrapper around the owner inline bot: answer queries with rich articles."""

    def __init__(self, inline_manager: Any = None) -> None:
        self._inline = inline_manager

    def article(self, result_id: str, title: str, text: str, **kw: Any) -> dict[str, Any]:
        from goygram.types import InlineObj

        kbd = kw.pop("kbd", kw.pop("reply_markup", None))
        kw.setdefault("parse_mode", "HTML")
        result = InlineObj.article(result_id, title, text, **kw)
        if kbd is not None:
            result["reply_markup"] = kbd.to_dict() if hasattr(kbd, "to_dict") else kbd
        return result

    async def answer(self, query: Any, results: list[dict[str, Any]], **kw: Any) -> Any:
        return await query.answer(results, **kw)

    def rich(self, result_id: str, title: str, html_text: str, *, buttons: Any = None, **kw: Any) -> dict[str, Any]:
        from goygram.types import InlineObj
        result = InlineObj.article(result_id, title, html_text)
        result["input_message_content"] = {"rich_message": {"_": "inputRichMessageHTML", "html": html_text}}
        if buttons is not None:
            result["reply_markup"] = {"inline_keyboard": buttons}
        return result

    def command(self, result_id: str, title: str, text: str, **kw: Any) -> dict[str, Any]:
        return self.article(result_id, title, text, **kw)

    async def show(self, query: Any, title: str, text: str, **kw: Any) -> Any:
        return await self.answer(query, [self.article(secrets.token_hex(6), title, text, **kw)])

    async def from_query(self, query: Any, title: str, text: str, **kw: Any) -> Any:
        return await self.show(query, title, text, **kw)

    def form(self, text: str, buttons: Any = None, **kw: Any) -> dict[str, Any]:
        kw.setdefault("parse_mode", "HTML")
        result = self.article(secrets.token_hex(6), "Hotaru form", text, **kw)
        if buttons is not None:
            result["reply_markup"] = {"inline_keyboard": buttons}
        return result

    def html(self, text: str, **kw: Any) -> dict[str, Any]:
        return self.rich("hotaru", "Hotaru", text, **kw)

    @staticmethod
    def escape(value: Any) -> str:
        return html.escape(str(value), quote=False)


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

    def button(self, text: str, action: str | Callable[[Any, Any], Any], payload: Any = None, *, style: str | None = None) -> dict[str, str]:
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
        result = {"text": text, "callback_data": handle}
        if style is not None:
            if style not in {"primary", "success", "danger"}:
                raise ValueError("button style is invalid")
            result["style"] = style
        return result

    def callback(self, text: str, handler: Callable[[Any, Any], Any], payload: Any = None, *, style: str | None = None) -> dict[str, str]:
        return self.button(text, handler, payload, style=style)

    def primary(self, text: str, handler: Callable[[Any, Any], Any], payload: Any = None) -> dict[str, str]:
        return self.button(text, handler, payload, style="primary")

    def success(self, text: str, handler: Callable[[Any, Any], Any], payload: Any = None) -> dict[str, str]:
        return self.button(text, handler, payload, style="success")

    def danger(self, text: str, handler: Callable[[Any, Any], Any], payload: Any = None) -> dict[str, str]:
        return self.button(text, handler, payload, style="danger")

    def row(self, *buttons: dict[str, str]) -> list[dict[str, str]]:
        return [b for b in buttons]

    def rows(self, *rows: list[dict[str, str]]) -> list[list[dict[str, str]]]:
        return [list(row) for row in rows]

    def button_url(self, text: str, url: str) -> dict[str, str]:
        if not url.startswith(("https://", "tg://")):
            raise ValueError("button URL must use https or tg scheme")
        return {"text": text, "url": url}

    def url(self, text: str, url: str) -> dict[str, str]:
        return self.button_url(text, url)

    def close(self, text: str = "Close") -> dict[str, str]:
        async def handler(callback: Any, payload: Any) -> Any:
            return await callback.delete()

        return self.button(text, handler, style="danger")

    def back(self, text: str, handler: Callable[[Any, Any], Any], payload: Any = None) -> dict[str, str]:
        return self.button(text, handler, payload)

    def confirm(self, text: str, handler: Callable[[Any, Any], Any], payload: Any = None) -> dict[str, str]:
        return self.button(text, handler, payload, style="success")

    def cancel(self, text: str, handler: Callable[[Any, Any], Any], payload: Any = None) -> dict[str, str]:
        return self.button(text, handler, payload, style="danger")

    def form(self, text: str, *rows: Any) -> dict[str, Any]:
        normalized = [list(row) if isinstance(row, (list, tuple)) else [row] for row in rows]
        return {"text": text, "buttons": normalized}

    def screen(self, text: str, *rows: Any) -> dict[str, Any]:
        return self.form(text, *rows)

    def switch(self, text: str, query: str, *, same_chat: bool = True) -> dict[str, str]:
        return {"text": text, "switch_inline_query_current_chat" if same_chat else "switch_inline_query": query}

    def grid(self, *buttons: dict[str, str], columns: int = 2) -> list[list[dict[str, str]]]:
        if columns < 1:
            raise ValueError("columns must be positive")
        return [list(buttons[i:i + columns]) for i in range(0, len(buttons), columns)]

    def keyboard(self, *rows: list[dict[str, str]]) -> dict[str, Any]:
        return {"inline_keyboard": [list(row) for row in rows]}

    def actions(self) -> dict[str, Callable[[Any, Any], Any]]:
        return dict(self._actions)


async def _noop(callback: Any, payload: Any) -> None:
    return None
