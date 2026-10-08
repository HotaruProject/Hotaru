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


TOOLKIT_FUNCS = {
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
