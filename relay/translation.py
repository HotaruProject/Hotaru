from __future__ import annotations

import asyncio
import json
import math
import re
import weakref
from typing import Any, Awaitable, Callable, Dict, cast
from urllib.parse import urlencode


Request = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]
Telegram = Callable[[str, str, str], Awaitable[str]]
providers = ("auto", "telegram", "google", "mymemory")
active: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, int] = weakref.WeakKeyDictionary()


class TranslationError(RuntimeError):
    pass


def language(value: object, *, auto: bool = False) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}|auto", value):
        raise ValueError("invalid translation language code")
    if value == "auto" and not auto:
        raise ValueError("target language cannot be auto")
    return value


def result(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TranslationError("provider returned no translated text")
    if len(value.encode("utf-8")) > limit:
        raise TranslationError("translated text exceeds response limit")
    return value


def decode(name: str, raw: str, limit: int) -> str:
    try:
        data: Any = json.loads(raw)
    except (ValueError, RecursionError):
        raise TranslationError("provider returned invalid JSON") from None
    if name == "google":
        if not isinstance(data, list) or not data or not isinstance(data[0], list) or not data[0]:
            raise TranslationError("invalid Google translation response")
        parts: list[str] = []
        for segment in cast("list[Any]", data[0]):
            if not isinstance(segment, list) or not segment or not isinstance(segment[0], str):
                raise TranslationError("incomplete Google translation response")
            parts.append(segment[0])
        return result("".join(parts), limit)
    if not isinstance(data, dict):
        raise TranslationError("invalid MyMemory translation response")
    body = cast("dict[str, Any]", data)
    if body.get("responseStatus") not in (200, "200") or body.get("quotaFinished"):
        raise TranslationError("MyMemory rejected translation or exhausted quota")
    response = body.get("responseData")
    if not isinstance(response, dict):
        raise TranslationError("invalid MyMemory translation response")
    return result(cast("dict[str, Any]", response).get("translatedText"), limit)


async def translate(
    text: str,
    to_lang: str,
    *,
    source: str = "auto",
    provider: str = "auto",
    request: Request | None = None,
    telegram: Telegram | None = None,
    timeout: float = 15.0,
    attempt_timeout: float = 7.0,
    max_bytes: int = 65536,
) -> str:
    if not isinstance(cast(object, text), str) or not text.strip():
        raise ValueError("translation requires non-empty text")
    size = len(text.encode("utf-8"))
    if size > 4000:
        raise ValueError("translation input exceeds 4000 UTF-8 bytes; split explicitly")
    source = language(source, auto=True)
    to_lang = language(to_lang)
    if provider not in providers:
        raise ValueError("unknown translation provider")
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("timeout must be between 0 and 60 seconds")
    if not math.isfinite(attempt_timeout) or not 0 < attempt_timeout <= 30:
        raise ValueError("attempt_timeout must be between 0 and 30 seconds")
    if isinstance(max_bytes, bool) or not isinstance(cast(object, max_bytes), int) or not 1 <= max_bytes <= 262144:
        raise ValueError("max_bytes must be between 1 and 262144")
    if provider == "mymemory" and (source == "auto" or size > 500):
        raise ValueError("MyMemory requires explicit source and at most 500 UTF-8 bytes")
    if provider == "telegram" and telegram is None:
        raise ValueError("telegram translation callback is required")
    if provider in ("google", "mymemory") and request is None:
        raise ValueError("a capability-backed async request callable is required")
    if provider == "auto":
        choices = (["telegram"] if telegram is not None else [])
        if request is not None:
            choices.append("google")
            if source != "auto" and size <= 500:
                choices.append("mymemory")
        choices = choices[:2]
        if not choices:
            raise ValueError("translation requires request or telegram callback")
    else:
        choices = [provider]
    loop = asyncio.get_running_loop()
    if active.get(loop, 0) >= 4:
        raise TranslationError("translation concurrency limit reached")
    active[loop] = active.get(loop, 0) + 1
    deadline = loop.time() + timeout
    failures: list[str] = []
    try:
        for name in choices:
            remaining = deadline - loop.time()
            if remaining <= 0:
                failures.append("total timeout")
                break
            budget = min(remaining, attempt_timeout)
            try:
                if name == "telegram":
                    if telegram is None:
                        raise TranslationError("telegram callback unavailable")
                    return result(await asyncio.wait_for(telegram(text, to_lang, source), budget), max_bytes)
                if request is None:
                    raise TranslationError("request callback unavailable")
                if name == "google":
                    url = "https://translate.googleapis.com/translate_a/single?" + urlencode(
                        {"client": "gtx", "sl": source, "tl": to_lang, "dt": "t", "q": text}
                    )
                else:
                    url = "https://api.mymemory.translated.net/get?" + urlencode(
                        {"q": text, "langpair": source + "|" + to_lang}
                    )
                response = await asyncio.wait_for(
                    request({"url": url, "timeout": budget, "max_bytes": max_bytes, "allow_redirects": False}),
                    budget,
                )
                if response.get("status") != 200:
                    raise TranslationError("provider returned non-200 HTTP status")
                raw = response.get("body")
                if response.get("truncated") or not isinstance(raw, str) or len(raw.encode("utf-8")) > max_bytes:
                    raise TranslationError("provider response missing or exceeds response limit")
                return decode(name, raw, max_bytes)
            except (TranslationError, OSError, asyncio.TimeoutError) as exc:
                if isinstance(exc, PermissionError):
                    raise
                failures.append(name + ": " + (str(exc) if isinstance(exc, TranslationError) else type(exc).__name__))
        raise TranslationError("translation failed (" + "; ".join(failures) + ")") from None
    finally:
        count = active.get(loop, 1) - 1
        if count:
            active[loop] = count
        else:
            _ = active.pop(loop, None)
