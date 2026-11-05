from __future__ import annotations

import re
from typing import Any, cast

from goygram.sugar import html_to_entities

_CUSTOM = "messageEntityCustomEmoji"
_LINK = "messageEntityTextUrl"
_PROTECTED = ("messageEntityPre", "messageEntityCode")
_ENTITY_LIMIT = 100
_LINK_LIMIT = 48

_premium: bool | None = None


def set_premium(value: bool | None) -> None:
    global _premium
    _premium = value


def premium() -> bool:
    return bool(_premium)


def degrade(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not any(entity.get("_") == _CUSTOM for entity in entities):
        return entities
    protected = [
        (cast(int, entity["offset"]), cast(int, entity["offset"]) + cast(int, entity["length"]))
        for entity in entities
        if entity.get("_") in _PROTECTED
    ]
    other = sum(1 for entity in entities if entity.get("_") != _CUSTOM)
    budget = max(0, min(_LINK_LIMIT, _ENTITY_LIMIT - 1 - other))
    result: list[dict[str, Any]] = []
    for entity in entities:
        if entity.get("_") != _CUSTOM:
            result.append(entity)
            continue
        start = cast(int, entity["offset"])
        end = start + cast(int, entity["length"])
        if any(low <= start and end <= high for low, high in protected):
            result.append(entity)
            continue
        if budget <= 0:
            continue
        result.append({"_": _LINK, "offset": start, "length": entity["length"], "url": f"tg://emoji?id={entity['document_id']}"})
        budget -= 1
    return result


_quote = re.compile(r"<blockquote(\s+expandable)?\s*>", re.IGNORECASE)


def _collapse(html: str, entities: list[dict[str, Any]]) -> None:
    flags = [bool(match.group(1)) for match in _quote.finditer(html)]
    if not any(flags):
        return
    quotes = sorted((entity for entity in entities if entity.get("_") == "messageEntityBlockquote"), key=lambda entity: cast(int, entity["offset"]))
    for entity, collapsed in zip(quotes, flags):
        if collapsed:
            entity["collapsed"] = True


def to_entities(text: str) -> tuple[str, list[dict[str, Any]]]:
    html = str(text)
    plain, entities = html_to_entities(_quote.sub("<blockquote>", html))
    _collapse(html, entities)
    if not (_premium or not entities):
        entities = degrade(entities)
    tail = plain.rstrip()
    if tail == plain or not entities:
        return plain, entities
    end = len(tail.encode("utf-16-le")) // 2
    kept: list[dict[str, Any]] = []
    for entity in entities:
        offset = cast(int, entity["offset"])
        length = min(offset + cast(int, entity["length"]), end) - offset
        if length > 0:
            kept.append({**entity, "offset": offset, "length": length})
    return tail, kept

_TG_EMOJI = re.compile(r'''<tg-emoji\s+emoji-id\s*=\s*(?P<quote>["']?)(?P<id>[0-9]+)(?P=quote)\s*>(?P<text>.*?)</tg-emoji\s*>''', re.DOTALL | re.IGNORECASE)
_PROTECTED_HTML = re.compile(r"<(pre|code)\b.*?</\1>", re.DOTALL | re.IGNORECASE)


def has_emoji(html: str) -> bool:
    return bool(_TG_EMOJI.search(str(html)))


_button = re.compile(r"<tg-button\s[^>]*>.*?</tg-button>", re.DOTALL | re.IGNORECASE)


def _emoji_links(html: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return f'<a href="tg://emoji?id={match.group("id")}">{match.group("text")}</a>'

    def plain(match: re.Match[str]) -> str:
        return _TG_EMOJI.sub(lambda emoji: emoji.group("text"), match.group(0))

    html = _button.sub(plain, html)

    parts: list[str] = []
    pos = 0
    for guard in _PROTECTED_HTML.finditer(html):
        parts.append(_TG_EMOJI.sub(replace, html[pos:guard.start()]))
        parts.append(guard.group(0))
        pos = guard.end()
    parts.append(_TG_EMOJI.sub(replace, html[pos:]))
    return "".join(parts)


def _format_rich_html(html: str) -> str:
    pres: list[str] = []
    def save_pre(m: re.Match[str]) -> str:
        pres.append(m.group(0))
        return f"\x00PRE{len(pres)-1}\x00"

    text = re.sub(r"<pre\b[^>]*>.*?</pre>", save_pre, html, flags=re.DOTALL | re.IGNORECASE)

    def clean_table(m: re.Match[str]) -> str:
        return re.sub(r">\s+<", "><", m.group(0))

    text = re.sub(r"<table\b[^>]*>.*?</table>", clean_table, text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"(</?(?:table|tbody|thead|tfoot|tr|td|th|blockquote|hr|details|summary|tg-button-row)>|<(?:img|video)\b[^>]*\/?>)\s*\n+", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\n+\s*(</?(?:table|tbody|thead|tfoot|tr|td|th|blockquote|hr|details|summary|tg-button-row)>|<(?:img|video)\b[^>]*\/?>)", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"(\x00PRE\d+\x00)\s*\n+", r"\1", text)
    text = re.sub(r"\n+\s*(\x00PRE\d+\x00)", r"\1", text)

    text = re.sub(r"\n(?![^<]*>)", "<br>", text)

    for i, pre_content in enumerate(pres):
        text = text.replace(f"\x00PRE{i}\x00", pre_content)

    return text


def to_rich(html: str) -> dict[str, str]:
    formatted = _format_rich_html(str(html))
    if not _premium:
        formatted = _emoji_links(formatted)
    return {"html": formatted}
