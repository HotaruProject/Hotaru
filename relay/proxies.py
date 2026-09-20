from __future__ import annotations

import asyncio
import html
import secrets
import time
from typing import Any, Awaitable, Callable

from .caps import MT_BLOCKED, normalize_method
from .rpc import rpcname
from .denylist import payload_hits_blocked
from .firewall import trusted_scope
from goygram.errors import ChannelNotFoundError
from goygram.rich import rich_html
from goygram.sugar import extract_sent_message, html_to_entities
from goygram.types.kbd import kbd_to_tl


class AssetsHelper:
    def __init__(self, context: Any) -> None:
        self._context = context

    async def upload(self, file: Any, filename: str | None = None) -> Any:
        return await self._context.tg.send_media(file, caption=filename or "", peer="me")

    async def download(self, message: Any, destination: str | Any | None = None) -> Any:
                                                               
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

    async def send_html(self, text: str, **kwargs: Any) -> Any:
        plain, entities = html_to_entities(str(text))
        payload: dict[str, Any] = {"message": plain, **kwargs}
        if entities:
            payload["entities"] = entities
        return await self.call("messages.sendMessage", payload)

    async def send_file(self, path: Any, caption: str = "", **kwargs: Any) -> Any:
        app = getattr(getattr(self._host, "runtime", None), "app", None)
        file_name = kwargs.pop("file_name", None)
        mime = kwargs.pop("mime_type", "application/octet-stream")
        if app is not None:
            from relay.files import document, put
            up = await put(app, path, file_name=file_name)
            media = document(up, mime=mime, file_name=file_name)
            return await self.call("messages.sendMedia", {"media": media, "message": caption, **kwargs})
        return await self.call("messages.sendMedia", {
            "media": {"_": "inputMediaUploadedDocument", "file": path, "mime_type": mime},
            "message": caption, **kwargs,
        })

    async def send_to(self, chat: int | str, text: str, **kwargs: Any) -> Any:
        return await self.send_message(text, peer=chat, **kwargs)

    async def reply_to_message(self, chat: int | str, message_id: int, text: str, **kwargs: Any) -> Any:
        payload: dict[str, Any] = {"message": text, "peer": chat, "reply_to": {"_": "inputReplyToMessage", "reply_to_msg_id": int(message_id)}, **kwargs}
        return await self.call("messages.sendMessage", payload)

    async def react(self, chat: int | str, message_id: int, emoji: str = "👍", **kwargs: Any) -> Any:
        reaction: list[dict[str, Any]] = [{"_": "reactionEmoji", "emoticon": emoji}]
        if kwargs.get("big"):
            reaction[0] = {"_": "reactionEmoji", "emoticon": emoji, "flags": 1}
        return await self.call("messages.sendReaction", {
            "peer": chat, "msg_id": int(message_id), "reaction": reaction, **{k: v for k, v in kwargs.items() if k != "big"},
        })

    async def send_media(self, media: Any, caption: str = "", **kwargs: Any) -> Any:
        return await self.call("messages.sendMedia", {"media": media, "message": caption, **kwargs})

    async def send_rich(self, html_text: str | dict[str, Any], **kwargs: Any) -> Any:
        rich = {"_": "inputRichMessageHTML", **rich_html(html_text)} if isinstance(html_text, str) else html_text
        return await self.call("messages.sendMessage", {"rich_message": rich, **kwargs})

    async def edit_message(self, message_id: int, text: str, **kwargs: Any) -> Any:
        return await self.call("messages.editMessage", {"id": message_id, "message": text, **kwargs})

    async def edit_rich(self, message_id: int, html_text: str | dict[str, Any], **kwargs: Any) -> Any:
        rich = {"_": "inputRichMessageHTML", **rich_html(html_text)} if isinstance(html_text, str) else html_text
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
        result: dict[str, Any] = {"_": "inputRichMessageHTML", **rich_html(str(value))}
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

    @staticmethod
    def extract_sent(result: Any) -> dict[str, Any] | None:
        body = result.get("result", result) if isinstance(result, dict) else result
        sent = extract_sent_message(body)
        if sent is not None and isinstance(sent.get("id"), int):
            return sent
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            return result
        if isinstance(body, dict) and isinstance(body.get("message_id"), int):
            return {"id": body["message_id"], "message_id": body["message_id"]}
        return None

    async def call(self, method: str, **kwargs: Any) -> Any:
        app = getattr(self._manager, "bot_app", None)
        if app is None or getattr(app, "mt", None) is None:
            raise RuntimeError("inline bot is not ready")
        if method == "deleteMessage":
            from relay.rpc import delete_chat_msg
            with trusted_scope():
                return await delete_chat_msg(app, kwargs.get("chat_id"), int(kwargs["message_id"]))
        if method in {"sendPhoto", "sendDocument", "sendVideo"}:
            return await self._send_media_mt(app, method, kwargs)
        mapped = self._map_method(method, kwargs)
        if mapped is None:
            if "." in method or method.startswith("mt_"):
                with trusted_scope():
                    return await getattr(app, rpcname(method))(**kwargs)
            raise RuntimeError(f"unmapped bot method {method}")
        act, data = mapped
        with trusted_scope():
            return await getattr(app, rpcname(act))(**data)

    async def _send_media_mt(self, app: Any, method: str, kwargs: dict[str, Any]) -> Any:
        from relay.files import document, put
        key = "photo" if method == "sendPhoto" else "video" if method == "sendVideo" else "document"
        source = kwargs.get(key)
        caption = str(kwargs.get("caption", ""))
        up = await put(app, source, file_name=kwargs.get("file_name"))
        if method == "sendPhoto":
            file = document(up)["file"]
            media: dict[str, Any] = {"_": "inputMediaUploadedPhoto", "file": file}
        else:
            mime = "video/mp4" if method == "sendVideo" else "application/octet-stream"
            media = document(up, mime=mime, file_name=kwargs.get("file_name"))
        data: dict[str, Any] = {"peer": kwargs.get("chat_id"), "media": media, "message": caption, "random_id": secrets.randbits(63)}
        if str(kwargs.get("parse_mode", "")).lower() == "html" and caption:
            from goygram.sugar import html_to_entities
            plain, ents = html_to_entities(caption)
            data["message"] = plain
            if ents:
                data["entities"] = ents
        markup = self._tl_markup(kwargs.get("reply_markup"))
        if markup is not None:
            data["reply_markup"] = markup
        with trusted_scope():
            return await app.mt_messages_send_media( **data)

    def _tl_markup(self, markup: Any) -> Any:
        if markup is None:
            return None
        if isinstance(markup, list):
            markup = {"inline_keyboard": [row if isinstance(row, (list, tuple)) else [row] for row in markup]}
        if isinstance(markup, dict) and markup.get("_") in {"replyInlineMarkup", "replyKeyboardMarkup"}:
            return markup
        tl_markup = kbd_to_tl(markup) if isinstance(markup, dict) else None
        return tl_markup if isinstance(tl_markup, dict) else None

    def _map_method(self, method: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        from goygram.sugar import html_to_entities

        data: dict[str, Any] = {}
        if method in {"sendMessage", "sendRichMessage"}:
            text = str(kwargs.get("text", kwargs.get("message", "")))
            rich = kwargs.get("rich_message")
            data["peer"] = kwargs.get("chat_id")
            data["random_id"] = secrets.randbits(63)
            if isinstance(rich, dict):
                data["message"] = ""
                data["rich_message"] = rich
            else:
                data["message"] = text
                if str(kwargs.get("parse_mode", "")).lower() == "html":
                    plain, ents = html_to_entities(text)
                    data["message"] = plain
                    if ents:
                        data["entities"] = ents
            markup = self._tl_markup(kwargs.get("reply_markup"))
            if markup is not None:
                data["reply_markup"] = markup
            reply_to = kwargs.get("reply_to_message_id")
            if isinstance(reply_to, int):
                data["reply_to"] = {"_": "inputReplyToMessage", "reply_to_msg_id": reply_to}
            return "messages.sendMessage", data
        if method in {"editMessageText", "sendRichMessageDraft"} and kwargs.get("inline_message_id") is None:
            text = str(kwargs.get("text", ""))
            rich = kwargs.get("rich_message")
            data["peer"] = kwargs.get("chat_id")
            data["id"] = int(kwargs.get("message_id", 0))
            if isinstance(rich, dict):
                data["message"] = ""
                data["rich_message"] = rich
            else:
                data["message"] = text
                if str(kwargs.get("parse_mode", "")).lower() == "html":
                    plain, ents = html_to_entities(text)
                    data["message"] = plain
                    if ents:
                        data["entities"] = ents
            markup = self._tl_markup(kwargs.get("reply_markup"))
            if markup is not None:
                data["reply_markup"] = markup
            return "messages.editMessage", data
        if method == "editMessageText" and kwargs.get("inline_message_id") is not None:
            inline_mid = kwargs.get("inline_message_id")
            text = str(kwargs.get("text", ""))
            rich = kwargs.get("rich_message")
            data["id"] = inline_mid if isinstance(inline_mid, dict) else {"_": "inputBotInlineMessageID", "raw": inline_mid}
            if isinstance(rich, dict):
                data["message"] = ""
                data["rich_message"] = rich
            else:
                data["message"] = text
                if str(kwargs.get("parse_mode", "")).lower() == "html":
                    plain, ents = html_to_entities(text)
                    data["message"] = plain
                    if ents:
                        data["entities"] = ents
            markup = self._tl_markup(kwargs.get("reply_markup"))
            if markup is not None:
                data["reply_markup"] = markup
            return "messages.editInlineBotMessage", data
        if method == "answerCallbackQuery":
            qid = kwargs.get("callback_query_id", kwargs.get("query_id"))
            return "messages.setBotCallbackAnswer", {
                "query_id": int(qid or 0),
                "message": str(kwargs.get("text", "")),
                "alert": bool(kwargs.get("show_alert") or kwargs.get("alert")),
                "cache_time": int(kwargs.get("cache_time") or 0),
            }
        if method == "getMe":
            return "users.getUsers", {"id": [{"_": "inputUserSelf"}]}
        return None

    async def send(self, method: str, **kwargs: Any) -> Any:
        return await self.call(method, **kwargs)

    async def rich_send(self, chat_id: int | str, html_text: str, *, buttons: Any = None, **kwargs: Any) -> Any:
        data = {"chat_id": chat_id, "rich_message": {"_": "inputRichMessageHTML", **rich_html(html_text)}, **kwargs}
        if buttons is not None:
            data["reply_markup"] = {"inline_keyboard": buttons} if isinstance(buttons, list) else buttons
        try:
            return await self.call("sendRichMessage", **data)
        except RuntimeError as exc:
            error = str(exc).lower()
            blocked = any(text in error for text in ("chat not found", "bot was blocked", "forbidden: bot"))
            if not blocked or not await self._manager.reopen_owner_chat(chat_id):
                raise
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
        data = {"chat_id": chat_id, "message_id": message_id, "rich_message": {"_": "inputRichMessageHTML", **rich_html(html_text)}, **kwargs}
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self.call("editMessageText", **data)

    async def rich_draft(self, chat_id: int | str, html_text: str, *, draft_id: int, **kwargs: Any) -> Any:
        return await self.call("sendRichMessageDraft", chat_id=chat_id, draft_id=draft_id, rich_message={"_": "inputRichMessageHTML", **rich_html(html_text)}, **kwargs)

    async def inline_answer(self, query: Any, results: list[dict[str, Any]], **kwargs: Any) -> Any:
        return await query.answer(results=results, **kwargs)

    async def inline_edit(self, inline_message_id: str, html_text: str, *, buttons: Any = None, **kwargs: Any) -> Any:
        data = {"inline_message_id": inline_message_id, "rich_message": {"_": "inputRichMessageHTML", **rich_html(html_text)}, **kwargs}
        if buttons is not None:
            data["reply_markup"] = buttons
        return await self.call("editMessageText", **data)

    def reply(self, chat_id: int | str, message_id: int, text: str, **kwargs: Any) -> Any:
        return self.send("sendMessage", chat_id=chat_id, text=text, **kwargs)

    def __getattr__(self, name: str) -> Any:
        async def method(**kwargs: Any) -> Any:
            return await self.call(name, **kwargs)
        return method


class ForumHelper:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._lock = asyncio.Lock()
        self._rpc_lock = asyncio.Lock()
        self._group_try_ts = 0.0
        self._sync_ts = 0.0
        self._topic_tries: dict[str, float] = {}
        self._warmed: set[int] = set()
        self._warm_tries: dict[int, float] = {}
        self._invite_ts = 0.0
        self._apply_task: asyncio.Task[None] | None = None
        self._apply_chat: int | None = None

    def _apply_background(self, chat_id: int) -> None:
        if self._apply_task is not None and not self._apply_task.done():
            if self._apply_chat == chat_id:
                return
            self._apply_task.cancel()
        self._apply_chat = chat_id
        self._apply_task = asyncio.create_task(self._apply_group(chat_id), name="hotaru:forum-apply")

    def _bot_app(self) -> Any:
        manager = getattr(self._runtime, "inline", None)
        app = getattr(manager, "bot_app", None) if manager is not None else None
        if app is None or getattr(app, "mt", None) is None:
            raise RuntimeError("inline bot is not ready")
        return app

    def _user_mt(self) -> Any:
        app = getattr(self._runtime, "app", None)
        mt = getattr(app, "mt", None) if app is not None else None
        if mt is None:
            raise RuntimeError("userbot transport is not ready")
        return mt

    async def _user_call(self, act: str, **kw: Any) -> Any:
        app = getattr(self._runtime, "app", None)
        if app is None:
            raise RuntimeError("userbot transport is not ready")
        async with self._rpc_lock:
            with trusted_scope():
                return await getattr(app, rpcname(act))(**kw)

    async def _bot_call(self, act: str, **kw: Any) -> Any:
        app = self._bot_app()
        with trusted_scope():
            return await getattr(app, rpcname(act))(**kw)

    @staticmethod
    def _body(result: Any) -> dict[str, Any]:
        body = result.get("result", result) if isinstance(result, dict) else result
        return body if isinstance(body, dict) else {}

    def _ingest_user(self, result: Any) -> None:
        mt = self._user_mt()
        body = self._body(result)
        if body:
            mt._ingest_entities(body)

    def _ingest_bot(self, result: Any) -> None:
        app = self._bot_app()
        mt = getattr(app, "mt", None)
        body = self._body(result)
        if mt is not None and body:
            mt._ingest_entities(body)

    async def group(self) -> int | None:
        state = getattr(self._runtime, "state", None)
        stored = state.get_setting("forum-channel-id") if state is not None else None
        if isinstance(stored, int) and stored < -1000000000000:
            return stored
        return None

    async def _save_group(self, chat_id: int) -> None:
        state = getattr(self._runtime, "state", None)
        if state is not None:
            state.set_setting("forum-channel-id", int(chat_id))

    async def _find_groups_by_title(self, title: str) -> list[tuple[dict[str, Any], int]]:
        dialogs = await self._user_call(
            "messages.getDialogs",
            offset_date=0,
            offset_id=0,
            offset_peer={"_": "inputPeerEmpty"},
            limit=100,
            hash=0,
        )
        self._ingest_user(dialogs)
        found: list[tuple[dict[str, Any], int]] = []
        for chat in self._body(dialogs).get("chats") or []:
            if isinstance(chat, dict) and chat.get("_") == "channel" and str(chat.get("title") or "") == title and chat.get("access_hash") and not chat.get("left"):
                found.append((chat, -1000000000000 - int(chat["id"])))
        return found

    async def _delete_owned_group(self, chat: dict[str, Any], chat_id: int) -> None:
        if chat.get("creator") is not True:
            return
        peer = await self._user_peer(chat_id)
        if peer is None:
            return
        try:
            await self._user_call("channels.deleteChannel", channel=peer)
        except Exception:
            return

    async def _hide_general(self, chat_id: int) -> None:
        peer = await self._user_peer(chat_id)
        if peer is None:
            return
        try:
            await self._user_call("messages.editForumTopic", peer=peer, topic_id=1, hidden=True)
        except Exception:
            return

    def _emit(self, event: str, **fields: Any) -> None:
        obs = getattr(self._runtime, "observatory", None)
        if obs is not None:
            try:
                obs.emit("forum", event, **fields)
            except Exception:
                pass

    async def _warm_user_entity(self, chat_id: int, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and chat_id in self._warmed and now - self._warm_tries.get(chat_id, 0.0) < 30.0:
            return True
        if not force and now - self._warm_tries.get(chat_id, 0.0) < 30.0:
            return False
        self._warm_tries[chat_id] = now
        mt = self._user_mt()
        raw = -chat_id - 1000000000000
        with trusted_scope():
            entity = mt.entities.get(("chat", raw))
            if not isinstance(entity, dict) or not int(entity.get("access_hash") or 0):
                try:
                    dialogs = await self._user_call(
                        "messages.getDialogs",
                        offset_date=0,
                        offset_id=0,
                        offset_peer={"_": "inputPeerEmpty"},
                        limit=100,
                        hash=0,
                    )
                    self._ingest_user(dialogs)
                    entity = mt.entities.get(("chat", raw))
                except Exception as exc:
                    self._emit("warm_failed", chat=chat_id, reason="resolve", detail=str(exc)[:160])
                    return False
            if not isinstance(entity, dict):
                self._emit("warm_failed", chat=chat_id, reason="entity")
                return False
            access_hash = int(entity.get("access_hash") or 0)
            if not access_hash:
                self._emit("warm_failed", chat=chat_id, reason="hash")
                return False
            try:
                res = await self._user_call(
                    "channels.getChannels",
                    id=[{"_": "inputChannel", "channel_id": raw, "access_hash": access_hash}],
                )
            except Exception as exc:
                text = str(exc).upper()
                self._emit("warm_failed", chat=chat_id, reason="getchannels", detail=str(exc)[:160])
                if "CHANNEL_PRIVATE" in text or "CHANNEL_INVALID" in text:
                    self._warmed.discard(chat_id)
                    await self._reset_group(chat_id)
                return False
        alive = False
        for chat in self._body(res).get("chats") or []:
            if not isinstance(chat, dict) or int(chat.get("id") or 0) != raw:
                continue
            if chat.get("_") == "channel" and chat.get("access_hash") and not chat.get("left"):
                alive = True
                break
        if not alive:
            self._emit("warm_failed", chat=chat_id, reason="missing")
            self._warmed.discard(chat_id)
            await self._reset_group()
            return False
        self._ingest_user(res)
        self._warmed.add(chat_id)
        return True

    async def _drop_if_private(self, chat_id: int, exc: Exception) -> None:
        text = str(exc).upper()
        if "CHANNEL_PRIVATE" not in text and "CHANNEL_INVALID" not in text:
            return
        self._warmed.discard(chat_id)
        await self._reset_group(chat_id)

    async def _recover_bot_membership(self, chat_id: int, exc: Exception) -> bool:
        text = str(exc).upper()
        if "CHANNEL_PRIVATE" not in text and "USER_NOT_PARTICIPANT" not in text:
            return False
        self._sync_ts = 0.0
        await self._invite_bot(chat_id)
        await self._sync_channel_to_bot(chat_id)
        return await self._bot_resolve(chat_id) is not None

    def _dead_groups(self) -> set[int]:
        state = getattr(self._runtime, "state", None)
        if state is None:
            return set()
        value = state.get_setting("forum-dead-channel-ids")
        result = {int(item) for item in value if isinstance(item, int)} if isinstance(value, list) else set()
        legacy = state.get_setting("forum-dead-channel-id")
        if isinstance(legacy, int):
            result.add(legacy)
        return result

    async def _reset_group(self, dead: int | None = None) -> None:
        state = getattr(self._runtime, "state", None)
        if state is not None:
            state.set_setting("forum-channel-id", 0)
            if dead is not None:
                dead_groups = self._dead_groups()
                dead_groups.add(int(dead))
                state.set_setting("forum-dead-channel-ids", sorted(dead_groups)[-32:])
                state.set_setting("forum-dead-channel-id", int(dead))
        self._warmed.clear()
        self._topic_tries.clear()
        self._group_try_ts = 0.0

    def _dead(self, exc: Exception) -> bool:
        text = str(exc).upper()
        return "CHANNEL_PRIVATE" in text or "CHANNEL_INVALID" in text or "NO RESPONSE" in text or type(exc).__name__ == "TimeoutError"

    async def ensure_group(self) -> int | None:
        ready = getattr(self._runtime, "_forum_ready", None)
        if ready is not None:
            await ready.wait()
        async with self._lock:
            chat_id = await self.group()
            if chat_id is not None:
                if chat_id not in self._dead_groups() and await self._warm_user_entity(chat_id):
                    self._apply_background(chat_id)
                    return chat_id
                await self._reset_group()
            now = time.monotonic()
            if now - self._group_try_ts < 60.0:
                return None
            self._group_try_ts = now
            title = str(getattr(self._runtime, "forum_title", None) or "Hotaru Userbot")
            found = await self._find_groups_by_title(title)
            dead = self._dead_groups()
            live = [(chat, cid) for chat, cid in found if not chat.get("left") and cid not in dead]
            if live:
                for chat, cid in live[1:]:
                    await self._delete_owned_group(chat, cid)
                cid = live[0][1]
                await self._save_group(cid)
                if await self._warm_user_entity(cid, force=True):
                    self._apply_background(cid)
                    return cid
                await self._reset_group()
            try:
                result = await self._user_call(
                    "channels.createChannel",
                    megagroup=True,
                    forum=True,
                    title=title,
                    about="Hotaru observatory",
                )
            except Exception as exc:
                self._emit("create_failed", detail=str(exc)[:160])
                raise
            body = self._body(result)
            for chat in body.get("chats") or []:
                if isinstance(chat, dict) and chat.get("_") == "channel" and isinstance(chat.get("id"), int):
                    self._ingest_user(result)
                    new_id = -1000000000000 - int(chat["id"])
                    await self._save_group(new_id)
                    self._warmed.add(new_id)
                    self._apply_background(new_id)
                    self._emit("created", chat=new_id)
                    return new_id
            self._emit("create_failed", detail="no channel in result")
            return None

    async def _apply_group(self, chat_id: int) -> None:
        self._sync_ts = 0.0
        self._invite_ts = 0.0
        await self._invite_bot(chat_id)
        await self._promote_bot(chat_id)
        await self._hide_general(chat_id)
        await self._sync_channel_to_bot(chat_id)

    async def _user_bot(self, bot_id: int, username: str) -> dict[str, Any] | None:
        entity = self._user_mt().entities.get(("user", bot_id))
        if isinstance(entity, dict) and entity.get("access_hash"):
            return {"_": "inputUser", "user_id": bot_id, "access_hash": int(entity["access_hash"])}
        state = getattr(self._runtime, "state", None)
        owner = getattr(getattr(self._runtime, "config", None), "owner_id", None)
        ref_chat = state.get_setting("inline-reference-chat") if state is not None else None
        ref_message = state.get_setting("inline-reference-message") if state is not None else None
        if isinstance(owner, int) and ref_chat == owner and isinstance(ref_message, int):
            return {"_": "inputUserFromMessage", "peer": {"_": "inputPeerSelf"}, "msg_id": ref_message, "user_id": bot_id}
        return None

    async def _promote_bot(self, chat_id: int) -> None:
        manager = getattr(self._runtime, "inline", None)
        info = getattr(manager, "info", None)
        bot_id = getattr(info, "bot_id", None)
        username = getattr(info, "username", None)
        if not isinstance(bot_id, int) or not isinstance(username, str) or not username:
            return
        channel = self._user_channel(chat_id)
        if channel is None:
            return
        try:
            bot_user = await self._user_bot(bot_id, username)
            if bot_user is None:
                return
            await self._user_call(
                "channels.editAdmin",
                channel=channel,
                user_id=bot_user,
                admin_rights={
                    "_": "chatAdminRights",
                    "change_info": True,
                    "post_messages": True,
                    "edit_messages": True,
                    "delete_messages": True,
                    "ban_users": True,
                    "invite_users": True,
                    "pin_messages": True,
                    "manage_topics": True,
                    "anonymous": False,
                    "manage_call": False,
                    "other": True,
                },
                rank="Hotaru",
            )
        except Exception as exc:
            self._emit("promote_failed", chat=chat_id, error=type(exc).__name__, detail=str(exc)[:160])
            return

    async def _bot_resolve(self, chat_id: int) -> bytes | None:
        app = self._bot_app()
        with trusted_scope():
            try:
                return await app.mt.resolve_peer(chat_id)
            except Exception:
                return None

    async def _invite_bot(self, chat_id: int) -> None:
        manager = getattr(self._runtime, "inline", None)
        info = getattr(manager, "info", None)
        bot_id = getattr(info, "bot_id", None)
        username = getattr(info, "username", None)
        if not isinstance(bot_id, int) or not isinstance(username, str) or not username:
            self._emit("invite_skipped", chat=chat_id, reason="identity")
            return
        now = time.monotonic()
        if now - self._invite_ts < 60.0:
            return
        self._invite_ts = now
        channel = self._user_channel(chat_id)
        if channel is None:
            self._emit("invite_skipped", chat=chat_id, reason="peer")
            return
        try:
            bot_user = await self._user_bot(bot_id, username)
            if bot_user is None:
                self._emit("invite_skipped", chat=chat_id, reason="bot_user")
                return
            await self._user_call(
                "channels.inviteToChannel",
                channel=channel,
                users=[bot_user],
            )
            self._emit("invited", chat=chat_id, bot=bot_id)
        except Exception as exc:
            self._emit("invite_failed", chat=chat_id, error=type(exc).__name__, detail=str(exc)[:160])
            return

    def _state_key_bot_hash(self, raw_channel_id: int) -> str:
        return f"forum-bot-hash-{raw_channel_id}"

    def _load_bot_hash(self, chat_id: int) -> int | None:
        state = getattr(self._runtime, "state", None)
        if state is None:
            return None
        raw = -chat_id - 1000000000000
        stored = state.get_setting(self._state_key_bot_hash(raw))
        return int(stored) if isinstance(stored, int) and stored != 0 else None

    def _save_bot_hash(self, chat_id: int, access_hash: int) -> None:
        state = getattr(self._runtime, "state", None)
        if state is not None and access_hash:
            raw = -chat_id - 1000000000000
            state.set_setting(self._state_key_bot_hash(raw), access_hash)

    async def _sync_channel_to_bot(self, chat_id: int) -> None:
        now = time.monotonic()
        if now - self._sync_ts < 30.0:
            return
        self._sync_ts = now
        app = self._bot_app()
        bot_mt = getattr(app, "mt", None)
        if bot_mt is None:
            return
        raw = -chat_id - 1000000000000

        stored_hash = self._load_bot_hash(chat_id)
        if stored_hash is not None:
            try:
                res = await self._bot_call(
                    "channels.getChannels",
                    id=[{"_": "inputChannel", "channel_id": raw, "access_hash": stored_hash}],
                )
                self._ingest_bot(res)
                if await self._bot_resolve(chat_id) is not None:
                    return
            except Exception as exc:
                self._emit("bot_sync_stored_failed", chat=chat_id, error=type(exc).__name__, detail=str(exc)[:160])

        try:
            res = await self._bot_call(
                "channels.getChannels",
                id=[{"_": "inputChannel", "channel_id": raw, "access_hash": 0}],
            )
            self._ingest_bot(res)
            for chat in self._body(res).get("chats") or []:
                if isinstance(chat, dict) and int(chat.get("id") or 0) == raw:
                    ah = chat.get("access_hash")
                    if isinstance(ah, int) and ah:
                        self._save_bot_hash(chat_id, ah)
                    break
        except Exception as exc:
            self._emit("bot_sync_failed", chat=chat_id, error=type(exc).__name__, detail=str(exc)[:160])
            return

    async def ensure_topic(self, title: str) -> int | None:
        chat_id = await self.ensure_group()
        if chat_id is None:
            return None
        try:
            result = await self._user_call("messages.getForumTopics", peer=chat_id, offset_date=0, offset_id=0, offset_topic=0, limit=100)
        except Exception as exc:
            await self._drop_if_private(chat_id, exc)
            raise
        self._ingest_user(result)
        for topic in self._body(result).get("topics") or []:
            if isinstance(topic, dict) and isinstance(topic.get("id"), int) and str(topic.get("title") or "") == title:
                return int(topic["id"])
        now = time.monotonic()
        last = self._topic_tries.get(title, 0.0)
        if now - last < 60.0:
            return None
        self._topic_tries[title] = now
        created = await self._user_call("messages.createForumTopic", peer=chat_id, title=title, random_id=secrets.randbits(63))
        body = self._body(created)
        self._ingest_user(created)
        for upd in body.get("updates") or []:
            msg = upd.get("message") if isinstance(upd, dict) else None
            if not isinstance(msg, dict):
                continue
            action = msg.get("action")
            if isinstance(action, dict) and action.get("_") == "messageActionTopicCreate" and isinstance(msg.get("id"), int):
                return int(msg["id"])
        self._topic_tries.pop(title, None)
        recheck = await self._user_call("messages.getForumTopics", peer=chat_id, offset_date=0, offset_id=0, offset_topic=0, limit=100)
        self._ingest_user(recheck)
        for topic in self._body(recheck).get("topics") or []:
            if isinstance(topic, dict) and isinstance(topic.get("id"), int) and str(topic.get("title") or "") == title:
                return int(topic["id"])
        return None

    async def _user_peer(self, chat_id: int) -> bytes | None:
        mt = self._user_mt()
        with trusted_scope():
            try:
                return await mt.resolve_peer(chat_id)
            except Exception:
                return None

    def _user_channel(self, chat_id: int) -> dict[str, Any] | None:
        raw = -chat_id - 1000000000000
        entity = self._user_mt().entities.get(("chat", raw))
        access_hash = entity.get("access_hash") if isinstance(entity, dict) else None
        if not isinstance(access_hash, int) or not access_hash:
            return None
        return {"_": "inputChannel", "channel_id": raw, "access_hash": access_hash}

    async def _bot_ready(self, chat_id: int) -> bool:
        peer = await self._bot_resolve(chat_id)
        if peer is not None:
            return True
        await self._invite_bot(chat_id)
        await self._sync_channel_to_bot(chat_id)
        return await self._bot_resolve(chat_id) is not None

    async def send(self, topic_id: int, text: str, *, rich: Any = None, reply_to: int | None = None) -> Any:
        chat_id = await self.ensure_group()
        if chat_id is None:
            raise RuntimeError("forum group is not available")
        try:
            return await self._send_once(chat_id, topic_id, text, rich=rich, reply_to=reply_to)
        except Exception as exc:
            await self._drop_if_private(chat_id, exc)
            if await self._recover_bot_membership(chat_id, exc):
                return await self._send_once(chat_id, topic_id, text, rich=rich, reply_to=reply_to)
            if self._dead(exc):
                await self._reset_group()
            raise

    async def _send_once(self, chat_id: int, topic_id: int, text: str, *, rich: Any = None, reply_to: int | None = None) -> Any:
        use_bot = await self._bot_ready(chat_id)
        data: dict[str, Any] = {"random_id": secrets.randbits(63)}
        if isinstance(rich, dict):
            data["message"] = ""
            data["rich_message"] = rich
        else:
            plain, ents = html_to_entities(str(text))
            data["message"] = plain
            if ents:
                data["entities"] = ents
        reply = {"_": "inputReplyToMessage", "reply_to_msg_id": int(reply_to or topic_id)}
        if reply_to is not None:
            reply["top_msg_id"] = int(topic_id)
        data["reply_to"] = reply
        if use_bot:
            data["peer"] = await self._bot_resolve(chat_id)
            return await self._bot_call("messages.sendMessage", **data)
        data["peer"] = chat_id
        return await self._user_call("messages.sendMessage", **data)

    def _media_document(self, up: dict[str, Any], file_name: str | None) -> dict[str, Any]:
        from relay.files import document
        return document(up, mime="text/plain", file_name=file_name)

    async def send_file(self, topic_id: int, path: Any, caption: str = "", *, file_name: str | None = None) -> Any:
        chat_id = await self.ensure_group()
        if chat_id is None:
            raise RuntimeError("forum group is not available")
        try:
            return await self._send_file_once(chat_id, topic_id, path, caption, file_name=file_name)
        except Exception as exc:
            await self._drop_if_private(chat_id, exc)
            chat_id = await self.ensure_group()
            if chat_id is None:
                raise
            return await self._send_file_once(chat_id, topic_id, path, caption, file_name=file_name)

    async def _send_file_once(self, chat_id: int, topic_id: int, path: Any, caption: str, *, file_name: str | None = None) -> Any:
        use_bot = await self._bot_ready(chat_id)
        from relay.files import put
        host = self._bot_app() if use_bot else getattr(self._runtime, "app", None)
        up = await put(host, path, file_name=file_name)
        media = self._media_document(up, file_name)
        plain, ents = html_to_entities(str(caption))
        data: dict[str, Any] = {"media": media, "message": plain, "random_id": secrets.randbits(63), "reply_to": {"_": "inputReplyToMessage", "reply_to_msg_id": int(topic_id)}}
        if ents:
            data["entities"] = ents
        if use_bot:
            data["peer"] = await self._bot_resolve(chat_id)
            return await self._bot_call("messages.sendMedia", **data)
        data["peer"] = chat_id
        return await self._user_call("messages.sendMedia", **data)

    async def edit(self, message_id: int, text: str, *, rich: Any = None) -> Any:
        chat_id = await self.ensure_group()
        if chat_id is None:
            raise RuntimeError("forum group is not available")
        try:
            return await self._edit_once(chat_id, message_id, text, rich=rich)
        except Exception as exc:
            await self._drop_if_private(chat_id, exc)
            raise

    async def _edit_once(self, chat_id: int, message_id: int, text: str, *, rich: Any = None) -> Any:
        use_bot = await self._bot_ready(chat_id)
        data: dict[str, Any] = {"id": int(message_id), "message": ""}
        if isinstance(rich, dict):
            data["rich_message"] = rich
        else:
            plain, ents = html_to_entities(str(text))
            data["message"] = plain
            if ents:
                data["entities"] = ents
        if use_bot:
            data["peer"] = await self._bot_resolve(chat_id)
            return await self._bot_call("messages.editMessage", **data)
        data["peer"] = chat_id
        return await self._user_call("messages.editMessage", **data)

    async def edit_file(self, message_id: int, path: Any, caption: str = "", *, file_name: str | None = None) -> Any:
        chat_id = await self.ensure_group()
        if chat_id is None:
            raise RuntimeError("forum group is not available")
        use_bot = await self._bot_ready(chat_id)
        from relay.files import put
        host = self._bot_app() if use_bot else getattr(self._runtime, "app", None)
        up = await put(host, path, file_name=file_name)
        media = self._media_document(up, file_name)
        plain, ents = html_to_entities(str(caption))
        data: dict[str, Any] = {"id": int(message_id), "media": media, "message": plain}
        if ents:
            data["entities"] = ents
        if use_bot:
            data["peer"] = await self._bot_resolve(chat_id)
            return await self._bot_call("messages.editMessage", **data)
        data["peer"] = chat_id
        return await self._user_call("messages.editMessage", **data)

    async def topic_id_of(self, message_id: int) -> int:
        chat_id = await self.ensure_group()
        if chat_id is None:
            raise RuntimeError("forum group is not available")
        peer = await self._bot_resolve(chat_id) or await self._user_peer(chat_id)
        result = await self._bot_call("messages.getHistory", peer=peer, offset_id=int(message_id) + 1, offset_date=0, add_offset=0, limit=3, max_id=0, min_id=0, hash=0)
        for message in self._body(result).get("messages") or []:
            if isinstance(message, dict) and message.get("id") == int(message_id):
                reply = message.get("reply_to") or {}
                if isinstance(reply, dict):
                    top = reply.get("reply_to_top_id")
                    if isinstance(top, int):
                        return top
                    rid = reply.get("reply_to_msg_id")
                    if isinstance(rid, int):
                        return rid
        raise RuntimeError("topic not resolvable for message {mid}".format(mid=message_id))


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
        return await query.answer(results=results, **kw)

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
        result["input_message_content"] = {"rich_message": {"_": "inputRichMessageHTML", **rich_html(html_text)}}
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
            CallbackBinding(self._owner_id or 0, None, 0),
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
            try:
                await callback.answer()
            except Exception:
                pass
            deleter = getattr(callback, "delete", None)
            if callable(deleter):
                result = deleter()
                if asyncio.iscoroutine(result):
                    return await result
                return result
            return None
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
