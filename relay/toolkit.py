from __future__ import annotations
import asyncio
import heapq
import unicodedata
import hashlib
import html as _html_mod
from html.parser import HTMLParser
import json
import random
import re
import shlex
import string
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlparse

_BOOT_TS = time.perf_counter()

_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9\-]*)(?:\s[^<>]*)?>")
_TG_TAGS = frozenset({"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote", "tg-spoiler", "tg-emoji", "tg-button", "tg-button-row", "tg-time", "tg-math", "br", "p", "details", "summary", "h1", "h2", "h3", "ul", "ol", "li"})
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"
    "\U0001f300-\U0001f5ff"
    "\U0001f680-\U0001f6ff"
    "\U0001f1e0-\U0001f1ff"
    "\U00002702-\U000027b0"
    "\U000024c2-\U0001f251"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "\U00002600-\U000026ff"
    "\U0000fe00-\U0000fe0f"
    "\U0000200d"
    "]+",
    flags=re.UNICODE,
)
_FACES = [
    "ヽ(๑◠ܫ◠๑)ﾉ", "(◕ᴥ◕ʋ)", "ᕙ(`▽´)ᕗ", "(✿◠‿◠)", "(▰˘◡˘▰)",
    "(˵ ͡° ͜ʖ ͡°˵)", "ʕっ•ᴥ•ʔっ", "( ͡° ᴥ ͡°)", "(๑•́ ヮ •̀๑)", "٩(^‿^)۶",
    "(っˆڡˆς)", "ψ(｀∇´)ψ", "⊙ω⊙", "٩(^ᴗ^)۶", "(´・ω・)っ由",
    "( ͡~ ͜ʖ ͡°)", "✧♡(◕‿◕✿)", "∩｡• ᵕ •｡∩ ♡", "(♡´౪`♡)", "(◍＞◡＜◍)⋈。✧♡",
    "╰(✿´⌣`✿)╯♡", "ʕ•ᴥ•ʔ", "ᶘ ◕ᴥ◕ᶅ", "▼・ᴥ・▼", "ฅ^•ﻌ•^ฅ",
    "(΄◞ิ౪◟ิ‵)", "ᕴｰᴥｰᕵ", "ʕ￫ᴥ￩ʔ", "ʕᵕᴥᵕʔ", "ʕᵒᴥᵒʔ",
    "ᵔᴥᵔ", "(✿╹◡╹)", "(๑￫ܫ￩)", "ʕ·ᴥ· ʔ", "(ﾉ≧ڡ≦)",
    "(≖ᴗ≖✿)", "（〜^∇^ )〜", "( ﾉ･ｪ･ )ﾉ", "~( ˘▾˘~)", "(〜^∇^)〜",
    "ヽ(^ᴗ^ヽ)", "(´･ω･`)", "₍ᐢ•ﻌ•ᐢ₎*･ﾟ｡", "(。・・)_且", "(=｀ω´=)",
    "(*•‿•*)", "(*ﾟ∀ﾟ*)", "(☉⋆‿⋆☉)", "ɷ◡ɷ", "ʘ‿ʘ",
    "(。-ω-)ﾉ", "( ･ω･)ﾉ", "(=ﾟωﾟ)ﾉ", "(・ε・`*)", "(*˘︶˘*)",
    "ಥ_ಥ", "･ﾟ･(｡>д<｡)･ﾟ･", "(┬┬＿┬┬)", "(◞‸◟ㆀ)",
]


def args_parse(text: str) -> list[str]:
    if not text:
        return []
    parts = text.split(maxsplit=1)
    if len(parts) <= 1:
        return []
    try:
        return [x for x in shlex.split(parts[1]) if x]
    except ValueError:
        return [parts[1]]


def args_raw(text: str) -> str:
    if not text:
        return ""
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def args_split(text: str, separator: str | list[str]) -> list[str]:
    raw = args_raw(text)
    if isinstance(separator, str):
        sections = raw.split(separator)
    else:
        sections = [raw]
        for sep in separator:
            new: list[str] = []
            for s in sections:
                new.extend(s.split(sep))
            sections = new
    return [s.strip() for s in sections if s.strip()]


def args_int(text: str) -> list[int]:
    result: list[int] = []
    for arg in args_parse(text):
        try:
            result.append(int(arg))
        except ValueError:
            pass
    return result


def args_bool(text: str) -> list[bool]:
    result: list[bool] = []
    for arg in args_parse(text):
        low = arg.lower()
        if low in ("true", "yes", "1", "on", "y", "да", "вкл"):
            result.append(True)
        elif low in ("false", "no", "0", "off", "n", "нет", "выкл"):
            result.append(False)
    return result


def args_float(text: str) -> list[float]:
    result: list[float] = []
    for arg in args_parse(text):
        try:
            result.append(float(arg))
        except ValueError:
            pass
    return result


def escape(value: Any) -> str:
    return _html_mod.escape(str(value), quote=False)


def escape_attr(value: Any) -> str:
    return _html_mod.escape(str(value), quote=True)


def escape_smart(text: str) -> str:
    out: list[str] = []
    last = 0
    for m in _TAG_RE.finditer(text):
        out.append(escape(text[last:m.start()]))
        if m.group(1).lower() in _TG_TAGS:
            out.append(m.group(0))
        else:
            out.append(escape(m.group(0)))
        last = m.end()
    out.append(escape(text[last:]))
    return "".join(out)


def strip_tags(text: str, keep_tg: bool = True) -> str:
    if keep_tg:
        pattern = r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|blockquote|tg-spoiler|tg-emoji)(?:\s[^>]*)?>"
        return re.sub(r"<[^>]+>", "", re.sub(pattern, lambda m: m.group(0), text))
    return re.sub(r"<[^>]+>", "", text)


def strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text)


def flag(code: str) -> str:
    clean = [c for c in code.lower() if c in string.ascii_lowercase]
    if len(clean) == 2:
        return "".join(chr(ord(c.upper()) + (ord("\U0001f1e6") - ord("A"))) for c in clean)
    return code


def entity_url(entity: object, openmessage: bool = False) -> str:
    if isinstance(entity, dict):
        data = cast('dict[str, Any]', entity)
        eid = data.get("user_id") or data.get("id") or data.get("channel_id")
        username = data.get("username")
        kind = data.get("_", "")
    else:
        eid = getattr(entity, "id", None)
        username = getattr(entity, "username", None)
        kind = type(entity).__name__
    if "user" in str(kind).lower() or (isinstance(entity, dict) and "user" in str(cast('dict[str, Any]', entity).get("_", "")).lower()):
        return f"tg://openmessage?id={eid}" if openmessage else f"tg://user?id={eid}"
    if username:
        return f"tg://resolve?domain={username}"
    return ""


def entity_link(entity: object, label: str | None = None) -> str:
    url = entity_url(entity)
    if not url:
        return escape(label or "")
    name = label or str(getattr(entity, "username", None) or getattr(entity, "id", ""))
    if isinstance(entity, dict):
        data = cast('dict[str, Any]', entity)
        name = label or str(data.get("username") or data.get("id") or data.get("first_name") or "")
    return f'<a href="{escape_attr(url)}">{escape(name)}</a>'


def valid_url(url: str) -> bool:
    try:
        return bool(urlparse(url).netloc)
    except Exception:
        return False


def is_url(text: str) -> bool:
    pattern = re.compile(
        r"^https?://"
        r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|"
        r"localhost|"
        r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
        r"(?::\d+)?"
        r"(?:/?|[/?]\S+)$",
        re.IGNORECASE,
    )
    return pattern.match(text) is not None


def urls_extract(text: str) -> list[str]:
    return _URL_RE.findall(text)


def chunk(items: list[Any] | tuple[Any, ...] | str, size: int) -> list[Any]:
    if size < 1:
        raise ValueError("chunk size must be positive")
    return [items[i:i + size] for i in range(0, len(items), size)]


def random_str(size: int, charset: str | None = None) -> str:
    if size < 1:
        return ""
    pool = charset or string.ascii_lowercase + string.digits
    return "".join(random.choice(pool) for _ in range(size))


