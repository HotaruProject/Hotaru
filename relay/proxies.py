from __future__ import annotations

import html
import secrets
from typing import Any, Awaitable, Callable

from .caps import MT_BLOCKED, normalize_method
from .denylist import payload_hits_blocked
from .firewall import trusted_scope


class AssetsHelper:
    def __init__(self, context: Any) -> None:
        self._context = context

    async def upload(self, file: Any, filename: str | None = None) -> Any:
        return await self._context.tg.send_media(file, caption=filename or "", peer="me")

    async def download(self, message: Any, destination: str | Any | None = None) -> Any:
        # Assuming message is an object with .download() method
        return await self._context.message.__class__(message, self._context.responses).download(destination)


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
    def __init__(self, host: Any, module_id: str) -> None:
        self._host = host
        self._module_id = module_id

    def _check(self, method: str) -> None:
        if not isinstance(method, str) or not method.strip():
            raise PermissionError("mt method name is required")
        lowered = method.strip().lower()
        canonical = normalize_method(lowered).lower()
        if canonical.startswith(("auth.", "phone.")) or canonical in MT_BLOCKED:
            raise PermissionError(f"mt method is blocked by policy: {method}")

    async def call(self, method: str, kwargs: dict[str, Any] | None = None) -> Any:
        self._check(method)
        if payload_hits_blocked(kwargs):
            raise PermissionError("mt kwargs target a denied peer")
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
        rich = {"_": "inputRichMessageHTML", "html": __import__("re").sub(r"\n(?![^<]*>)", "<br>", html_text)} if isinstance(html_text, str) else html_text
        return await self.call("messages.sendMessage", {"rich_message": rich, **kwargs})

    async def edit_message(self, message_id: int, text: str, **kwargs: Any) -> Any:
        return await self.call("messages.editMessage", {"id": message_id, "message": text, **kwargs})

    async def edit_rich(self, message_id: int, html_text: str | dict[str, Any], **kwargs: Any) -> Any:
        rich = {"_": "inputRichMessageHTML", "html": __import__("re").sub(r"\n(?![^<]*>)", "<br>", html_text)} if isinstance(html_text, str) else html_text
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
        result: dict[str, Any] = {"_": "inputRichMessageHTML", "html": __import__("re").sub(r"\n(?![^<]*>)", "<br>", str(value))}
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

    @staticmethod
    def block(kind: str, **fields: Any) -> dict[str, Any]:
        return {"type": kind, **fields}

    def paragraph(self, html_text: str) -> dict[str, Any]:
        return self.block("paragraph", text=html_text)

    def heading(self, html_text: str, *, level: int = 1) -> dict[str, Any]:
        return self.block("section_heading", text=html_text, level=level)

    def preformatted(self, text: str, *, language: str = "") -> dict[str, Any]:
        return self.block("preformatted", text=text, language=language)

    def divider(self) -> dict[str, Any]:
        return self.block("divider")

    def list(self, items: list[Any], *, ordered: bool = False) -> dict[str, Any]:
        return self.block("list", items=[self.block("list_item", text=item) if isinstance(item, str) else item for item in items], ordered=ordered)

    def quote(self, html_text: str, *, expandable: bool = False) -> dict[str, Any]:
        return self.block("expandable_block_quotation" if expandable else "block_quotation", text=html_text)

    def details(self, summary: str, blocks: list[dict[str, Any]], *, open: bool = False) -> dict[str, Any]:
        return self.block("details", summary=summary, blocks=blocks, is_open=open)

    def table(self, cells: list[list[Any]], *, bordered: bool = True, striped: bool = False, compact: bool = False, caption: Any = None) -> dict[str, Any]:
        rows = [[self.block("table_cell", text=cell) if isinstance(cell, str) else cell for cell in row] for row in cells]
        return self.block("table", cells=rows, is_bordered=bordered, is_striped=striped, is_compact=compact, caption=caption)

    def location(self, latitude: float, longitude: float, *, zoom: int = 15, width: int | None = None, height: int | None = None, caption: Any = None) -> dict[str, Any]:
        return self.map(latitude, longitude, zoom=zoom, width=width, height=height, caption=caption)

    def map(self, latitude: float, longitude: float, *, zoom: int = 15, width: int | None = None, height: int | None = None, caption: Any = None) -> dict[str, Any]:
        result = self.block("map", location={"latitude": latitude, "longitude": longitude}, zoom=zoom)
        result["latitude"] = latitude
        result["longitude"] = longitude
        if width is not None:
            result["width"] = width
        if height is not None:
            result["height"] = height
        if caption is not None:
            result["caption"] = caption
        return result

    def photo(self, media: Any, *, caption: Any = None) -> dict[str, Any]:
        return self.block("photo", photo=media, caption=caption)

    def video(self, media: Any, *, caption: Any = None) -> dict[str, Any]:
        return self.block("video", video=media, caption=caption)

    def audio(self, media: Any, *, caption: Any = None) -> dict[str, Any]:
        return self.block("audio", audio=media, caption=caption)

    def document(self, media: Any, *, caption: Any = None) -> dict[str, Any]:
        return self.block("document", document=media, caption=caption)

    def collage(self, items: list[Any]) -> dict[str, Any]:
        return self.block("collage", items=items)

    def slideshow(self, items: list[Any], *, autoplay: bool = False) -> dict[str, Any]:
        return self.block("slideshow", items=items, autoplay=autoplay)

    def buttons(self, buttons: list[Any], *, align: str = "left") -> dict[str, Any]:
        return self.block("buttons", buttons=buttons, align=align)

    def input(self, value: str | dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else self.html(value, **kwargs)

    async def send(self, html_text: str | dict[str, Any], *, peer: Any = None, reply_to: Any = None, buttons: Any = None, **kwargs: Any) -> Any:
        data = {"message": "", "random_id": secrets.randbits(63), "rich_message": self.input(html_text), **kwargs}
        target = peer if peer is not None else getattr(self._source, "chat_id", None)
        if target is not None:
            data["peer"] = target
        if reply_to is not None:
            data["reply_to"] = reply_to
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self._tg.call("messages.sendMessage", data)

    async def send_blocks(self, blocks: list[dict[str, Any]], *, peer: Any = None, **kwargs: Any) -> Any:
        target = peer if peer is not None else getattr(self._source, "chat_id", None)
        return await self.send(self._blocks_html(blocks), peer=target, **kwargs)

    def _blocks_html(self, blocks: list[dict[str, Any]]) -> str:
        result = []
        for block in blocks:
            kind = block.get("type")
            if kind == "section_heading":
                result.append(f"<h{max(1, min(6, int(block.get('level', 1))))}>{block.get('text', '')}</h{max(1, min(6, int(block.get('level', 1))))}>")
            elif kind == "paragraph":
                result.append(f"<p>{block.get('text', '')}</p>")
            elif kind == "preformatted":
                result.append(f"<pre><code>{html.escape(str(block.get('text', '')), quote=False)}</code></pre>")
            elif kind == "divider":
                result.append("<hr>")
            elif kind == "list":
                tag = "ol" if block.get("ordered") else "ul"
                items = "".join(f"<li>{item.get('text', '') if isinstance(item, dict) else item}</li>" for item in block.get("items", []))
                result.append(f"<{tag}>{items}</{tag}>")
            elif kind in {"block_quotation", "expandable_block_quotation"}:
                attr = " expandable" if kind.startswith("expandable") else ""
                result.append(f"<blockquote{attr}>{block.get('text', '')}</blockquote>")
            elif kind == "details":
                attr = " open" if block.get("is_open") else ""
                result.append(f"<details{attr}><summary>{block.get('summary', '')}</summary>{self._blocks_html(block.get('blocks', []))}</details>")
            elif kind == "table":
                rows = []
                for row in block.get("cells", []):
                    cells = "".join(f"<td>{cell.get('text', '') if isinstance(cell, dict) else cell}</td>" for cell in row)
                    rows.append(f"<tr>{cells}</tr>")
                result.append(f"<table>{''.join(rows)}</table>")
            elif kind == "map":
                loc = block.get("location", {})
                result.append(f"<tg-map latitude=\"{block.get('latitude', loc.get('latitude'))}\" longitude=\"{block.get('longitude', loc.get('longitude'))}\" zoom=\"{block.get('zoom', 15)}\">{block.get('caption') or ''}</tg-map>")
            elif kind == "photo":
                result.append(f"<img src=\"{html.escape(str(block.get('photo', '')), quote=True)}\">{block.get('caption') or ''}")
            elif kind == "video":
                result.append(f"<video src=\"{html.escape(str(block.get('video', '')), quote=True)}\">{block.get('caption') or ''}</video>")
            elif kind == "audio":
                result.append(f"<audio src=\"{html.escape(str(block.get('audio', '')), quote=True)}\">{block.get('caption') or ''}</audio>")
            elif kind == "document":
                result.append(f"<tg-document src=\"{html.escape(str(block.get('document', '')), quote=True)}\">{block.get('caption') or ''}</tg-document>")
            elif kind == "buttons":
                result.append("<tg-button-row>" + "".join(str(item) for item in block.get("buttons", [])) + "</tg-button-row>")
            else:
                result.append(str(block.get("text", "")))
        return "".join(result)

    async def send_photo(self, photo: Any, caption: str = "", **kwargs: Any) -> Any:
        return await self.send_media(photo, caption, **kwargs)

    async def send_video(self, video: Any, caption: str = "", **kwargs: Any) -> Any:
        return await self.send_media(video, caption, **kwargs)

    async def send_audio(self, audio: Any, caption: str = "", **kwargs: Any) -> Any:
        return await self.send_media(audio, caption, **kwargs)

    async def send_document(self, document: Any, caption: str = "", **kwargs: Any) -> Any:
        return await self.send_media(document, caption, **kwargs)

    async def send_html(self, html_text: str, *, peer: Any = None, **kwargs: Any) -> Any:
        return await self.send(html_text, peer=peer, **kwargs)

    async def edit_html(self, html_text: str, **kwargs: Any) -> Any:
        return await self.edit(html_text, **kwargs)

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
        data = {"peer": target, "media": media, "message": "", "random_id": secrets.randbits(63), "rich_message": self.input(html_text), **kwargs}
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
        with trusted_scope():
            return await app.bot_req(method, **kwargs)

    async def send(self, method: str, **kwargs: Any) -> Any:
        return await self.call(method, **kwargs)

    async def rich_send(self, chat_id: int | str, html_text: str, *, buttons: Any = None, **kwargs: Any) -> Any:
        data = {"chat_id": chat_id, "rich_message": {"_": "inputRichMessageHTML", "html": __import__("re").sub(r"\n(?![^<]*>)", "<br>", html_text)}, **kwargs}
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self.call("sendRichMessage", **data)

    async def send_message(self, chat_id: int | str, text: str, *, buttons: Any = None, **kwargs: Any) -> Any:
        data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", **kwargs}
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self.call("sendMessage", **data)

    async def send_photo(self, chat_id: int | str, photo: Any, *, caption: str = "", buttons: Any = None, **kwargs: Any) -> Any:
        data = {"chat_id": chat_id, "photo": photo, "caption": caption, "parse_mode": "HTML", **kwargs}
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self.call("sendPhoto", **data)

    async def send_video(self, chat_id: int | str, video: Any, *, caption: str = "", buttons: Any = None, **kwargs: Any) -> Any:
        data = {"chat_id": chat_id, "video": video, "caption": caption, "parse_mode": "HTML", **kwargs}
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self.call("sendVideo", **data)

    async def send_document(self, chat_id: int | str, document: Any, *, caption: str = "", buttons: Any = None, **kwargs: Any) -> Any:
        data = {"chat_id": chat_id, "document": document, "caption": caption, "parse_mode": "HTML", **kwargs}
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self.call("sendDocument", **data)

    async def rich_edit(self, chat_id: int | str, message_id: int, html_text: str, *, buttons: Any = None, **kwargs: Any) -> Any:
        data = {"chat_id": chat_id, "message_id": message_id, "rich_message": {"_": "inputRichMessageHTML", "html": __import__("re").sub(r"\n(?![^<]*>)", "<br>", html_text)}, **kwargs}
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self.call("editMessageText", **data)

    async def rich_draft(self, chat_id: int | str, html_text: str, *, draft_id: int, **kwargs: Any) -> Any:
        return await self.call("sendRichMessageDraft", chat_id=chat_id, draft_id=draft_id, rich_message={"_": "inputRichMessageHTML", "html": __import__("re").sub(r"\n(?![^<]*>)", "<br>", html_text)}, **kwargs)

    async def inline_answer(self, query: Any, results: list[dict[str, Any]], **kwargs: Any) -> Any:
        return await query.answer(results, **kwargs)

    async def inline_edit(self, inline_message_id: str, html_text: str, *, buttons: Any = None, **kwargs: Any) -> Any:
        data = {"inline_message_id": inline_message_id, "rich_message": {"_": "inputRichMessageHTML", "html": __import__("re").sub(r"\n(?![^<]*>)", "<br>", html_text)}, **kwargs}
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

    def rich_article(self, title: str, html_text: str, *, result_id: str | None = None, description: str | None = None, buttons: Any = None, **kw: Any) -> dict[str, Any]:
        return self.rich(result_id or secrets.token_urlsafe(10), title, html_text, buttons=buttons, description=description, **kw)

    async def answer(self, query: Any, results: list[dict[str, Any]], **kw: Any) -> Any:
        return await query.answer(results, **kw)

    def photo(self, result_id: str, title: str, photo: str, *, caption: str = "", buttons: Any = None, **kw: Any) -> dict[str, Any]:
        result = {"type": "photo", "id": result_id, "title": title, "photo_url": photo, "thumbnail_url": photo, "caption": caption, "parse_mode": "HTML", **kw}
        self._attach(result, buttons)
        return result

    @staticmethod
    def _attach(result: dict[str, Any], buttons: Any) -> dict[str, Any]:
        if buttons is not None:
            result["reply_markup"] = {"inline_keyboard": buttons}
        return result

    def video(self, result_id: str, title: str, video: str, *, mime: str = "video/mp4", thumb: str | None = None, caption: str = "", buttons: Any = None, **kw: Any) -> dict[str, Any]:
        result = {"type": "video", "id": result_id, "title": title, "video_url": video, "mime_type": mime, "thumbnail_url": thumb or video, "caption": caption, "parse_mode": "HTML", **kw}
        return self._attach(result, buttons)

    def document(self, result_id: str, title: str, document: str, *, mime: str = "application/octet-stream", caption: str = "", buttons: Any = None, **kw: Any) -> dict[str, Any]:
        result = {"type": "document", "id": result_id, "title": title, "document_url": document, "mime_type": mime, "caption": caption, "parse_mode": "HTML", **kw}
        return self._attach(result, buttons)

    def gallery(self, items: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
        return list(items)

    def animation(self, result_id: str, title: str, animation: str, *, thumb: str | None = None, caption: str = "", buttons: Any = None, **kw: Any) -> dict[str, Any]:
        result = {"type": "mpeg4_gif", "id": result_id, "title": title, "mpeg4_url": animation, "mpeg4_width": kw.pop("width", 0), "mpeg4_height": kw.pop("height", 0), "mpeg4_duration": kw.pop("duration", 0), "thumbnail_url": thumb or animation, "caption": caption, "parse_mode": "HTML", **kw}
        return self._attach(result, buttons)

    def audio(self, result_id: str, title: str, audio: str, *, caption: str = "", buttons: Any = None, **kw: Any) -> dict[str, Any]:
        result = {"type": "audio", "id": result_id, "title": title, "audio_url": audio, "caption": caption, "parse_mode": "HTML", **kw}
        return self._attach(result, buttons)

    def voice(self, result_id: str, title: str, voice: str, *, caption: str = "", buttons: Any = None, **kw: Any) -> dict[str, Any]:
        result = {"type": "voice", "id": result_id, "title": title, "voice_url": voice, "caption": caption, "parse_mode": "HTML", **kw}
        return self._attach(result, buttons)

    def location(self, result_id: str, title: str, latitude: float, longitude: float, *, buttons: Any = None, **kw: Any) -> dict[str, Any]:
        result = {"type": "location", "id": result_id, "title": title, "latitude": latitude, "longitude": longitude, **kw}
        return self._attach(result, buttons)

    def contact(self, result_id: str, title: str, phone: str, first_name: str, *, last_name: str = "", buttons: Any = None, **kw: Any) -> dict[str, Any]:
        result = {"type": "contact", "id": result_id, "title": title, "phone_number": phone, "first_name": first_name, "last_name": last_name, **kw}
        return self._attach(result, buttons)

    def venue(self, result_id: str, title: str, latitude: float, longitude: float, address: str, *, provider: str = "", venue_id: str = "", buttons: Any = None, **kw: Any) -> dict[str, Any]:
        result = {"type": "venue", "id": result_id, "title": title, "latitude": latitude, "longitude": longitude, "address": address, "provider": provider, "venue_id": venue_id, **kw}
        return self._attach(result, buttons)

    def paginate(self, items: list[Any], page: int = 0, size: int = 5) -> tuple[list[Any], bool]:
        if size < 1 or page < 0:
            raise ValueError("page and size must be non-negative and positive")
        start = page * size
        return items[start:start + size], start + size < len(items)

    async def error(self, code: int, text: str, *, query: Any) -> Any:
        return await self.show(query, f"Error {code}", f"<b>{code}</b> {self.escape(text)}")

    async def e400(self, query: Any, text: str = "Bad request") -> Any:
        return await self.error(400, text, query=query)

    async def e403(self, query: Any, text: str = "Forbidden") -> Any:
        return await self.error(403, text, query=query)

    async def e404(self, query: Any, text: str = "Not found") -> Any:
        return await self.error(404, text, query=query)

    async def e500(self, query: Any, text: str = "Internal error") -> Any:
        return await self.error(500, text, query=query)

    def rich(self, result_id: str, title: str, html_text: str, *, buttons: Any = None, **kw: Any) -> dict[str, Any]:
        from goygram.types import InlineObj
        result = InlineObj.article(result_id, title, html_text)
        result["input_message_content"] = {"rich_message": {"_": "inputRichMessageHTML", "html": __import__("re").sub(r"\n(?![^<]*>)", "<br>", html_text)}}
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
        result = {"text": text, "callback_data": handle, "_action_id": action_id, "_payload": payload}
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

    def input(self, text: str, handler: Callable[[Any, Any], Any], payload: Any = None, *, placeholder: str = "", style: str | None = None) -> dict[str, Any]:
        return {"text": text, "input": placeholder, "handler": handler, "callback": handler, "payload": payload, "style": style}

    def actions(self) -> dict[str, Callable[[Any, Any], Any]]:
        return dict(self._actions)


class ModulesHelper:
    def __init__(self, host: Any) -> None:
        self._host = host

    async def list(self) -> Any:
        return await self._host.cap("modules", {"op": "list"})

    async def info(self, module_id: str) -> Any:
        return await self._host.cap("modules", {"op": "info", "module_id": module_id})

    async def hashes(self) -> Any:
        return await self._host.cap("modules", {"op": "hashes"})

    async def load(self, url: str | None = None, text: str | None = None, source: str | None = None) -> Any:
        payload: dict[str, Any] = {"op": "load"}
        if url is not None:
            payload["url"] = url
        if text is not None:
            payload["text"] = text
        if source is not None:
            payload["source"] = source
        return await self._host.cap("modules", payload)

    async def unload(self, module_id: str) -> Any:
        return await self._host.cap("modules", {"op": "unload", "module_id": module_id})

    async def reload(self, module_id: str) -> Any:
        return await self._host.cap("modules", {"op": "reload", "module_id": module_id})


async def _noop(callback: Any, payload: Any) -> None:
    return None
