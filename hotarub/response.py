from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .state import StateNamespace


OutputMode = Literal["edit", "reply", "auto"]


@dataclass(frozen=True, slots=True)
class Response:
    delivered: bool
    action: Literal["edit", "reply", "none"]
    transport: str | None
    message: Any = None


class ResponseService:
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
        payload = {"kbd": buttons}
        if rich_message is not None:
            payload["rich_message"] = rich_message
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


@dataclass(slots=True)
class ModuleContext:
    module_id: str
    message: Any
    state: StateNamespace
    responses: ResponseService

    async def answer(self, **kwargs: Any) -> Response:
        return await self.responses.answer(self.message, **kwargs)

    async def answer_file(self, media: Any, **kwargs: Any) -> Response:
        return await self.answer(media=media, **kwargs)

    async def answer_media(self, media: Any, **kwargs: Any) -> Response:
        return await self.answer(media=media, **kwargs)

    async def answer_rich(self, rich_message: Any, **kwargs: Any) -> Response:
        return await self.answer(rich_message=rich_message, **kwargs)