def file_size(size_bytes: int | float) -> str:
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    val = float(size_bytes)
    while val >= 1024 and i < len(units) - 1:
        val /= 1024.0
        i += 1
    return f"{val:.1f} {units[i]}" if i > 0 else f"{int(val)} B"


def iso_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def merge_dicts(a: dict[str, Any], b: dict[str, Any], *, deep: bool = True) -> dict[str, Any]:
    for key, a_val in a.items():
        b_val = b.get(key)
        if key not in b:
            b[key] = a_val
        elif deep and isinstance(a_val, dict) and isinstance(b_val, dict):
            b[key] = merge_dicts(cast('dict[str, Any]', a_val), cast('dict[str, Any]', b_val), deep=True)
        elif isinstance(a_val, list) and isinstance(b_val, list):
            b[key] = list(dict.fromkeys(cast('list[Any]', b_val) + cast('list[Any]', a_val)))
        else:
            b[key] = a_val
    return b


def json_ok(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except Exception:
        return False


def flatten(nested: list[Any]) -> list[Any]:
    result: list[Any] = []
    for item in nested:
        if isinstance(item, list):
            result.extend(cast('list[Any]', item))
        else:
            result.append(item)
    return result


def censor(obj: Any, fields: list[str] | None = None, replacement: str = "***") -> Any:
    if fields is None:
        fields = ["phone", "password", "token", "secret", "api_hash", "api_id"]
    if isinstance(obj, dict):
        return {k: (replacement if k.lower() in [f.lower() for f in fields] and isinstance(v, str) else censor(v, fields, replacement)) for k, v in cast('dict[str, Any]', obj).items()}
    if isinstance(obj, list):
        return [censor(item, fields, replacement) for item in cast('list[Any]', obj)]
    return obj


def attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def face() -> str:
    return escape(random.choice(_FACES))


def html_fix(text: str) -> str:
    return escape_smart(text)


def chat_id(message: Any) -> int | None:
    cid = getattr(message, "chat_id", None)
    if cid is None and hasattr(message, "get"):
        cid = message.get("chat_id")
    if isinstance(cid, int) and cid < -1000000000000:
        return int(str(cid)[4:])
    return cid


def entity_id(entity: Any) -> int | None:
    if isinstance(entity, dict):
        data = cast('dict[str, Any]', entity)
        value = data.get("channel_id") or data.get("chat_id") or data.get("user_id") or data.get("id")
        return cast(int | None, value)
    return getattr(entity, "id", None)


def topic_id(message: Any) -> int | None:
    for name in ("topic_id", "message_thread_id", "top_msg_id"):
        val = getattr(message, name, None)
        if isinstance(val, int) and val > 0:
            return val
    if hasattr(message, "get"):
        for name in ("topic_id", "message_thread_id", "top_msg_id"):
            val = message.get(name)
            if isinstance(val, int) and val > 0:
                return val
    return None


def mime(message: Any) -> str:
    if hasattr(message, "get"):
        media = message.get("media") or message.get("document")
        if isinstance(media, dict):
            value = cast('dict[str, Any]', media).get("mime_type", "")
            return cast(str, value)
    m = getattr(message, "media", None)
    if isinstance(m, dict):
        value = cast('dict[str, Any]', m).get("mime_type", "")
        return cast(str, value)
    if m is not None:
        return getattr(m, "mime_type", "") or ""
    return ""


def msg_link(message: Any, chat: Any = None) -> str:
    mid = getattr(message, "id", None) or (message.get("id") if hasattr(message, "get") else None)
    cid = chat_id(message)
    if cid is None or mid is None:
        return ""
    if cid > 0:
        return f"tg://openmessage?user_id={cid}&message_id={mid}"
    username = getattr(chat, "username", None) if chat else None
    if username:
        return f"https://t.me/{username}/{mid}"
    return f"https://t.me/c/{cid}/{mid}"


def has_media(message: Any) -> bool:
    if hasattr(message, "get"):
        for kind in ("photo", "document", "video", "audio", "voice", "sticker", "animation"):
            if message.get(kind):
                return True
        media = message.get("media")
        return isinstance(media, dict)
    return getattr(message, "media", None) is not None


def target_id(message: Any, arg_index: int = 0) -> int | None:
    entities = cast('list[object]' | tuple[object, ...] | None, getattr(message, "entities", None))
    if entities:
        for ent in entities:
            if isinstance(ent, dict):
                data = cast('dict[str, Any]', ent)
                kind = data.get("_", "")
                uid = data.get("user_id")
            else:
                kind = getattr(ent, "_", "")
                uid = getattr(ent, "user_id", None)
            if "mentionname" in str(kind).lower() and uid:
                return cast(int, uid)
    args = args_parse(getattr(message, "text", "") or (message.get("text", "") if hasattr(message, "get") else ""))
    if len(args) > arg_index:
        try:
            return int(args[arg_index].lstrip("@"))
        except ValueError:
            pass
    return None


def render(template: str, **data: Any) -> str:
    result = template
    for key, value in data.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def _template_format(value: Any, spec: str) -> str:
    if len(spec) > 32 or "{" in spec or "}" in spec:
        raise ValueError("Nested or oversized format specifications are not allowed")
    if spec and type(value) not in (int, float):
        raise ValueError("Format specifications require a numeric field")
    if spec and not re.fullmatch(r"(?:[^{}][<>=^]|[<>=^])?[+ -]?#?0?\d{0,3}[_,]?(?:\.\d{1,3})?[bcdeEfFgGnosxX%]?", spec):
        raise ValueError("Unsupported numeric format specification")
    if any(int(size) > 256 for size in re.findall(r"\d+", spec)):
        raise ValueError("Format width and precision must not exceed 256")
    try:
        return format(value, spec)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid numeric format: {spec}") from exc


def _template_value(value: Any, kind: str) -> bool:
    return ((kind == "str" and type(value) is str and len(value) <= 16000)
            or (kind == "int" and type(value) is int and value.bit_length() <= 256)
            or (kind == "float" and type(value) in (int, float) and -1e100 <= cast(float, value) <= 1e100)
            or (kind == "bool" and type(value) is bool))


def _template_parts(template: str, declared: dict[str, Any], legacy_braces: bool) -> list[tuple[str, str | None, str | None, str | None]]:
    try:
        parts = list(string.Formatter().parse(template))
        for _, name, _, conversion in parts:
            if name is not None and (name not in declared or conversion is not None):
                raise ValueError(f"Unknown or unsafe template field {{{name}}}; available: " + ", ".join(declared))
    except ValueError:
        if not legacy_braces:
            raise
        escaped = template.replace("{", "{{").replace("}", "}}")
        for name in declared:
            escaped = escaped.replace("{{" + name + "}}", "{" + name + "}")
        parts = list(string.Formatter().parse(escaped))
    if len(parts) > 256:
        raise ValueError("Template has too many fields or escaped braces (maximum 256)")
    return parts


def template_names(template: str, fields: dict[str, Any] | None = None, *, legacy_braces: bool = False) -> list[str]:
    if fields is not None:
        return list(dict.fromkeys(name for _, name, _, _ in _template_parts(template, fields, legacy_braces) if name is not None))
    try:
        return list(dict.fromkeys(name for _, name, _, _ in string.Formatter().parse(template) if name is not None))
    except ValueError:
        if not legacy_braces:
            raise
        return list(dict.fromkeys(re.findall(r"\{([a-z0-9][a-z0-9-]{0,63}/[A-Za-z][A-Za-z0-9_]{0,63})\}", template)))


def template_validate(template: object, fields: object) -> str:
    declared = {} if fields == {} else template_fields(fields)
    if not isinstance(template, str) or len(template) > 4096:
        raise ValueError("Template must be text of at most 4096 characters")
    for _, name, spec, conversion in string.Formatter().parse(template):
        if name is not None and name not in declared and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}/[A-Za-z][A-Za-z0-9_]{0,63}", name):
            if conversion is not None:
                raise ValueError("Template conversions are not allowed")
            declared[name] = {"type": "int" if spec else "str", "example": 0 if spec else "", "description": name}
    return template_render(template, declared)


