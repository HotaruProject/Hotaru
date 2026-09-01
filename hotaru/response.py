from __future__ import annotations

import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from .state import StateNamespace


OutputMode = Literal["edit", "reply", "auto"]


class ResponseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Response:
    delivered: bool
    action: Literal["edit", "reply", "none"]
    transport: str | None
    message: Any = None


class FormHandle:
    def __init__(self, runtime: Any, source: Any, value: Any = None, key: str | None = None) -> None:
        self._runtime = runtime
        self._source = source
        self._record = None
        self._value = value
        self._key = key or secrets.token_urlsafe(12)

    async def edit(self, text: str, buttons: Any = None, **kwargs: Any) -> "FormHandle":
        if self._runtime is None:
            raise ResponseError("form runtime is unavailable")
        self._value = await self._runtime.edit_form(self, text, buttons, **kwargs)
        return self

    async def edit_buttons(self, buttons: Any) -> "FormHandle":
        if self._runtime is None:
            raise ResponseError("form runtime is unavailable")
        self._value = await self._runtime.edit_form(self, None, buttons)
        return self

    async def delete(self) -> bool:
        if self._runtime is None:
            return False
        return await self._runtime.delete_form(self)

    async def close(self) -> bool:
        return await self.delete()

    async def unload(self) -> bool:
        if self._runtime is None:
            return False
        return await self._runtime.delete_form(self)

    @property
    def value(self) -> Any:
        return self._value

    @property
    def message(self) -> Any:
        return self._value

    @property
    def transport(self) -> str | None:
        return getattr(self._source, "src", None)

    def snapshot(self) -> dict[str, Any] | None:
        if self._runtime is None:
            return None
        return self._runtime.form_snapshot(self)

    @property
    def key(self) -> str | None:
        return self._key

    def __bool__(self) -> bool:
        return bool(self._value)

    def __await__(self):
        async def resolve():
            return self
        return resolve().__await__()


