from __future__ import annotations

from typing import Any

_MEDIA_KINDS = {"photo", "video", "mpeg4_gif", "audio", "voice", "document", "gif"}


def _mime_of(result: dict[str, Any], kind: str) -> str:
    mime = result.get("mime_type")
    if isinstance(mime, str) and mime:
        return mime
    if kind == "photo":
        return "image/jpeg"
    if kind == "video":
        return "video/mp4"
    if kind in {"mpeg4_gif", "gif"}:
        return "video/mp4"
    if kind == "audio":
        return "audio/mpeg"
    if kind == "voice":
        return "audio/ogg"
    return "application/octet-stream"


def _html_to_entities(html_src: str) -> tuple[str, list[dict[str, Any]]]:
    from goygram.sugar import html_to_entities

    return html_to_entities(html_src)


def _kbd(buttons: Any) -> dict[str, Any] | None:
    from goygram.types.kbd import kbd_to_tl

    if buttons is None:
        return None
    if isinstance(buttons, dict) and buttons.get("_") in {"replyInlineMarkup", "replyKeyboardMarkup", "replyKeyboardHide", "replyForceReply"}:
        return buttons
    if isinstance(buttons, list):
        buttons = {"inline_keyboard": [row if isinstance(row, (list, tuple)) else [row] for row in buttons]}
    if isinstance(buttons, dict) and "inline_keyboard" not in buttons and "keyboard" not in buttons:
        return None
    markup = kbd_to_tl(buttons)
    return markup if isinstance(markup, dict) else None


def _markup_of(result: dict[str, Any]) -> dict[str, Any] | None:
    markup = result.get("reply_markup")
    if markup is None:
        return None
    if isinstance(markup, list):
        rows = [row if isinstance(row, (list, tuple)) else [row] for row in markup]
        markup = {"inline_keyboard": rows}
    return _kbd(markup)


def _send_message(result: dict[str, Any], content: dict[str, Any], kind: str) -> dict[str, Any]:
    rich = content.get("rich_message")
    if isinstance(rich, dict):
        payload: dict[str, Any] = {"_": "inputBotInlineMessageRichMessage", "rich_message": rich}
        markup = _markup_of(result)
        if markup is None:
            markup = _markup_of(content)
        if markup is not None:
            payload["reply_markup"] = markup
        return payload
    markup = _markup_of(result)
    if markup is None:
        markup = _markup_of(content)
    if kind in _MEDIA_KINDS:
        text = str(content.get("message_text", content.get("text", "")) or result.get("caption", ""))
        media: dict[str, Any] = {"_": "inputBotInlineMessageMediaAuto", "message": text}
        if str(result.get("parse_mode", "")).lower() == "html" or str(content.get("parse_mode", "")).lower() == "html":
            plain, ents = _html_to_entities(text)
            if ents:
                media["message"] = plain
                media["entities"] = ents
        if markup is not None:
            media["reply_markup"] = markup
        return media
    text = str(content.get("message_text", content.get("text", "")))
    text_payload = {"_": "inputBotInlineMessageText", "message": text}
    if content.get("disable_web_page_preview") or content.get("no_webpage"):
        text_payload["no_webpage"] = True
    if str(content.get("parse_mode", "")).lower() == "html" or str(result.get("parse_mode", "")).lower() == "html":
        plain, ents = _html_to_entities(text)
        if ents:
            text_payload["message"] = plain
            text_payload["entities"] = ents
    if markup is not None:
        text_payload["reply_markup"] = markup
    return text_payload


def _web_doc(url: str, mime: str = "image/jpeg", *, w: int | None = None, h: int | None = None) -> dict[str, Any]:
    attrs: list[dict[str, Any]] = []
    if w and h:
        attrs.append({"_": "documentAttributeImageSize", "w": int(w), "h": int(h)})
    return {"_": "inputWebDocument", "url": url, "size": 0, "mime_type": mime, "attributes": attrs}


def _thumb(result: dict[str, Any]) -> dict[str, Any] | None:
    url = result.get("thumb_url") or result.get("thumbnail_url")
    if not isinstance(url, str) or not url:
        return None
    return _web_doc(url, w=result.get("thumb_width"), h=result.get("thumb_height"))


def to_tl_result(result: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(result, dict) and result.get("_") == "inputBotInlineResult":
        return result
    kind = str(result.get("type", "article"))
    if kind in {"location", "venue", "contact"}:
        return None
    content = result.get("input_message_content") or {"message_text": result.get("caption", "")}
    send = _send_message(result, content, kind)
    payload: dict[str, Any] = {"_": "inputBotInlineResult", "id": str(result.get("id", "")), "type": kind, "send_message": send}
    if result.get("title") is not None:
        payload["title"] = str(result["title"])
    if result.get("description") is not None:
        payload["description"] = str(result["description"])
    if kind in _MEDIA_KINDS:
        url = result.get("photo_url") or result.get("video_url") or result.get("audio_url") or result.get("voice_url") or result.get("document_url") or result.get("mpeg4_url")
        if isinstance(url, str) and url:
            payload["content"] = _web_doc(url, mime=_mime_of(result, kind))
    elif kind == "article":
        url = result.get("url")
        if isinstance(url, str) and url:
            payload["url"] = url
    thumb = _thumb(result)
    if thumb is not None:
        payload["thumb"] = thumb
    return payload


def to_tl_results(results: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in results or []:
        converted = to_tl_result(item)
        if converted is not None:
            out.append(converted)
    return out


async def answer_tl(query: Any, results: list[dict[str, Any]] | None = None, **kw: Any) -> Any:
    if getattr(query, "src", None) == "mt" and results:
        results = to_tl_results(results)
    return await query.answer(results=results, **kw)