def template_fields(raw: object) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or not 1 <= len(cast('dict[object, object]', raw)) <= 64:
        raise ValueError("template_fields must declare between 1 and 64 fields")
    result: dict[str, dict[str, Any]] = {}
    for name, entry in cast('dict[object, object]', raw).items():
        if not isinstance(name, str) or not re.fullmatch(r"(?:[a-z0-9][a-z0-9-]{0,63}/)?[A-Za-z][A-Za-z0-9_]{0,63}", name):
            raise ValueError("Invalid template field name")
        if not isinstance(entry, dict):
            raise ValueError(f"{name}: field metadata must be a mapping")
        meta = cast('dict[str, Any]', entry)
        if set(meta) - {"type", "description", "description_key", "example", "example_key", "format"}:
            raise ValueError(f"{name}: unknown template field metadata")
        kind = meta.get("type", "str")
        if kind not in ("str", "int", "float", "bool") or not _template_value(meta.get("example"), kind):
            raise ValueError(f"{name}: example must match type str/int/float/bool within size limits")
        description = meta.get("description", "")
        if isinstance(description, dict):
            descriptions = cast('dict[object, object]', description)
            if not descriptions or any(lang not in ("en", "ru", "kz", "uk", "ja") or not isinstance(text, str) or len(text) > 1000 for lang, text in descriptions.items()):
                raise ValueError(f"{name}: invalid localized description")
        elif not isinstance(description, str) or len(description) > 1000:
            raise ValueError(f"{name}: description must be text or a locale mapping")
        description_key = meta.get("description_key", "")
        if not isinstance(description_key, str) or len(description_key) > 200 or not (description or description_key):
            raise ValueError(f"{name}: a description or description_key is required")
        example_key = meta.get("example_key", "")
        if not isinstance(example_key, str) or len(example_key) > 200 or (example_key and kind != "str"):
            raise ValueError(f"{name}: example_key requires a string field and a translation key")
        spec = meta.get("format", "")
        if not isinstance(spec, str):
            raise ValueError(f"{name}: format must be a string")
        try:
            _template_format(meta["example"], spec)
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from exc
        result[name] = dict(meta, type=kind)
    return result


def template_render(template: object, fields: object, values: object = None, *, legacy_braces: bool = False) -> str:
    declared = {} if fields == {} else template_fields(fields)
    if not isinstance(template, str) or len(template) > 4096:
        raise ValueError("Template must be text of at most 4096 characters")
    if values is None:
        data = {name: meta["example"] for name, meta in declared.items()}
    else:
        if not isinstance(values, dict):
            raise ValueError("Template values must be a mapping")
        data = cast('dict[str, Any]', values)
        if len(data) > 64 or any(not isinstance(key, str) for key in cast('dict[object, object]', values)):
            raise ValueError("Template values require at most 64 named keys")
    if set(data) - set(declared):
        raise ValueError("Unknown template values: " + ", ".join(sorted(set(data) - set(declared))))
    parts = _template_parts(template, declared, legacy_braces)
    output: list[str] = []
    size = 0
    for literal, name, spec, _ in parts:
        piece = literal
        if name is not None:
            if name not in data:
                raise ValueError(f"Missing template value: {name}")
            meta = declared[name]
            value = data[name]
            if not _template_value(value, meta["type"]):
                raise ValueError(f"{name}: expected {meta['type']} within size limits")
            try:
                formatted = _template_format(value, spec or meta.get("format", ""))
            except ValueError as exc:
                raise ValueError(f"{name}: {exc}") from exc
            piece += _html_mod.escape(formatted, quote=True)
        size += len(piece)
        if size > 16000:
            raise ValueError("Rendered template exceeds 16000 characters")
        output.append(piece)
    return "".join(output)


def uptime() -> int:
    return round(time.perf_counter() - _BOOT_TS)


_DURATION_UNITS = (31557600 * 10**9, 31557600 * 10**8, 31557600 * 10**6,
                   31557600 * 1000, 31557600 * 100, 31557600, 2629800, 604800, 86400, 3600, 60, 1)
_DURATION_NAMES = {
    "en": ("eon|eons", "era|eras", "epoch|epochs", "millennium|millennia", "century|centuries", "year|years", "month|months", "week|weeks", "day|days", "hour|hours", "minute|minutes", "second|seconds"),
    "ru": ("эон|эона|эонов", "эра|эры|эр", "эпоха|эпохи|эпох", "тысячелетие|тысячелетия|тысячелетий", "век|века|веков", "год|года|лет", "месяц|месяца|месяцев", "неделя|недели|недель", "день|дня|дней", "час|часа|часов", "минута|минуты|минут", "секунда|секунды|секунд"),
    "uk": ("еон|еони|еонів", "ера|ери|ер", "епоха|епохи|епох", "тисячоліття|тисячоліття|тисячоліть", "століття|століття|століть", "рік|роки|років", "місяць|місяці|місяців", "тиждень|тижні|тижнів", "день|дні|днів", "година|години|годин", "хвилина|хвилини|хвилин", "секунда|секунди|секунд"),
    "kz": ("эон", "эра", "дәуір", "мыңжылдық", "ғасыр", "жыл", "ай", "апта", "күн", "сағат", "минут", "секунд"),
    "ja": ("累代", "代", "世", "千年", "世紀", "年", "か月", "週間", "日", "時間", "分", "秒"),
}
_MONTH_NAMES = {
    "en": "January February March April May June July August September October November December",
    "ru": "января февраля марта апреля мая июня июля августа сентября октября ноября декабря",
    "uk": "січня лютого березня квітня травня червня липня серпня вересня жовтня листопада грудня",
    "kz": "қаңтар ақпан наурыз сәуір мамыр маусым шілде тамыз қыркүйек қазан қараша желтоқсан",
}


def uptime_fmt(language: str = "en") -> str:
    return duration(uptime(), language)


def duration(seconds: int | float, language: str = "en") -> str:
    remaining = max(0, int(seconds))
    language = language.lower().replace("_", "-").split("-")[0]
    language = "kz" if language == "kk" else language
    names = _DURATION_NAMES.get(language, _DURATION_NAMES["en"])
    parts: list[str] = []
    for unit_secs, labels in zip(_DURATION_UNITS, names):
        count, remaining = divmod(remaining, unit_secs)
        if not count:
            continue
        forms = labels.split("|")
        label = plural(count, *forms) if len(forms) == 3 else forms[min(count != 1, len(forms) - 1)]
        parts.append(f"{count}{'' if language == 'ja' else ' '}{label}")
    return " ".join(parts) or {"ru": "меньше секунды", "uk": "менше секунди", "kz": "бір секундтан аз", "ja": "1秒未満"}.get(language, "less than a second")


def timestamp(value: int | float, language: str = "en") -> str:
    date = datetime.fromtimestamp(value, timezone.utc)
    language = language.lower().replace("_", "-").split("-")[0]
    language = "kz" if language == "kk" else language
    if language == "ja":
        day = f"{date.year}年{date.month}月{date.day}日"
    else:
        month = _MONTH_NAMES.get(language, _MONTH_NAMES["en"]).split()[date.month - 1]
        day = f"{date.day} {month} {date.year}"
    return f"{day} · {date:%H:%M:%S} UTC"


def time_range(start: int | float, end: int | float | None = None, language: str = "en") -> str:
    first = timestamp(start, language)
    if end is None:
        return first
    last = timestamp(end, language)
    if first == last:
        return first
    if last.split(" · ")[0] == first.split(" · ")[0]:
        return first[:-4] + "–" + last.split(" · ")[1]
    return first + " — " + last


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    if limit <= len(suffix):
        return text[:limit]
    cut = text[:limit - len(suffix)].rstrip()
    if cut and cut[-1] != " ":
        space = cut.rfind(" ")
        if space >= limit // 2:
            cut = cut[:space]
    return cut + suffix


def table(rows: list[list[Any] | tuple[Any, ...] | dict[str, Any]], headers: list[str] | None = None) -> str:
    if not rows:
        return "<table></table>"
    normalized: list[list[Any]] = []
    if isinstance(rows[0], dict):
        if headers is None:
            headers = list(rows[0].keys())
        for row in rows:
            if isinstance(row, dict):
                normalized.append([row.get(h, "") for h in headers])
            else:
                normalized.append(list(row))
    else:
        for row in rows:
            normalized.append(list(row))
    parts = ["<table>"]
    if headers:
        parts.append("<tr>" + "".join(f"<td><b>{escape(h)}</b></td>" for h in headers) + "</tr>")
    for row in normalized:
        parts.append("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>")
    parts.append("</table>")
    return "".join(parts)