class ResponseService:
    def __init__(self, rich_sender: Callable[..., Any] | None = None) -> None:
        self.rich_sender = rich_sender

    async def answer(
        self,
        message: Any,
        *,
        text: str | None = None,
        rich_message: Any = None,
        media: Any = None,
        buttons: Any = None,
        output: OutputMode = "edit",
        reply_to: int | None = None,
        topic_id: int | None = None,
    ) -> Response:
        if text is None and rich_message is None and media is None:
            raise ValueError("response requires text, rich_message, or media")
        if text is not None and rich_message is not None:
            raise ValueError("text and rich_message are mutually exclusive")
        if output not in ("edit", "reply", "auto"):
            raise ValueError("output mode is invalid")
        if rich_message is not None:
            if self.rich_sender is None:
                raise ResponseError("rich transport is not configured")
            result = self.rich_sender(
                message,
                rich_message=rich_message,
                buttons=buttons,
                output=output,
                reply_to=reply_to,
                topic_id=topic_id,
            )
            if hasattr(result, "__await__"):
                result = await result
            return Response(True, "edit" if output == "edit" else "reply", getattr(message, "src", None), result)
        payload = {"kbd": buttons}
        if media is not None:
            payload["media"] = media
        if reply_to is not None:
            payload["reply_to"] = reply_to
        if topic_id is not None:
            payload["topic_id"] = topic_id
        if output in ("edit", "auto") and hasattr(message, "edit"):
            try:
                result = await message.edit(text or "", **payload)
            except Exception:
                if output == "edit":
                    raise
            else:
                return Response(True, "edit", getattr(message, "src", None), result)
        if output == "edit":
            return Response(False, "none", None)
        if not hasattr(message, "reply"):
            return Response(False, "none", None)
        result = await message.reply(text or "", **payload)
        return Response(True, "reply", getattr(message, "src", None), result)

    async def smart(
        self,
        message: Any,
        content: Any = None,
        *,
        output: OutputMode = "auto",
        inline: bool = False,
        bot: bool = False,
        form: Any = None,
        buttons: Any = None,
        file: Any = None,
        media: Any = None,
        rich_message: Any = None,
        **kwargs: Any,
    ) -> Any:
        if content is not None:
            if isinstance(content, str):
                kwargs.setdefault("text", content)
            elif isinstance(content, dict) and content.get("_") == "inputRichMessageHTML":
                rich_message = content
            else:
                media = content
        if file is not None:
            media = file
        if kwargs.get("topic_id") is None:
            for name in ("topic_id", "message_thread_id", "top_msg_id"):
                candidate = getattr(message, name, None)
                if isinstance(candidate, int) and candidate > 0:
                    kwargs["topic_id"] = candidate
                    break
                getter = getattr(message, "get", None)
                if callable(getter):
                    candidate = getter(name)
                    if isinstance(candidate, int) and candidate > 0:
                        kwargs["topic_id"] = candidate
                        break
        if inline or form is not None or buttons is not None:
            data = form if isinstance(form, dict) else {"text": kwargs.pop("text", ""), "buttons": buttons}
            if hasattr(message, "form"):
                return await message.form(data.get("text", ""), data.get("buttons"), **kwargs)
            return await self.answer(message, text=data.get("text", ""), buttons=data.get("buttons"), output="reply")
        if bot:
            return await kwargs.pop("bot_gateway").send_message(getattr(message, "chat_id", None), kwargs.pop("text", ""), buttons=buttons, **kwargs)
        if rich_message is not None:
            return await self.answer(message, rich_message=rich_message, output=output, **kwargs)
        return await self.answer(message, text=kwargs.pop("text", None), media=media, output=output, buttons=buttons, **kwargs)

    async def split(self, message: Any, text: str, *, limit: int = 4096, **kwargs: Any) -> list[Any]:
        if limit < 1:
            raise ValueError("limit must be positive")
        parts = []
        rest = text
        while len(rest) > limit:
            cut = max(rest.rfind("\n", 0, limit + 1), rest.rfind(" ", 0, limit + 1))
            if cut < max(1, limit // 2):
                cut = limit
            parts.append(rest[:cut].rstrip())
            rest = rest[cut:].lstrip()
        parts.append(rest)
        result = []
        for index, part in enumerate(parts):
            options = dict(kwargs)
            options["output"] = "edit" if index == 0 and kwargs.get("output", "auto") == "auto" else kwargs.get("output", "reply")
            result.append(await self.answer(message, text=part, **options))
        return result

    async def fallback_file(self, message: Any, text: str, *, filename: str = "response.html", **kwargs: Any) -> Any:
        fd, raw = tempfile.mkstemp(prefix="hotaru-response-", suffix=".html")
        os.close(fd)
        path = Path(raw)
        path.write_text(text, encoding="utf-8")
        try:
            if hasattr(message, "reply"):
                return await message.reply(path, filename=filename, **kwargs)
            return await self.answer(message, media=path, output="reply", **kwargs)
        finally:
            path.unlink(missing_ok=True)

    async def respond(self, message: Any, content: Any = None, **kwargs: Any) -> Any:
        return await self.smart(message, content, **kwargs)


@dataclass(frozen=True, slots=True)
class Attachment:
    kind: str
    file_name: str | None
    mime_type: str | None
    size: int | None
    raw: Any


class ModuleMessage:
    def __init__(self, source: Any, responses: ResponseService) -> None:
        self._source = source
        self._responses = responses

    def __getattr__(self, name: str) -> Any:
        if name in {"app", "raw", "net", "_resolve_peer", "_source"}:
            raise AttributeError(name)
        if name in {"id", "msg_id", "chat_id", "from_id", "text", "is_me", "src"}:
            return getattr(self._source, name, None)
        raise AttributeError(name)

    def get(self, key: str, default: Any = None) -> Any:
        if key in {"raw", "app", "net", "_resolve_peer"}:
            return default
        return self._source.get(key, default) if hasattr(self._source, "get") else default

    async def edit(self, text: str, **kwargs: Any) -> Any:
        return await self._responses.answer(self._source, text=text, output="edit", **kwargs)

    async def reply(self, text: str, **kwargs: Any) -> Any:
        kwargs.setdefault("output", "reply")
        return await self._responses.answer(self._source, text=text, **kwargs)

    async def download(self, destination: str | Path | None = None) -> Any:
        if not hasattr(self._source, "download"):
            raise ResponseError("media download is unavailable")
        return await self._source.download(destination)


@dataclass(slots=True)
class ModuleContext:
    module_id: str
    _source: Any
    state: StateNamespace
    responses: ResponseService
    cap_host: Any = None
    callback_router: Any = None
    inline_manager: Any = None
    form_sender: Any = None
    runtime: Any = None

    @property
    def message(self) -> ModuleMessage:
        return ModuleMessage(self._source, self.responses)

    @property
    def tg(self) -> Any:
        from relay.proxies import Gateway
        return Gateway(self.cap_host, self.module_id)

    @property
    def rich(self) -> Any:
        from relay.proxies import RichGateway
        return RichGateway(self.tg, self._source)

    @property
    def bot(self) -> Any:
        from relay.proxies import BotGateway
        return BotGateway(self.inline_manager)

    @property
    def inline(self) -> Any:
        from relay.proxies import InlineHelper
        return InlineHelper(self.inline_manager)

    @property
    def html(self) -> Any:
        from relay.proxies import HtmlHelper
        return HtmlHelper()

    @property
    def ui(self) -> Any:
        from relay.proxies import UiHelper
        return UiHelper(self.module_id, self.callback_router.store, getattr(self._source, "from_id", None), getattr(self._source, "chat_id", None), getattr(self._source, "id", 0), self.callback_router)

    async def cap(self, capability: str, payload: dict[str, Any] | None = None) -> Any:
        if self.cap_host is None:
            raise ResponseError("capabilities are not available")
        return await self.cap_host.call(self.module_id, capability, payload or {})

    async def mt(self, method: str, **kwargs: Any) -> Any:
        return await self.cap("mt", {"method": method, "kwargs": kwargs})

    async def net(self, url: str, *, data: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        return await self.cap("net", {"url": url, "data": data, "timeout": timeout})

    async def answer(self, text: str | None = None, **kwargs: Any) -> Any:
        if text is not None:
            kwargs["text"] = text
        if kwargs.get("text") is not None:
            kwargs.setdefault("parse_mode", "HTML")
        if kwargs.get("buttons"):
            kwargs["buttons"] = self._normalize_buttons(kwargs["buttons"])
        if kwargs.get("buttons") and self.form_sender is not None:
            return await self.form_sender(self._source, kwargs.get("text", ""), kwargs["buttons"], kwargs)
        if kwargs.get("text") is not None and kwargs.get("rich", True):
            value = kwargs.pop("text")
            limit = int(kwargs.pop("split_limit", 4096))
            if len(value) > limit:
                return await self.responses.split(self._source, value, limit=limit, **kwargs)
            return await self.send_rich(value, **kwargs)
        return await self.responses.answer(self._source, **kwargs)

    async def respond(self, content: Any = None, **kwargs: Any) -> Any:
        mode = kwargs.pop("mode", kwargs.pop("output", "auto"))
        delete_source = kwargs.pop("delete_source", False)
        if kwargs.pop("force_reply", False):
            mode = "reply"
        if mode == "auto":
            mode = "edit" if self._outgoing else "reply"
        if mode not in {"edit", "reply"}:
            raise ResponseError("response mode must be edit, reply, or auto")
        if delete_source == "auto":
            delete_source = self._outgoing and mode == "reply"
        file_value = kwargs.pop("file", None)
        if file_value is not None:
            kwargs.setdefault("media", file_value)
        topic_id = kwargs.pop("topic_id", None)
        if topic_id is None:
            topic_id = self.topic_id
        if topic_id is not None:
            kwargs["topic_id"] = topic_id
        if content is not None:
            if isinstance(content, dict) and content.get("_") == "inputRichMessageHTML":
                kwargs["rich_message"] = content
            elif isinstance(content, str):
                kwargs.setdefault("text", content)
            else:
                kwargs.setdefault("media", content)
        buttons = kwargs.pop("buttons", None)
        if kwargs.pop("inline", False):
            self.runtime.purge_forms() if self.runtime is not None else None
            result = await self.inline_form(kwargs.pop("text", ""), buttons, **kwargs)
        elif kwargs.pop("bot", False):
            chat_id = kwargs.pop("chat_id", getattr(self._source, "chat_id", None))
            if kwargs.get("rich_message") is not None:
                result = await self.bot.rich_send(chat_id, kwargs.pop("rich_message"), buttons=buttons, **kwargs)
            elif kwargs.get("media") is not None:
                result = await self.bot.send_photo(chat_id, kwargs.pop("media"), caption=kwargs.pop("text", ""), buttons=buttons, **kwargs)
            else:
                result = await self.bot.send_message(chat_id, kwargs.pop("text", ""), buttons=buttons, **kwargs)
        elif kwargs.get("form") is not None:
            form = kwargs.pop("form")
            result = await self.form(form.get("text", ""), form.get("buttons"), output=mode, **kwargs)
        elif buttons is not None:
            result = await self.form(kwargs.pop("text", ""), buttons, output=mode, **kwargs)
        else:
            kwargs["output"] = mode
            if kwargs.get("rich_message") is not None:
                result = await self.answer(rich_message=kwargs.pop("rich_message"), **kwargs)
            else:
                result = await self.answer(**kwargs)
        if delete_source:
            await self._delete_source()
        return result

    async def smart_answer(self, content: Any = None, **kwargs: Any) -> Any:
        return await self.respond(content, **kwargs)

    @property
    def _outgoing(self) -> bool:
        value = getattr(self._source, "is_me", None)
        if isinstance(value, bool):
            return value
        value = getattr(self._source, "out", None)
        return bool(value)

    @property
    def topic_id(self) -> int | None:
        for name in ("topic_id", "message_thread_id", "top_msg_id"):
            value = getattr(self._source, name, None)
            if isinstance(value, int) and value > 0:
                return value
        getter = getattr(self._source, "get", None)
        if callable(getter):
            for name in ("topic_id", "message_thread_id", "top_msg_id"):
                value = getter(name)
                if isinstance(value, int) and value > 0:
                    return value
        return None

    async def _delete_source(self) -> None:
        deleter = getattr(self._source, "delete", None)
        if callable(deleter):
            result = deleter()
            if hasattr(result, "__await__"):
                await result

    async def send_file(self, file: Any, caption: str | None = None, **kwargs: Any) -> Response:
        output = kwargs.pop("output", "reply")
        mode = kwargs.pop("mode", output)
        if caption is None:
            return await self.respond(file, mode=mode, **kwargs)
        return await self.respond(caption, media=file, mode=mode, **kwargs)

    async def send_media(self, media: Any, caption: str | None = None, **kwargs: Any) -> Response:
        return await self.send_file(media, caption, **kwargs)

    async def form(self, text: str, buttons: Any = None, *rows: Any, **kwargs: Any) -> FormHandle:
        if isinstance(buttons, dict) and "buttons" in buttons:
            text = str(buttons.get("text", text))
            buttons = buttons["buttons"]
        if buttons is None:
            buttons = list(rows)
        elif rows:
            buttons = [buttons, *rows]
        if buttons and isinstance(buttons[0], dict):
            buttons = [buttons]
        buttons = self._normalize_buttons(buttons)
        kwargs["buttons"] = buttons
        kwargs["text"] = text
        result = await self.answer(**kwargs)
        handle = FormHandle(self.runtime, self._source, result)
        if self.runtime is not None:
            self.runtime.register_form(handle, self._source, text, buttons, kwargs)
        return handle

    def _normalize_buttons(self, buttons: Any) -> Any:
        if not buttons or self.callback_router is None:
            return buttons
        ui = self.ui
        result = []
        for row in buttons:
            current = []
            for button in row:
                if isinstance(button, dict) and callable(button.get("handler")) and isinstance(button.get("input"), str):
                    current.append(button)
                    continue
                if isinstance(button, dict) and callable(button.get("handler")) and "callback" not in button:
                    button = {**button, "callback": button["handler"]}
                if not isinstance(button, dict) or "callback" not in button:
                    current.append(button)
                    continue
                current.append(ui.button(str(button.get("text", "")), button["callback"], button.get("payload"), style=button.get("style")))
            result.append(current)
        return result

    @staticmethod
    def escape(value: Any) -> str:
        from html import escape
        return escape(str(value), quote=False)

    async def inline_form(self, text: str, buttons: Any = None, *rows: Any, **kwargs: Any) -> Any:
        if self.form_sender is None:
            raise ResponseError("inline form transport is not available")
        value = list(rows) if buttons is None else ([buttons, *rows] if rows else buttons)
        if value and isinstance(value[0], dict):
            value = [value]
        if self.callback_router is not None:
            value = self._normalize_buttons(value)
        result = await self.form_sender(self._source, text, value, kwargs)
        handle = FormHandle(self.runtime, self._source, result)
        if self.runtime is not None:
            self.runtime.register_form(handle, self._source, text, value, kwargs)
        return handle

    async def reply_html(self, text: str, **kwargs: Any) -> Response:
        kwargs.setdefault("output", "reply")
        return await self.answer(text=text, **kwargs)

    async def edit_html(self, text: str, **kwargs: Any) -> Response:
        kwargs.setdefault("output", "edit")
        return await self.answer(text=text, **kwargs)

    async def answer_file(self, media: Any, **kwargs: Any) -> Response:
        kwargs.setdefault("output", "reply")
        return await self.answer(media=media, **kwargs)

    async def answer_media(self, media: Any, **kwargs: Any) -> Response:
        kwargs.setdefault("output", "reply")
        return await self.answer(media=media, **kwargs)

    async def answer_rich(self, rich_message: Any, **kwargs: Any) -> Response:
        if isinstance(rich_message, str):
            return await self.send_rich(rich_message, **kwargs)
        if not isinstance(rich_message, dict):
            raise ResponseError("rich_message must be HTML text or a native input object")
        buttons = kwargs.pop("buttons", None)
        if buttons is not None and self.form_sender is not None:
            return await self.form_sender(self._source, rich_message, buttons, kwargs)
        return await self._context_tg_call("messages.sendMessage", {"peer": self._source.chat_id, "message": "", "rich_message": rich_message, **kwargs})

    async def send_rich(self, html: str, **kwargs: Any) -> Response:
        buttons = kwargs.pop("buttons", None)
        if buttons is not None and self.form_sender is not None:
            result = await self.form_sender(self._source, html, buttons, kwargs)
            return result if isinstance(result, Response) else Response(True, "reply", getattr(self._source, "src", None), result)
        peer = getattr(self._source, "chat_id", None)
        if peer is None:
            raise ResponseError("rich message target is missing")
        data = {"peer": peer, "message": "", "random_id": secrets.randbits(63), "rich_message": {"_": "inputRichMessageHTML", "html": html}}
        kwargs.pop("parse_mode", None)
        output = kwargs.pop("output", "reply")
        if output == "edit" and getattr(self._source, "id", None) is not None:
            data["id"] = int(self._source.id)
            data.pop("peer", None)
            result = await self._context_tg_call("messages.editMessage", {"peer": peer, **data})
            return Response(True, "edit", getattr(self._source, "src", None), result)
        reply_to = kwargs.pop("reply_to", None) or getattr(self._source, "id", None)
        topic_id = kwargs.pop("topic_id", None) or self.topic_id
        if reply_to is not None:
            data["reply_to"] = {"_": "inputReplyToMessage", "reply_to_msg_id": int(reply_to), **({"top_msg_id": int(topic_id)} if topic_id is not None else {})}
        data.update(kwargs)
        result = await self._context_tg_call("messages.sendMessage", data)
        return Response(True, "reply", getattr(self._source, "src", None), result)

    async def _context_tg_call(self, method: str, data: dict[str, Any]) -> Any:
        host = getattr(self, "cap_host", None)
        if host is None:
            raise ResponseError("capabilities are not available")
        return await host.call(self.module_id, "mt", {"method": method, "kwargs": data})

    async def send(self, text: str, **kwargs: Any) -> Response:
        kwargs.setdefault("output", "reply")
        return await self.answer(text=text, **kwargs)

    async def edit(self, text: str, **kwargs: Any) -> Response:
        return await self.answer(text=text, output="edit", **kwargs)

    @property
    def reply_message(self) -> Any | None:
        candidate = self.message.get("reply_to_message") or self.message.get("reply")
        if candidate is not None:
            return candidate
        return None

    @property
    def reply_text(self) -> str | None:
        reply = self.reply_message
        if reply is None:
            return None
        if hasattr(reply, "get"):
            text = reply.get("text") or reply.get("message") or reply.get("caption")
            return str(text) if text is not None else None
        return None

    @property
    def has_media(self) -> bool:
        return self.attachment is not None

    @property
    def attachment(self) -> Attachment | None:
        return self._attachment_of(self.message)

    @property
    def reply_attachment(self) -> Attachment | None:
        reply = self.reply_message
        if reply is None:
            return None
        return self._attachment_of(reply)

    @staticmethod
    def _attachment_of(message: Any) -> Attachment | None:
        if not hasattr(message, "get"):
            return None
        for kind in ("document", "photo", "video", "audio", "voice", "animation", "video_note", "sticker"):
            media = message.get(kind)
            if media is None:
                continue
            if isinstance(media, list):
                media = media[-1] if media else None
            if not isinstance(media, dict):
                continue
            return Attachment(
                kind=kind,
                file_name=media.get("file_name") or media.get("name"),
                mime_type=media.get("mime_type"),
                size=media.get("size") if isinstance(media.get("size"), int) else None,
                raw=media,
            )
        media_wrap = message.get("media")
        if isinstance(media_wrap, dict):
            document = media_wrap.get("document")
            if isinstance(document, dict):
                return Attachment(
                    kind="document",
                    file_name=document.get("file_name"),
                    mime_type=document.get("mime_type"),
                    size=document.get("size") if isinstance(document.get("size"), int) else None,
                    raw=document,
                )
            photo = media_wrap.get("photo")
            if isinstance(photo, dict):
                return Attachment(kind="photo", file_name=None, mime_type="image/jpeg", size=None, raw=photo)
        return None

    async def download(self, attachment: Attachment | None = None, destination: str | Path | None = None) -> Path:
        target = attachment or self.attachment or self.reply_attachment
        if target is None:
            raise ResponseError("no attachment to download")
        if destination is None:
            fd, raw = tempfile.mkstemp(prefix="hotaru-dl-")
            os.close(fd)
            destination = raw
        path = Path(destination)
        source = self.message
        if self.attachment is None and self.reply_message is not None:
            source = self.reply_message
        if hasattr(source, "download"):
            await source.download(str(path))
            return path
        document = target.raw if isinstance(target.raw, dict) else None
        if document is None:
            raise ResponseError("attachment is not downloadable")
        file_id = document.get("file_id")
        if isinstance(file_id, str):
            await self._source.app.download_file(file_id, str(path))
            return path
        if isinstance(document.get("id"), int) and isinstance(document.get("access_hash"), int):
            file_reference = document.get("file_reference", b"")
            if isinstance(file_reference, str):
                try:
                    file_reference = bytes.fromhex(file_reference)
                except ValueError:
                    file_reference = file_reference.encode("utf-8")
            location = {
                "_": "inputDocumentFileLocation",
                "id": document["id"],
                "access_hash": document["access_hash"],
                "file_reference": bytes(file_reference),
                "thumb_size": "",
            }
            await self._source.app.mt.download_file(location, str(path), limit=524288)
            return path
        raise ResponseError("attachment location is incomplete")


class ModuleContextFactory:
    def __init__(self, state: Any, responses: ResponseService, runtime: Any = None) -> None:
        self._state = state
        self._responses = responses
        self._runtime = runtime
        self.runtime = runtime
        self.cap_host: Any = None
        self.callback_router: Any = None
        self.inline_manager: Any = None
        self.form_sender: Any = None
        self.runtime: Any = runtime

    def create(self, module_id: str, message: Any) -> ModuleContext:
        return ModuleContext(module_id, message, self._state.namespace(module_id), self._responses, self.cap_host, self.callback_router, self.inline_manager, self.form_sender, self.runtime)
