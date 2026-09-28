"""Inline keyboard to TL conversion.

GoyGram's ``kbd_to_tl`` drops ``style`` and knows nothing about disabled or
profile buttons, so every keyboard Hotaru sends goes through this module.
"""
from __future__ import annotations

from typing import Any, Dict, List, cast

from goygram.types.kbd import KbdBuilder

ROW_LIMIT = 8
COPY_LIMIT = 256
DATA_LIMIT = 64
TEXT_LIMIT = 64
STYLES = {"primary": "bg_primary", "danger": "bg_danger", "success": "bg_success"}


def _bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return str(value).encode()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def button_type(button: dict[str, Any]) -> dict[str, Any]:
    kind = button.get("type")
    if isinstance(kind, dict) and cast(Dict[str, Any], kind).get("_"):
        return cast(Dict[str, Any], kind)
    if button.get("disabled"):
        return {"_": "inlineButtonTypeDisabled"}
    if button.get("callback_data") is not None:
        data = _bytes(button["callback_data"])
        if len(data) > DATA_LIMIT:
            raise ValueError("callback_data exceeds 64 bytes")
        return {"_": "inlineButtonTypeCallback", "data": data}
    if button.get("url") is not None:
        return {"_": "inlineButtonTypeUrl", "url": str(button["url"])}
    if button.get("copy_text") is not None:
        text = str(button["copy_text"]) or " "
        return {"_": "inlineButtonTypeCopy", "copy_text": text[:COPY_LIMIT]}
    if button.get("web_app") is not None:
        app = button["web_app"]
        url = cast(Dict[str, Any], app).get("url", "") if isinstance(app, dict) else app
        return {"_": "inlineButtonTypeWebView", "url": str(url)}
    if button.get("switch_inline_query_current_chat") is not None:
        return {"_": "inlineButtonTypeSwitchInline", "query": str(button["switch_inline_query_current_chat"]), "same_peer": True}
    if button.get("switch_inline_query") is not None:
        return {"_": "inlineButtonTypeSwitchInline", "query": str(button["switch_inline_query"])}
    if isinstance(button.get("user_id"), int):
        return {"_": "inlineButtonTypeUserProfile", "user_id": int(button["user_id"])}
    return {"_": "inlineButtonTypeCallback", "data": _bytes(button.get("callback_data") or "noop")}


def button_style(button: dict[str, Any]) -> dict[str, Any] | None:
    style: dict[str, Any] = {"_": "keyboardButtonStyle"}
    flag = STYLES.get(str(button.get("style") or ""))
    if flag:
        style[flag] = True
    icon = button.get("icon_custom_emoji_id")
    if icon is not None and str(icon).isdigit():
        style["icon"] = int(icon)
    return style if len(style) > 1 else None


def to_button(button: object) -> dict[str, Any]:
    if isinstance(button, dict):
        data = cast(Dict[str, Any], button)
        if data.get("_") == "keyboardInlineButton":
            return data
    else:
        fn = getattr(type(button), "to_dict", None)
        data = fn(button) if fn is not None else {"text": str(button)}
    result: dict[str, Any] = {
        "_": "keyboardInlineButton",
        "text": _clip(str(data.get("text", "")) or " ", TEXT_LIMIT),
        "type": button_type(data),
    }
    style = button_style(data)
    if style is not None:
        result["style"] = style
    return result


def rows_of(markup: Any) -> list[list[Any]] | None:
    if isinstance(markup, KbdBuilder):
        markup = markup.build()
    if isinstance(markup, list):
        items = cast(List[Any], markup)
        return [cast(List[Any], row) if isinstance(row, list) else [row] for row in items]
    if isinstance(markup, dict):
        rows = cast(Dict[str, Any], markup).get("inline_keyboard")
        if isinstance(rows, list):
            return [cast(List[Any], row) if isinstance(row, list) else [row] for row in cast(List[Any], rows) if row is not None]
    return None


def kbd_to_tl(markup: Any) -> dict[str, Any] | None:
    """Drop-in replacement for ``goygram.types.kbd.kbd_to_tl``."""
    if isinstance(markup, dict) and cast(Dict[str, Any], markup).get("_") in {"replyInlineMarkup", "replyKeyboardMarkup", "replyKeyboardHide", "replyForceReply"}:
        return cast(Dict[str, Any], markup)
    rows = rows_of(markup)
    if rows is None:
        from goygram.types.kbd import kbd_to_tl as upstream
        return upstream(markup)
    tl_rows: list[dict[str, Any]] = []
    for row in rows:
        buttons = [to_button(item) for item in row if item is not None]
        for start in range(0, len(buttons), ROW_LIMIT):
            chunk = buttons[start:start + ROW_LIMIT]
            if chunk:
                tl_rows.append({"_": "keyboardInlineButtonRow", "buttons": chunk})
    return {"_": "replyInlineMarkup", "rows": tl_rows}