def progress(current: int | float, total: int | float, width: int = 10, filled: str = "█", empty: str = "░") -> str:
    if total <= 0:
        ratio = 0.0
    else:
        ratio = min(max(current / total, 0.0), 1.0)
    done = int(round(ratio * width))
    return filled * done + empty * (width - done) + f" {int(ratio * 100)}%"


def code_block(text: str, language: str = "") -> str:
    lang = f' class="language-{escape_attr(language)}"' if language else ""
    return f"<pre{lang}><code>{escape(text)}</code></pre>"


def spoiler(text: str) -> str:
    return f"<tg-spoiler>{text}</tg-spoiler>"


def blockquote(text: str, expandable: bool = False) -> str:
    attr = " expandable" if expandable else ""
    return f"<blockquote{attr}>{text}</blockquote>"


def mention(user_id: int | str, name: str | None = None) -> str:
    label = escape(name or str(user_id))
    return f'<a href="tg://user?id={user_id}">{label}</a>'


def link(label: str, url: str) -> str:
    return f'<a href="{escape_attr(url)}">{escape(label)}</a>'


def list_items(items: list[str], ordered: bool = False) -> str:
    tag = "ol" if ordered else "ul"
    inner = "".join(f"<li>{item}</li>" for item in items)
    return f"<{tag}>{inner}</{tag}>"


def kv(pairs: dict[str, Any] | list[tuple[str, Any]], bold_keys: bool = True) -> str:
    if isinstance(pairs, dict):
        pairs = list(pairs.items())
    lines: list[str] = []
    for key, value in pairs:
        k = f"<b>{escape(key)}</b>" if bold_keys else escape(key)
        lines.append(f"{k}: <code>{escape(value)}</code>")
    return "<br>".join(lines)


def tree(items: list[str], indent: str = "  ") -> str:
    if not items:
        return ""
    lines: list[str] = []
    for i, item in enumerate(items):
        prefix = "└─ " if i == len(items) - 1 else "├─ "
        lines.append(escape(prefix + str(item)))
    return "<code>" + "\n".join(lines) + "</code>"


def plural(count: int | float, one: str, few: str = "", many: str = "") -> str:
    count = abs(int(count))
    if not few:
        return one if count == 1 else one + "s"
    if not many:
        many = few
    last_two = count % 100
    if 11 <= last_two <= 19:
        return many
    last_one = count % 10
    if last_one == 1:
        return one
    if 2 <= last_one <= 4:
        return few
    return many


def percent(value: int | float, total: int | float, decimals: int = 1) -> str:
    if total == 0:
        return "0%"
    return f"{(value / total) * 100:.{decimals}f}%"


def clamp(value: int | float, minimum: int | float, maximum: int | float) -> int | float:
    return max(minimum, min(maximum, value))


def mask(value: str, visible: int = 4, char: str = "•") -> str:
    if len(value) <= visible:
        return char * len(value)
    return char * (len(value) - visible) + value[-visible:]


def hash_short(text: str, length: int = 8) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:length]


def badge(text: str, style: str = "info") -> str:
    icons = {"info": "ℹ️", "ok": "✅", "warn": "⚠️", "error": "❌", "new": "🆕"}
    icon = icons.get(style, "•")
    return f"{icon} {escape(text)}"


def countdown(seconds: int | float) -> str:
    if seconds <= 0:
        return "now"
    return duration(seconds)


def _rich_text(node: object) -> str:
    if not isinstance(node, dict):
        return escape(node) if node else ""
    node = cast('dict[str, Any]', node)
    kind = node.get("_", "")
    inner = _rich_text(node.get("text")) if node.get("text") is not None else ""
    if kind == "textPlain":
        return escape(node.get("text") or "")
    if kind == "textConcat":
        return "".join(_rich_text(item) for item in cast('list[Any]', node.get("texts") or []))
    if kind == "textEmpty":
        return ""
    if kind == "textBold":
        return f"<b>{inner}</b>"
    if kind == "textItalic":
        return f"<i>{inner}</i>"
    if kind == "textUnderline":
        return f"<u>{inner}</u>"
    if kind == "textStrike":
        return f"<s>{inner}</s>"
    if kind == "textFixed":
        return f"<code>{inner}</code>"
    if kind == "textSpoiler":
        return f"<tg-spoiler>{inner}</tg-spoiler>"
    if kind == "textMarked":
        return f"<mark>{inner}</mark>"
    if kind == "textSubscript":
        return f"<sub>{inner}</sub>"
    if kind == "textSuperscript":
        return f"<sup>{inner}</sup>"
    if kind == "textUrl":
        return f'<a href="{escape_attr(node.get("url") or "")}">{inner}</a>'
    if kind == "textEmail":
        return f'<a href="mailto:{escape_attr(node.get("email") or "")}">{inner}</a>'
    if kind == "textAnchor":
        return f'<a name="{escape_attr(node.get("name") or "")}"></a>'
    if kind == "textMath":
        return f"<tg-math>{escape(node.get('source') or '')}</tg-math>"
    return inner


def _rich_caption(node: object) -> str:
    if isinstance(node, dict):
        caption = cast('dict[str, Any]', node)
        if caption.get("_") == "pageCaption":
            return "<br>".join(part for part in (_rich_text(caption.get("text")), _rich_text(caption.get("credit"))) if part)
        return _rich_text(caption)
    return _rich_text(node) if node is not None else ""


def _rich_block(block: object) -> str:
    if not isinstance(block, dict):
        return ""
    block = cast('dict[str, Any]', block)
    kind = block.get("_", "")
    text = _rich_text(block.get("text")) if block.get("text") is not None else ""
    if kind == "pageBlockParagraph":
        return f"<p>{text}</p>"
    if kind == "pageBlockTitle":
        return f"<h1>{text}</h1>"
    if kind == "pageBlockSubtitle":
        return f"<h2>{text}</h2>"
    if kind == "pageBlockHeader":
        return f"<h3>{text}</h3>"
    if kind == "pageBlockSubheader":
        return f"<h4>{text}</h4>"
    if kind == "pageBlockKicker":
        return f"<h5>{text}</h5>"
    if kind == "pageBlockFooter":
        return f"<footer>{text}</footer>"
    if kind == "pageBlockAuthorDate":
        author = _rich_text(block.get("author"))
        return f'<p><b>{author}</b> <tg-time unix="{block.get("published_date", 0)}" format="dMy"></tg-time></p>'
    if kind == "pageBlockPreformatted":
        lang = escape_attr(block.get("language") or "")
        cls = f' class="language-{lang}"' if lang else ""
        return f"<pre><code{cls}>{text}</code></pre>"
    if kind in {"pageBlockPhoto", "pageBlockVideo", "pageBlockAudio", "pageBlockDocument", "pageBlockMap", "pageBlockEmbed"}:
        return f"<figcaption>{_rich_caption(block.get('caption'))}</figcaption>"
    if kind in {"pageBlockCollage", "pageBlockSlideshow"}:
        inner = "".join(_rich_block(item) for item in cast('list[Any]', block.get("items") or []))
        return f"<figure>{inner}<figcaption>{_rich_caption(block.get('caption'))}</figcaption></figure>"
    if kind == "pageBlockDivider":
        return "<hr>"
    if kind == "pageBlockAnchor":
        return f'<a name="{escape_attr(block.get("name") or "")}"></a>'
    if kind == "pageBlockList":
        items: list[str] = []
        for item in cast('list[Any]', block.get("items") or []):
            if not isinstance(item, dict):
                continue
            item = cast('dict[str, Any]', item)
            if item.get("text") is not None:
                inner = _rich_text(item.get("text"))
            else:
                inner = "".join(_rich_block(sub) for sub in cast('list[Any]', item.get("blocks") or []))
            items.append(f"<li>{inner}</li>")
        return f"<ul>{''.join(items)}</ul>" if items else ""
    if kind == "pageBlockOrderedList":
        ordered_items: list[str] = []
        for item in cast('list[Any]', block.get("items") or []):
            if not isinstance(item, dict):
                continue
            item = cast('dict[str, Any]', item)
            if item.get("text") is not None:
                inner = _rich_text(item.get("text"))
            else:
                inner = "".join(_rich_block(sub) for sub in cast('list[Any]', item.get("blocks") or []))
            ordered_items.append(f"<li>{inner}</li>")
        return f"<ol>{''.join(ordered_items)}</ol>" if ordered_items else ""
    if kind == "pageBlockBlockquote":
        return f"<blockquote>{text}{_rich_caption(block.get('caption'))}</blockquote>"
    if kind == "pageBlockPullquote":
        return f"<aside>{text}<cite>{_rich_caption(block.get('caption'))}</cite></aside>"
    if kind == "pageBlockDetails":
        title = _rich_text(block.get("title")) if block.get("title") is not None else ""
        inner = "".join(_rich_block(sub) for sub in cast('list[Any]', block.get("blocks") or []))
        open_attr = " open" if block.get("open") else ""
        return f"<details{open_attr}><summary>{title}</summary>{inner}</details>"
    if kind == "pageBlockTable":
        rows: list[str] = []
        for row in cast('list[Any]', block.get("rows") or []):
            if not isinstance(row, dict):
                continue
            row = cast('dict[str, Any]', row)
            cells: list[str] = []
            for cell in cast('list[Any]', row.get("cells") or []):
                if not isinstance(cell, dict):
                    continue
                cell = cast('dict[str, Any]', cell)
                tag = "th" if cell.get("header") else "td"
                cells.append(f"<{tag}>{_rich_text(cell.get('text'))}</{tag}>")
            if cells:
                rows.append(f"<tr>{''.join(cells)}</tr>")
        return f"<table>{''.join(rows)}</table>" if rows else ""
    return text


