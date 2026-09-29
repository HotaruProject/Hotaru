from __future__ import annotations

from typing import Any, cast

from goygram.types.kbd import KbdBuilder

row_limit = 8
copy_limit = 256
_styles = {"primary": "bg_primary", "danger": "bg_danger", "success": "bg_success"}


def _bytes(value: Any) -> bytes:
    return bytes(value) if isinstance(value, (bytes, bytearray)) else str(value).encode()


def button_type(button: dict[str, Any]) -> dict[str, Any]:
    kind = button.get("type")
    if isinstance(kind, dict) and cast('dict[str, Any]', kind).get("_"):
        return cast('dict[str, Any]', kind)
    if button.get("disabled"):
        return {"_": "inlineButtonTypeDisabled"}
    if button.get("callback_data") is not None:
        data = _bytes(button["callback_data"])
        if len(data) > 64:
            raise ValueError("callback_data exceeds 64 bytes")
        return {"_": "inlineButtonTypeCallback", "data": data}
    if button.get("url") is not None:
        return {"_": "inlineButtonTypeUrl", "url": str(button["url"])}
    if button.get("copy_text") is not None:
        return {"_": "inlineButtonTypeCopy", "copy_text": (str(button["copy_text"]) or " ")[:copy_limit]}
    if button.get("web_app") is not None:
        app = button["web_app"]
        url = cast('dict[str, Any]', app).get("url", "") if isinstance(app, dict) else app
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
    flag = _styles.get(str(button.get("style") or ""))
    if flag:
        style[flag] = True
    icon = button.get("icon_custom_emoji_id")
    if icon is not None and str(icon).isdigit():
        style["icon"] = int(icon)
    return style if len(style) > 1 else None


def to_button(button: object) -> dict[str, Any]:
    if isinstance(button, dict):
        data = cast('dict[str, Any]', button)
        if data.get("_") == "keyboardInlineButton":
            return data
    else:
        fn = getattr(type(button), "to_dict", None)
        data = cast('dict[str, Any]', fn(button)) if fn is not None else {"text": str(button)}
    text = str(data.get("text", "")) or " "
    result: dict[str, Any] = {"_": "keyboardInlineButton", "text": text if len(text) <= 64 else text[:63] + "…", "type": button_type(data)}
    style = button_style(data)
    if style is not None:
        result["style"] = style
    return result


def rows_of(markup: Any) -> list[list[Any]] | None:
    if isinstance(markup, KbdBuilder):
        markup = markup.build()
    if isinstance(markup, dict):
        markup = cast('dict[str, Any]', markup).get("inline_keyboard")
    if not isinstance(markup, list):
        return None
    return [cast('list[Any]', row) if isinstance(row, list) else [row] for row in cast('list[Any]', markup) if row is not None]


def kbd_to_tl(markup: Any) -> dict[str, Any] | None:
    if isinstance(markup, dict) and cast('dict[str, Any]', markup).get("_") in {"replyInlineMarkup", "replyKeyboardMarkup", "replyKeyboardHide", "replyForceReply"}:
        return cast('dict[str, Any]', markup)
    rows = rows_of(markup)
    if rows is None:
        from goygram.types.kbd import kbd_to_tl as upstream
        return upstream(markup)
    out: list[dict[str, Any]] = []
    for row in rows:
        buttons = [to_button(item) for item in row if item is not None]
        out += [{"_": "keyboardInlineButtonRow", "buttons": buttons[i:i + row_limit]} for i in range(0, len(buttons), row_limit)]
    return {"_": "replyInlineMarkup", "rows": out}
