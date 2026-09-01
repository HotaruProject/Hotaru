from __future__ import annotations

import os
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

    @property
    def message(self) -> ModuleMessage:
        return ModuleMessage(self._source, self.responses)

    @property
    def tg(self) -> Any:
        from relay.proxies import Gateway
        return Gateway(self.cap_host, self.module_id)

    @property
    def inline(self) -> Any:
        from relay.proxies import InlineHelper
        return InlineHelper(self.inline_manager)

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

    async def answer(self, **kwargs: Any) -> Response:
        return await self.responses.answer(self._source, **kwargs)

    async def answer_file(self, media: Any, **kwargs: Any) -> Response:
        kwargs.setdefault("output", "reply")
        return await self.answer(media=media, **kwargs)

    async def answer_media(self, media: Any, **kwargs: Any) -> Response:
        kwargs.setdefault("output", "reply")
        return await self.answer(media=media, **kwargs)

    async def answer_rich(self, rich_message: Any, **kwargs: Any) -> Response:
        return await self.answer(rich_message=rich_message, **kwargs)

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
    def __init__(self, state: Any, responses: ResponseService) -> None:
        self._state = state
        self._responses = responses
        self.cap_host: Any = None
        self.callback_router: Any = None
        self.inline_manager: Any = None

    def create(self, module_id: str, message: Any) -> ModuleContext:
        return ModuleContext(module_id, message, self._state.namespace(module_id), self._responses, self.cap_host, self.callback_router, self.inline_manager)