def _ent_get(e: object, name: str, default: Any = None) -> Any:
    if isinstance(e, dict):
        return cast('dict[str, Any]', e).get(name, default)
    return getattr(e, name, default)


def _ent_tags(e: object, kind: str) -> tuple[str, str] | None:
    kl = kind.rsplit(".", 1)[-1].lower()
    if "bold" in kl:
        return "<b>", "</b>"
    if "italic" in kl:
        return "<i>", "</i>"
    if "underline" in kl:
        return "<u>", "</u>"
    if "strike" in kl:
        return "<s>", "</s>"
    if "pre" in kl:
        lang = _ent_get(e, "language") or ""
        cls = f' class="language-{_html_mod.escape(str(lang))}"' if lang else ""
        return f"<pre{cls}>", "</pre>"
    if kl.endswith("code"):
        return "<code>", "</code>"
    if "spoiler" in kl:
        return "<tg-spoiler>", "</tg-spoiler>"
    if "blockquote" in kl:
        return "<blockquote>", "</blockquote>"
    if "texturl" in kl or kl.endswith("url"):
        url = _html_mod.escape(str(_ent_get(e, "url") or ""), quote=True)
        return f'<a href="{url}">', "</a>"
    if "mentionname" in kl:
        uid = _ent_get(e, "user_id") or 0
        return f'<a href="tg://user?id={uid}">', "</a>"
    if "customemoji" in kl:
        did = int(_ent_get(e, "document_id") or _ent_get(e, "emoji_id") or 0)
        return f'<tg-emoji emoji-id={did}>', "</tg-emoji>"
    return None


def entities_to_html(text: str | None, entities: list[object] | tuple[object, ...] | None = None) -> str:
    text = "" if text is None else str(text)
    if not entities:
        return _html_mod.escape(text)
    u2p: list[int] = []
    for i, ch in enumerate(text):
        u2p.append(i)
        if ord(ch) > 0xFFFF:
            u2p.append(i)
    nu = len(u2p)
    u2p.append(len(text))

    def py(u: int) -> int:
        if u <= 0:
            return 0
        if u >= nu:
            return len(text)
        return u2p[u]

    items: list[tuple[int, int, str, str]] = []
    for e in entities:
        kind = str(_ent_get(e, "_") or "")
        off = _ent_get(e, "offset")
        ln = _ent_get(e, "length")
        if off is None or ln is None:
            continue
        tags = _ent_tags(e, kind)
        if not tags:
            continue
        a, b = py(int(off)), py(int(off) + int(ln))
        if b > a:
            items.append((a, b, tags[0], tags[1]))
    if not items:
        return _html_mod.escape(text)
    opens: dict[int, list[str]] = {}
    closes: dict[int, list[str]] = {}
    for a, b, o, c in sorted(items, key=lambda x: x[1] - x[0], reverse=True):
        opens.setdefault(a, []).append(o)
    for a, b, o, c in sorted(items, key=lambda x: x[1] - x[0]):
        closes.setdefault(b, []).append(c)
    out: list[str] = []
    for i in range(len(text) + 1):
        out.extend(closes.get(i, ()))
        out.extend(opens.get(i, ()))
        if i < len(text):
            out.append(_html_mod.escape(text[i]))
    return "".join(out)


class _MessageTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "blockquote", "aside", "summary", "details", "tr", "br", "hr", "footer", "figure", "figcaption"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"}:
            self.parts.append("\t")
        else:
            self.handle_starttag(tag, [])


