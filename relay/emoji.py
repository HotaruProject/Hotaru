from __future__ import annotations

import re
from typing import Any, cast

from goygram.rich import rich_html
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


_QUOTE_OPEN = re.compile(r"<blockquote(\s+expandable)?\s*>", re.IGNORECASE)


def _collapse(html: str, entities: list[dict[str, Any]]) -> None:
    flags = [bool(match.group(1)) for match in _QUOTE_OPEN.finditer(html)]
    if not any(flags):
        return
    quotes = sorted((entity for entity in entities if entity.get("_") == "messageEntityBlockquote"), key=lambda entity: cast(int, entity["offset"]))
    for entity, collapsed in zip(quotes, flags):
        if collapsed:
            entity["collapsed"] = True


def to_entities(text: str) -> tuple[str, list[dict[str, Any]]]:
    html = str(text)
    plain, entities = html_to_entities(_QUOTE_OPEN.sub("<blockquote>", html))
    _collapse(html, entities)
    if _premium or not entities:
        return plain, entities
    return plain, degrade(entities)

_TG_EMOJI = re.compile(r'<tg-emoji emoji-id="(\d+)">(.*?)</tg-emoji>', re.DOTALL)
_PROTECTED_HTML = re.compile(r"<(pre|code)\b.*?</\1>", re.DOTALL | re.IGNORECASE)


def has_emoji(html: str) -> bool:
    return bool(_TG_EMOJI.search(str(html)))


def _emoji_links(html: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return f'<a href="tg://emoji?id={match.group(1)}">{match.group(2)}</a>'

    parts: list[str] = []
    pos = 0
    for guard in _PROTECTED_HTML.finditer(html):
        parts.append(_TG_EMOJI.sub(replace, html[pos:guard.start()]))
        parts.append(guard.group(0))
        pos = guard.end()
    parts.append(_TG_EMOJI.sub(replace, html[pos:]))
    return "".join(parts)


def to_rich(html: str) -> dict[str, str]:
    built = rich_html(str(html))
    value = built.get("html")
    if _premium or not isinstance(value, str):
        return built
    built["html"] = _emoji_links(value)
    return built