def message_text(message: object) -> str:
    if isinstance(message, str):
        return message
    raw = _ent_get(message, "raw", message)
    rich = _ent_get(raw, "rich_message") or _ent_get(raw, "richMessage")
    if rich is None and _ent_get(raw, "_") in {"richMessage", "inputRichMessage", "inputRichMessageHTML"}:
        rich = raw
    if rich is not None:
        markup = _ent_get(rich, "html")
        if not isinstance(markup, str):
            markup = rich_to_html({"rich_message": rich})
        parser = _MessageTextParser()
        parser.feed(markup)
        parser.close()
        text = "\n".join(line.rstrip("\t ") for line in "".join(parser.parts).splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            return text
    for name in ("message", "raw_text", "text", "caption"):
        value = _ent_get(raw, name)
        if isinstance(value, str) and value:
            return value
    return ""


def rich_to_html(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    message = cast('dict[str, Any]', message)
    rich = message.get("rich_message") or message.get("richMessage")
    if not isinstance(rich, dict):
        return ""
    rich = cast('dict[str, Any]', rich)
    return "".join(_rich_block(block) for block in cast('list[Any]', rich.get("blocks") or []))


def btn_html(button: dict[str, Any]) -> str:
    text = escape(button.get("text") or "")
    attrs: list[str] = []
    if isinstance(button.get("url"), str):
        attrs.append(f'url="{escape_attr(button["url"])}"')
    elif isinstance(button.get("copy_text"), str):
        attrs.append(f'copy="{escape_attr(button["copy_text"])}"')
    elif isinstance(button.get("callback_data"), str):
        attrs.append(f'data="{escape_attr(button["callback_data"])}"')
    if button.get("style"):
        attrs.append(f'style="{escape_attr(button["style"])}"')
    if button.get("icon_custom_emoji_id"):
        attrs.append(f'icon="{escape_attr(button["icon_custom_emoji_id"])}"')
    extra = (" " + " ".join(attrs)) if attrs else ""
    return f"<tg-button{extra}>{text}</tg-button>"


def buttons_html(buttons: list[Any], kind: str = "page") -> str:
    if not buttons:
        return ""
    rows: list[list[Any]] = cast('list[list[Any]]', buttons) if isinstance(buttons[0], list) else [buttons]
    if kind == "text":
        parts: list[str] = []
        for row in rows:
            for btn in row:
                if isinstance(btn, dict):
                    parts.append(btn_html(cast('dict[str, Any]', btn)))
        return "".join(parts)
    page_parts: list[str] = []
    for row in rows:
        inner = "".join(btn_html(cast('dict[str, Any]', btn)) for btn in row if isinstance(btn, dict))
        if inner:
            page_parts.append(f"<tg-button-row>{inner}</tg-button-row>")
    return "".join(page_parts)


def needs_form(buttons: object) -> bool:
    values = cast('list[Any]', buttons) if isinstance(buttons, list) else []
    rows: list[Any] = values if values and isinstance(values[0], list) else [buttons or []]
    for row in rows:
        if not isinstance(row, list):
            continue
        for btn in cast('list[Any]', row):
            if isinstance(btn, dict) and isinstance(cast('dict[str, Any]', btn).get("input"), str):
                return True
    return False


def needs_callback(buttons: object) -> bool:
    values = cast('list[Any]', buttons) if isinstance(buttons, list) else []
    rows: list[Any] = values if values and isinstance(values[0], list) else [buttons or []]
    for row in rows:
        if not isinstance(row, list):
            continue
        for btn in cast('list[Any]', row):
            if not isinstance(btn, dict):
                continue
            btn = cast('dict[str, Any]', btn)
            if isinstance(btn.get("input"), str):
                return True
            if btn.get("callback_data") or btn.get("data") or btn.get("callback") or callable(btn.get("handler")):
                return True
    return False


def _search_normalize(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")))


def _search_distance(left: str, right: str, maximum: int) -> int:
    if abs(len(left) - len(right)) > maximum:
        return maximum + 1
    previous = list(range(len(right) + 1))
    older = previous
    for i, char in enumerate(left, 1):
        current = [i] + [maximum + 1] * len(right)
        for j in range(max(1, i - maximum), min(len(right), i + maximum) + 1):
            current[j] = min(current[j - 1] + 1, previous[j] + 1, previous[j - 1] + (char != right[j - 1]))
            if i > 1 and j > 1 and char == right[j - 2] and left[i - 2] == right[j - 1]:
                current[j] = min(current[j], older[j - 2] + 1)
        if min(current) > maximum:
            return maximum + 1
        older, previous = previous, current
    return previous[-1]


def _search_score(query: str, text: str) -> float:
    if query == text:
        return 1.0
    words = text.split()
    terms = sorted(set(query.split()), key=lambda term: (-len(term), term))
    if not words or len(terms) > len(words):
        return 0.0
    scores: list[float] = []
    for term in terms:
        best, index = 0.0, -1
        for i, word in enumerate(words):
            score = 0.0
            if term == word:
                score = 0.96
            elif len(term) >= 2 and word.startswith(term):
                score = 0.9 + 0.03 * len(term) / len(word)
            elif len(term) >= 3 and len(word) >= 4:
                maximum = 1 if min(len(term), len(word)) < 8 else 2
                distance = _search_distance(term, word, maximum)
                if distance <= maximum:
                    score = 0.86 - 0.05 * distance
            if score > best:
                best, index = score, i
        if index < 0:
            return 0.0
        scores.append(best)
        words.pop(index)
    return sum(scores) / len(scores)


async def search(values: Any, query: object, *, key: Any = None, limit: object = 10, min_score: float = 0.76, layout: bool = True) -> list[dict[str, Any]]:
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    if not 0 <= min_score <= 1:
        raise ValueError("min_score must be between 0 and 1")
    if isinstance(values, (str, bytes)):
        raise TypeError("values must be a collection, not a string")
    original = _search_normalize(query)
    if not original or not limit:
        return []
    variants = [original]
    if layout:
        en = "qwertyuiop[]asdfghjkl;'zxcvbnm,./`"
        ru = "йцукенгшщзхъфывапролджэячсмитьбю.ё"
        for source, target in ((en, ru), (ru, en)):
            changed = _search_normalize(query.casefold().translate(str.maketrans(source, target)))
            if changed and changed not in variants:
                variants.append(changed)
    heap: list[Any] = []
    checkpoint = time.perf_counter()
    for index, value in enumerate(values):
        if time.perf_counter() - checkpoint >= 0.004:
            await asyncio.sleep(0)
            checkpoint = time.perf_counter()
        raw = key(value) if callable(key) else value[key] if isinstance(key, str) else value
        fields = cast('list[object] | tuple[object, ...]', raw) if isinstance(raw, (list, tuple)) else (raw,)
        best, matched, swapped = 0.0, "", False
        for field in fields:
            if not isinstance(field, str):
                raise TypeError("search fields must be strings")
            normalized = _search_normalize(field)
            for variant in variants:
                score = _search_score(variant, normalized)
                if variant != original:
                    score *= 0.98
                if score > best:
                    best, matched, swapped = score, field, variant != original
        best = round(best, 6)
        if best > 0 and best >= min_score:
            hit = {"value": value, "score": round(best, 4), "matched": matched, "layout": swapped}
            entry = (best, -index, hit)
            if len(heap) < limit:
                heapq.heappush(heap, entry)
            elif entry[:2] > heap[0][:2]:
                heapq.heapreplace(heap, entry)
    return [entry[2] for entry in sorted(heap, reverse=True)]


_DATEREG_POINTS = (
    (217509, 1381494240), (2768400, 1383242400), (6256671, 1389230743), (11538510, 1391104800),
    (11538514, 1391212800), (15835240, 1392832800), (23646070, 1393351200), (38015510, 1393632000),
    (44634660, 1396720800), (44634663, 1398751134), (46145305, 1400112000), (54845230, 1403287200),
    (54845238, 1409623100), (63263518, 1414368000), (72776425, 1426969600), (103151531, 1433002389),
    (109393468, 1434293556), (112594714, 1438356836), (124872440, 1439803800), (125828524, 1442621544),
    (133909600, 1444158000), (139646974, 1444273680), (143445120, 1448910000), (148670295, 1450442743),
    (171295414, 1455763620), (181783990, 1459048280), (222021230, 1465326000), (225034350, 1465741200),
    (267103082, 1472677200), (278941742, 1473304260), (281973858, 1475301600), (285253070, 1476783000),
    (294851030, 1479547800), (297621220, 1481828400), (310042805, 1486624520), (337808429, 1487635200),
    (339965214, 1488462748), (369669040, 1490814000), (369669043, 1490918400), (399597034, 1496257200),
    (400169470, 1501441200), (409518943, 1504845000), (484976017, 1516084200), (501535268, 1518638400),
    (554990133, 1521187200), (573604341, 1523822400), (601341900, 1530727714), (655730151, 1537472400),
    (660511546, 1542181050), (707751101, 1542304800), (717887953, 1544569200), (774723749, 1546096720),
    (805158066, 1560225600), (871177218, 1561951200), (920778234, 1566046582), (928636984, 1574600483),
    (1020178246, 1576957200), (1040151903, 1577817000), (1047436723, 1579132800), (1054883348, 1581355336),
    (1130489378, 1583002800), (1145856008, 1586342040), (1170070589, 1588284000), (1227964864, 1592650800),
    (1334421204, 1597360114), (1355237859, 1597536000), (1382531194, 1600188120), (1383895297, 1601492400),
    (1384783988, 1602806400), (1415571066, 1604817900), (1432107681, 1607419800), (1485680298, 1608757200),
    (1514538817, 1609441200), (1530835010, 1613424269), (1692464211, 1615402500), (1719536397, 1618527500),
    (1788462219, 1622458800), (1807942741, 1623394481), (1938921569, 1626933294), (1972424006, 1631669400),
    (1974255900, 1632133790), (2078711279, 1632453028), (5017595749, 1638309600), (5022636255, 1638921600),
    (5035057026, 1640352847), (5045293264, 1642032000), (5047148663, 1644214800), (5106451106, 1645050600),
    (5124771193, 1645488000), (5144324763, 1646006400), (5153900870, 1647474418), (5244529493, 1648070198),
    (5297354132, 1648760400), (5304951856, 1651840954), (5340744210, 1655108894), (5402540458, 1656615600),
    (5409444610, 1657520976), (5442755368, 1658534400), (5451256696, 1659359794), (5513192189, 1659626400),
    (5515826405, 1659785880), (5537251684, 1660014200), (5558980075, 1661410165), (5601951167, 1663487260),
    (5627539474, 1664251183), (5731896020, 1666171461), (5738347976, 1667458776), (5795660441, 1667692800),
    (5802242180, 1671821040), (5802659303, 1674050189), (5869978651, 1675348920), (5891297818, 1677003626),
    (5982648124, 1679972788), (5994561143, 1681955599), (6058560984, 1683960986), (6279839148, 1688257360),
    (6306077724, 1689872560), (6364973680, 1693449233), (6401027363, 1695238926), (6451891234, 1696378491),
    (6536173556, 1702430229), (6651640269, 1704425047), (6872061796, 1706360705), (7000000000, 1711889200),
    (7002435197, 1712119200), (7078066115, 1717503429), (7191651807, 1720927350), (7243375923, 1721260800),
    (7255466649, 1723727100), (7342300216, 1724385927), (7450316621, 1726743600), (7490725809, 1729065600),
    (7509628470, 1731225000), (7537361651, 1733837200), (7708562823, 1735944525), (7817256746, 1738419840),
    (7834356221, 1741233600), (7899152800, 1743520875), (8096580061, 1745598708), (8179125032, 1752019200),
    (8200159552, 1757362950), (8379482677, 1759605750), (8384648263, 1760136960), (8480708838, 1762300800),
    (8541347308, 1763028000), (8587904527, 1764543600), (8608772687, 1781568000), (8619075604, 1782533560),
    (8662290370, 1784985152), (8695922065, 1785314402), (8706747990, 1789516800), (8708390615, 1790349961),
    (8938423393, 1790737713), (8975598483, 1792108800),
)


_DATEREG_ERRORS = (
    (217509, 27), (1000000, 29), (2768400, 30), (2768409, 30),
    (6256671, 30), (7679610, 30), (11538510, 35), (11538514, 60),
    (15835240, 60), (15835244, 66), (23646070, 60), (23646077, 60),
    (38015510, 64), (44634660, 60), (44634663, 60), (45641754, 60),
    (46145305, 64), (54845230, 64), (54845238, 76), (60457419, 76),
    (63263518, 73), (72776425, 73), (95308423, 73), (101260938, 73),
    (101323197, 73), (103151531, 73), (103204956, 73), (103258382, 73),
    (109393468, 76), (110306836, 73), (111220210, 64), (112594714, 46),
    (115831565, 42), (116812045, 41), (122600695, 38), (124872440, 38),
    (124872445, 41), (125828524, 42), (127929226, 42), (130029930, 42),
    (133909600, 42), (133909606, 41), (139646974, 41), (143445120, 39),
    (143445125, 39), (148670295, 39), (152079341, 39), (155983984, 39),
    (157242073, 39), (171295414, 39), (172144707, 39), (181783990, 39),
    (188459175, 39), (222021230, 39), (222021233, 39), (225034350, 39),
    (225034354, 39), (234480941, 39), (267103082, 39), (278941742, 39),
    (279333717, 37), (281973858, 35), (285253070, 30), (285253072, 29),
    (294851030, 22), (294851037, 20), (297621220, 22), (297621225, 24),
    (310042805, 32), (328122828, 39), (328594461, 39), (337808429, 39),
    (339965214, 39), (341546272, 41), (352940995, 44), (369669040, 44),
    (369669043, 45), (399597034, 49), (400169470, 49), (400169472, 51),
    (409518943, 54), (441343998, 54), (484976017, 54), (501535268, 54),
    (554990133, 57), (573604341, 66), (601341900, 84), (616816630, 92),
    (655730151, 92), (660511546, 91), (675879664, 93), (681896077, 100),
    (695531082, 95), (707751101, 100), (717887953, 100), (727572658, 100),
    (774723749, 100), (796147074, 100), (805158066, 100), (820050353, 100),
    (871177218, 100), (920778234, 100), (925078064, 96), (928636984, 96),
    (970269006, 95), (979195258, 95), (998725855, 95), (1020178246, 95),
    (1040151903, 92), (1047436723, 92), (1054883348, 71), (1057704545, 66),
    (1059845356, 65), (1081554131, 65), (1130489378, 51), (1145856008, 38),
    (1170070589, 32), (1202891775, 32), (1227964864, 30), (1232810985, 30),
    (1279382482, 30), (1334421204, 32), (1355237859, 33), (1382531194, 32),
    (1383895297, 32), (1384783988, 30), (1415571066, 30), (1432107681, 30),
    (1439824385, 29), (1468778795, 30), (1471194210, 28), (1485680298, 25),
    (1488888443, 25), (1514538817, 25), (1530835010, 25), (1578805533, 27),
    (1649902679, 28), (1658586909, 32), (1660971491, 33), (1692464211, 35),
    (1719536397, 36), (1721844091, 36), (1735271901, 41), (1772991138, 36),
    (1787945512, 41), (1788462219, 43), (1807942741, 48), (1824157837, 48),
    (1881430862, 50), (1893429550, 58), (1938921569, 59), (1972424006, 63),
    (1974255900, 65), (1991158335, 65), (2030606431, 65), (2041327411, 65),
    (2078711279, 66), (2104178931, 65), (2107129437, 70), (2120496865, 69),
    (2123596685, 69), (2138472342, 71), (2250645697, 71), (3318845111, 71),
    (4317845111, 71), (5017595749, 69), (5022636255, 69), (5031711230, 69),
    (5035057026, 75), (5045293264, 71), (5047148663, 66), (5065900297, 66),
    (5070164216, 63), (5106451106, 61), (5124099423, 58), (5124771193, 56),
    (5144324763, 56), (5149590651, 55), (5153900870, 55), (5155903109, 55),
    (5159326926, 55), (5162494923, 55), (5166844465, 55), (5169485538, 54),
    (5170390109, 54), (5177789190, 54), (5179102906, 54), (5186883095, 54),
    (5196353812, 54), (5199399786, 54), (5207110227, 54), (5210565134, 52),
    (5244529493, 52), (5249255238, 52), (5259159476, 49), (5260388619, 49),
    (5268253519, 54), (5271530336, 55), (5288930461, 47), (5290311052, 49),
    (5297354132, 66), (5304951856, 66), (5308260177, 66), (5317829834, 66),
    (5318092331, 75), (5318972588, 75), (5336336790, 75), (5340744210, 77),
    (5349830748, 77), (5351497367, 77), (5362593868, 78), (5363536419, 77),
    (5387234031, 78), (5394432429, 78), (5396515972, 74), (5396575599, 73),
    (5396587273, 89), (5402540458, 89), (5409444610, 89), (5416026704, 80),
    (5428357996, 74), (5433708969, 74), (5434011049, 74), (5442755368, 74),
    (5451256696, 73), (5465223076, 70), (5468433192, 70), (5468950164, 70),
    (5472518401, 70), (5480654757, 70), (5488407539, 70), (5499934702, 70),
    (5502182459, 70), (5505809357, 83), (5513192189, 86), (5515826405, 86),
    (5519218712, 86), (5522237606, 86), (5537251684, 86), (5542245357, 89),
    (5546930145, 89), (5558980075, 83), (5559167331, 83), (5559594088, 83),
    (5562600534, 83), (5567880858, 83), (5568348673, 83), (5584365163, 83),
    (5591759222, 83), (5595045952, 83), (5596032583, 83), (5598262640, 83),
    (5601951167, 83), (5608562550, 83), (5614111200, 73), (5627539474, 83),
    (5644144970, 77), (5666819340, 77), (5681900282, 71), (5684254605, 71),
    (5684689868, 71), (5694365966, 71), (5705427359, 59), (5707112959, 52),
    (5721138769, 52), (5731896020, 52), (5735455201, 52), (5738347976, 52),
    (5744374534, 46), (5756095415, 46), (5765259845, 47), (5772670706, 52),
    (5778063231, 54), (5795660441, 66), (5802242180, 66), (5802659303, 66),
    (5804028268, 69), (5806925457, 73), (5815100469, 80), (5821105646, 83),
    (5839137822, 73), (5844821188, 86), (5853442730, 86), (5854845236, 90),
    (5856660622, 88), (5859861622, 91), (5859878513, 91), (5862080962, 91),
    (5869978651, 90), (5885964106, 91), (5891297818, 91), (5903848700, 91),
    (5904140174, 90), (5931294587, 90), (5964221956, 91), (5982648124, 91),
    (5983654523, 90), (5983753471, 90), (5994561143, 89), (6000582627, 91),
    (6001287799, 91), (6020888206, 91), (6025903944, 90), (6032606998, 90),
    (6057123350, 89), (6058560984, 89), (6074830852, 85), (6101607245, 85),
    (6108011341, 83), (6108395402, 83), (6115110768, 81), (6132325730, 81),
    (6135597783, 81), (6175817126, 81), (6180394472, 66), (6182056052, 66),
    (6188508923, 81), (6254094947, 67), (6271031786, 72), (6277658932, 78),
    (6279839148, 78), (6297860552, 81), (6306077724, 81), (6321562426, 81),
    (6326011828, 78), (6364973680, 78), (6373693377, 78), (6386727079, 78),
    (6401027363, 78), (6429580803, 81), (6433264186, 81), (6451891234, 82),
    (6507968649, 78), (6513268158, 87), (6514802524, 87), (6520005489, 91),
    (6523424924, 96), (6527226055, 95), (6536173556, 96), (6545049031, 96),
    (6559717847, 101), (6606876583, 96), (6609923725, 102), (6637805363, 96),
    (6639644540, 95), (6651640269, 95), (6670760749, 95), (6684986493, 95),
    (6703731755, 116), (6715889959, 116), (6720229740, 116), (6732829831, 116),
    (6749492866, 116), (6765129195, 116), (6813121418, 116), (6827058708, 116),
    (6829119388, 126), (6845043276, 126), (6854829938, 102), (6865576492, 102),
    (6872061796, 81), (6903333095, 78), (6925870357, 78), (6926984452, 69),
    (6947316117, 69), (7000000000, 69), (7002435197, 67), (7043862629, 67),
    (7078066115, 67), (7085776398, 67), (7100000000, 78), (7103700000, 78),
    (7104310277, 78), (7107400000, 69), (7129689557, 69), (7191651807, 67),
    (7224009547, 60), (7229898489, 59), (7242296450, 59), (7243375923, 60),
    (7254607307, 60), (7255466649, 57), (7273085448, 59), (7280136256, 59),
    (7293965553, 69), (7342300216, 72), (7347120414, 74), (7357703634, 72),
    (7363299295, 72), (7366200000, 72), (7398600000, 72), (7409259451, 80),
    (7423300000, 79), (7424559477, 80), (7433746267, 91), (7444300000, 82),
    (7450316621, 83), (7458668365, 83), (7469400000, 82), (7469700000, 82),
    (7490725809, 82), (7509628470, 85), (7519637470, 85), (7533900000, 88),
    (7537361651, 92), (7552093513, 98), (7591351660, 96), (7600158321, 102),
    (7625100000, 103), (7654300000, 109), (7657400000, 123), (7664959631, 123),
    (7683400000, 124), (7708562823, 138), (7716779743, 142), (7747102337, 138),
    (7793034911, 142), (7817256746, 145), (7825518194, 145), (7829910989, 147),
    (7831448272, 147), (7832006200, 147), (7834356221, 147), (7851389063, 158),
    (7852083588, 158), (7857659678, 158), (7870888707, 158), (7884373548, 158),
    (7899152800, 158), (7912577935, 157), (7915901421, 147), (7939700000, 170),
    (7958700000, 164), (7964511972, 164), (7967849270, 147), (8017192943, 143),
    (8044853035, 143), (8060910775, 138), (8085100000, 125), (8089817806, 124),
    (8096580061, 122), (8096742229, 122), (8102800000, 117), (8117852491, 117),
    (8135088730, 108), (8173852075, 102), (8174700000, 102), (8178600000, 102),
    (8179125032, 94), (8200159552, 94), (8209194945, 83), (8218310808, 83),
    (8238766847, 77), (8325327280, 77), (8343319353, 73), (8343786378, 73),
    (8369442459, 71), (8379482677, 70), (8384648263, 64), (8393200797, 64),
    (8454563873, 65), (8461412540, 65), (8461579295, 65), (8480708838, 65),
    (8541347308, 65), (8559682245, 65), (8587904527, 65), (8608772687, 65),
    (8619075604, 65), (8631703617, 65), (8636864387, 65), (8660097409, 65),
    (8662290370, 65), (8681634807, 63), (8695922065, 85), (8706747990, 64),
    (8708390615, 64), (8709330704, 64), (8713587395, 64), (8736786596, 62),
    (8838020594, 58), (8865450032, 58), (8867545858, 59), (8867646692, 59),
    (8890167640, 56), (8892165325, 51), (8893764521, 42), (8938423393, 39),
    (8938841362, 42), (8966578432, 42), (8975598483, 39),
)


def _datereg_interpolate(points: tuple[tuple[int, int], ...], uid: int) -> float:
    if uid <= points[0][0]:
        return float(points[0][1])
    if uid >= points[-1][0]:
        return float(points[-1][1])
    lo, hi = 0, len(points) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if points[mid][0] <= uid:
            lo = mid
        else:
            hi = mid
    (x0, y0), (x1, y1) = points[lo], points[hi]
    return y0 + (y1 - y0) * (uid - x0) / (x1 - x0)


# https://github.com/sepiol026-wq/telegram-id-age-research
def datereg(user_id: int | str) -> dict[str, str | int | bool]:
    if isinstance(user_id, str):
        user_id = user_id.strip()
        if not user_id.isascii() or not user_id.isdecimal():
            raise ValueError("datereg requires a numeric Telegram user ID")
        user_id = int(user_id)
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise TypeError("datereg requires a numeric Telegram user ID")
    if not _DATEREG_POINTS[0][0] <= user_id <= _DATEREG_POINTS[-1][0]:
        raise ValueError("user ID is outside the datereg calibration range")
    stamp = _datereg_interpolate(_DATEREG_POINTS, user_id)
    error = int(_datereg_interpolate(_DATEREG_ERRORS, user_id))
    now = datetime.now(timezone.utc)
    date = datetime.fromtimestamp(min(stamp, now.timestamp()), timezone.utc).date()
    return {
        "date": date.isoformat(),
        "earliest": datetime.fromtimestamp(min(stamp - error * 86400, now.timestamp()), timezone.utc).date().isoformat(),
        "latest": datetime.fromtimestamp(min(stamp + error * 86400, now.timestamp()), timezone.utc).date().isoformat(),
        "error_days": error,
        "age_days": (now.date() - date).days,
        "estimated": True,
    }


TOOLKIT_FUNCS = {
    "datereg": datereg,
    "template_fields": template_fields,
    "template_names": template_names,
    "template_render": template_render,
    "search": search,
    "args_parse": args_parse,
    "args_raw": args_raw,
    "args_split": args_split,
    "args_int": args_int,
    "args_bool": args_bool,
    "args_float": args_float,
    "escape": escape,
    "escape_attr": escape_attr,
    "escape_smart": escape_smart,
    "strip_tags": strip_tags,
    "strip_emoji": strip_emoji,
    "flag": flag,
    "entity_url": entity_url,
    "entity_link": entity_link,
    "valid_url": valid_url,
    "is_url": is_url,
    "urls_extract": urls_extract,
    "chunk": chunk,
    "random_str": random_str,
    "file_size": file_size,
    "iso_time": iso_time,
    "merge_dicts": merge_dicts,
    "json_ok": json_ok,
    "flatten": flatten,
    "censor": censor,
    "attr": attr,
    "face": face,
    "html_fix": html_fix,
    "chat_id": chat_id,
    "entity_id": entity_id,
    "topic_id": topic_id,
    "mime": mime,
    "msg_link": msg_link,
    "has_media": has_media,
    "target_id": target_id,
    "render": render,
    "uptime": uptime,
    "uptime_fmt": uptime_fmt,
    "duration": duration,
    "timestamp": timestamp,
    "time_range": time_range,
    "truncate": truncate,
    "table": table,
    "progress": progress,
    "code_block": code_block,
    "spoiler": spoiler,
    "blockquote": blockquote,
    "mention": mention,
    "link": link,
    "list_items": list_items,
    "kv": kv,
    "tree": tree,
    "plural": plural,
    "percent": percent,
    "clamp": clamp,
    "mask": mask,
    "hash_short": hash_short,
    "badge": badge,
    "countdown": countdown,
    "rich_to_html": rich_to_html,
    "message_text": message_text,
    "entities_to_html": entities_to_html,
    "btn_html": btn_html,
    "buttons_html": buttons_html,
    "needs_form": needs_form,
    "needs_callback": needs_callback,
}

TOOLS = SimpleNamespace(**TOOLKIT_FUNCS)
