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


_REGDATE_POINTS: tuple[tuple[int, int], ...] = (
    (25, 1383873634), (3941874, 1388448000), (6256671, 1389230743), (11538510, 1391104800),
    (11538514, 1391212800), (15835240, 1392832800), (23646070, 1393351200), (38015510, 1393632000),
    (44634660, 1396720800), (44634663, 1398751134), (46145305, 1400112000), (54845230, 1403287200),
    (54845238, 1409623100), (63263518, 1414368000), (72776425, 1426969600), (103151531, 1433002389),
    (109393468, 1434293556), (112594714, 1438356836), (124872440, 1439803800), (125828524, 1442621544),
    (133909600, 1444158000), (139646974, 1444273680), (143445120, 1448910000), (148670295, 1450442743),
    (171295414, 1455763620), (181783990, 1459048280), (222021230, 1465326000), (225034350, 1466190000),
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
    (5802242180, 1671821040), (5802659303, 1674050189), (5869015726, 1674259200), (5869978651, 1675348920),
    (5891297818, 1677003626), (5982648124, 1679972788), (5994561143, 1681955599), (6058560984, 1683960986),
    (6279839148, 1688257360), (6306077724, 1689872560), (6364973680, 1693449233), (6401027363, 1695238926),
    (6451891234, 1696378491), (6536173556, 1702430229), (6651640269, 1704425047), (6872061796, 1706360705),
    (7000000000, 1711889200), (7002435197, 1712119200), (7078066115, 1717503429), (7191651807, 1720927350),
    (7243375923, 1721260800), (7255466649, 1723727100), (7342300216, 1724385927), (7450316621, 1726743600),
    (7490725809, 1729065600), (7509628470, 1731225000), (7537361651, 1733837200), (7708562823, 1735944525),
    (7817256746, 1738419840), (7834356221, 1741233600), (7899152800, 1743520875), (8096580061, 1745598708),
    (8179125032, 1752019200), (8200159552, 1757362950), (8379482677, 1759605750), (8384648263, 1760136960),
    (8480708838, 1762300800), (8541347308, 1763028000), (8587904527, 1764543600), (8608772687, 1781568000),
    (8619075604, 1782533560), (8662290370, 1784985152), (8695922065, 1785314402), (8706747990, 1785406152),
    (8890167640, 1788264844), (8938841362, 1790276790), (8975598483, 1792108800),
)


_REGDATE_ERRORS: tuple[tuple[int, int], ...] = (
    (25, 91), (217509, 91), (1000000, 91), (2768400, 91),
    (2768409, 91), (3941874, 91), (6256671, 91), (7679610, 91),
    (11538510, 91), (11538514, 91), (15835240, 91), (15835244, 91),
    (23646070, 91), (23646077, 91), (38015510, 91), (44634660, 91),
    (44634663, 91), (45641754, 91), (46145305, 91), (54845230, 91),
    (54845238, 91), (60457419, 91), (63263518, 91), (72776425, 91),
    (95308423, 91), (101260938, 91), (101323197, 91), (103151531, 91),
    (103204956, 91), (103258382, 91), (109393468, 91), (110306836, 91),
    (111220210, 91), (112594714, 91), (115831565, 91), (116812045, 91),
    (122600695, 91), (124872440, 91), (124872445, 91), (125828524, 91),
    (127929226, 91), (130029930, 91), (133909600, 91), (133909606, 91),
    (139646974, 91), (143445120, 91), (143445125, 91), (148670295, 91),
    (152079341, 91), (155983984, 91), (157242073, 91), (171295414, 91),
    (172144707, 91), (181783990, 91), (188459175, 91), (222021230, 91),
    (222021233, 91), (225034350, 91), (225034354, 91), (234480941, 91),
    (267103082, 91), (278941742, 91), (279333717, 91), (281973858, 91),
    (285253070, 91), (285253072, 91), (294851030, 91), (294851037, 91),
    (297621220, 91), (297621225, 91), (310042805, 91), (328122828, 91),
    (328594461, 91), (337808429, 91), (339965214, 91), (341546272, 91),
    (352940995, 91), (369669040, 91), (369669043, 91), (399597034, 91),
    (400169470, 91), (400169472, 91), (409518943, 91), (441343998, 91),
    (484976017, 91), (501535268, 91), (554990133, 91), (573604341, 91),
    (601341900, 91), (616816630, 92), (655730151, 92), (660511546, 91),
    (675879664, 93), (681896077, 100), (695531082, 95), (707751101, 100),
    (717887953, 100), (727572658, 100), (774723749, 100), (796147074, 100),
    (805158066, 100), (820050353, 100), (871177218, 100), (920778234, 100),
    (925078064, 96), (928636984, 96), (970269006, 95), (979195258, 95),
    (998725855, 95), (1020178246, 95), (1040151903, 92), (1047436723, 92),
    (1054883348, 91), (1057704545, 91), (1059845356, 91), (1081554131, 91),
    (1130489378, 91), (1145856008, 91), (1170070589, 91), (1202891775, 91),
    (1227964864, 91), (1232810985, 91), (1279382482, 91), (1334421204, 91),
    (1355237859, 91), (1382531194, 91), (1383895297, 91), (1384783988, 91),
    (1415571066, 91), (1432107681, 91), (1439824385, 91), (1468778795, 91),
    (1471194210, 91), (1485680298, 91), (1488888443, 91), (1514538817, 91),
    (1530835010, 91), (1578805533, 91), (1649902679, 91), (1658586909, 91),
    (1660971491, 91), (1692464211, 91), (1719536397, 91), (1721844091, 91),
    (1735271901, 91), (1772991138, 91), (1787945512, 91), (1788462219, 91),
    (1807942741, 91), (1824157837, 91), (1881430862, 91), (1893429550, 91),
    (1938921569, 91), (1972424006, 91), (1974255900, 91), (1991158335, 91),
    (2030606431, 91), (2041327411, 91), (2078711279, 91), (2104178931, 91),
    (2107129437, 91), (2120496865, 91), (2123596685, 91), (2138472342, 91),
    (2250645697, 91), (3318845111, 91), (4317845111, 91), (5017595749, 91),
    (5022636255, 91), (5031711230, 91), (5035057026, 91), (5045293264, 91),
    (5047148663, 91), (5065900297, 91), (5070164216, 91), (5106451106, 91),
    (5124099423, 91), (5124771193, 91), (5144324763, 91), (5149590651, 91),
    (5153900870, 91), (5155903109, 91), (5159326926, 91), (5162494923, 91),
    (5166844465, 91), (5169485538, 91), (5170390109, 91), (5177789190, 91),
    (5179102906, 91), (5186883095, 91), (5196353812, 91), (5199399786, 91),
    (5207110227, 91), (5210565134, 91), (5244529493, 91), (5249255238, 91),
    (5259159476, 91), (5260388619, 91), (5268253519, 91), (5271530336, 91),
    (5288930461, 91), (5290311052, 91), (5297354132, 91), (5304951856, 91),
    (5308260177, 91), (5317829834, 91), (5318092331, 91), (5318972588, 91),
    (5336336790, 91), (5340744210, 91), (5349830748, 91), (5351497367, 91),
    (5362593868, 91), (5363536419, 91), (5387234031, 91), (5394432429, 91),
    (5396515972, 91), (5396575599, 91), (5396587273, 91), (5402540458, 91),
    (5409444610, 91), (5416026704, 91), (5428357996, 91), (5433708969, 91),
    (5434011049, 91), (5442755368, 91), (5451256696, 91), (5465223076, 91),
    (5468433192, 91), (5468950164, 91), (5472518401, 91), (5480654757, 91),
    (5488407539, 91), (5499934702, 91), (5502182459, 91), (5505809357, 91),
    (5513192189, 91), (5515826405, 91), (5519218712, 91), (5522237606, 91),
    (5537251684, 91), (5542245357, 91), (5546930145, 91), (5558980075, 91),
    (5559167331, 91), (5559594088, 91), (5562600534, 91), (5567880858, 91),
    (5568348673, 91), (5584365163, 91), (5591759222, 91), (5595045952, 91),
    (5596032583, 91), (5598262640, 91), (5601951167, 91), (5608562550, 91),
    (5614111200, 91), (5627539474, 91), (5644144970, 91), (5666819340, 91),
    (5681900282, 91), (5684254605, 91), (5684689868, 91), (5694365966, 91),
    (5705427359, 91), (5707112959, 91), (5721138769, 91), (5731896020, 91),
    (5735455201, 91), (5738347976, 91), (5744374534, 91), (5756095415, 91),
    (5765259845, 91), (5772670706, 91), (5778063231, 91), (5795660441, 91),
    (5802242180, 91), (5802659303, 91), (5804028268, 91), (5806925457, 91),
    (5815100469, 91), (5821105646, 91), (5839137822, 91), (5844821188, 91),
    (5853442730, 91), (5854845236, 91), (5856660622, 91), (5859861622, 91),
    (5859878513, 91), (5862080962, 91), (5869015726, 91), (5869978651, 91),
    (5885964106, 91), (5891297818, 91), (5903848700, 91), (5904140174, 91),
    (5931294587, 91), (5964221956, 91), (5982648124, 91), (5983654523, 91),
    (5983753471, 91), (5994561143, 91), (6000582627, 91), (6001287799, 91),
    (6020888206, 91), (6025903944, 91), (6032606998, 91), (6057123350, 91),
    (6058560984, 91), (6074830852, 91), (6101607245, 91), (6108011341, 91),
    (6108395402, 91), (6115110768, 91), (6132325730, 91), (6135597783, 91),
    (6175817126, 91), (6180394472, 91), (6182056052, 91), (6188508923, 91),
    (6254094947, 91), (6271031786, 91), (6277658932, 91), (6279839148, 91),
    (6297860552, 91), (6306077724, 91), (6321562426, 91), (6326011828, 91),
    (6364973680, 91), (6373693377, 91), (6386727079, 91), (6401027363, 91),
    (6429580803, 91), (6433264186, 91), (6451891234, 91), (6507968649, 91),
    (6513268158, 91), (6514802524, 91), (6520005489, 91), (6523424924, 96),
    (6527226055, 95), (6536173556, 96), (6545049031, 96), (6559717847, 101),
    (6606876583, 96), (6609923725, 102), (6637805363, 96), (6639644540, 95),
    (6651640269, 95), (6670760749, 95), (6684986493, 95), (6703731755, 116),
    (6715889959, 116), (6720229740, 116), (6732829831, 116), (6749492866, 116),
    (6765129195, 116), (6813121418, 116), (6827058708, 116), (6829119388, 126),
    (6845043276, 126), (6854829938, 102), (6865576492, 102), (6872061796, 91),
    (6903333095, 91), (6925870357, 91), (6926984452, 91), (6947316117, 91),
    (7000000000, 91), (7002435197, 91), (7043862629, 91), (7078066115, 91),
    (7085776398, 91), (7100000000, 91), (7103700000, 91), (7104310277, 91),
    (7107400000, 91), (7129689557, 91), (7191651807, 91), (7224009547, 91),
    (7229898489, 91), (7242296450, 91), (7243375923, 91), (7254607307, 91),
    (7255466649, 91), (7273085448, 91), (7280136256, 91), (7293965553, 91),
    (7342300216, 91), (7347120414, 91), (7357703634, 91), (7363299295, 91),
    (7366200000, 91), (7398600000, 91), (7409259451, 91), (7423300000, 91),
    (7424559477, 91), (7433746267, 91), (7444300000, 91), (7450316621, 91),
    (7458668365, 91), (7469400000, 91), (7469700000, 91), (7490725809, 91),
    (7509628470, 91), (7519637470, 91), (7533900000, 91), (7537361651, 92),
    (7552093513, 98), (7591351660, 96), (7600158321, 102), (7625100000, 103),
    (7654300000, 109), (7657400000, 123), (7664959631, 123), (7683400000, 124),
    (7708562823, 138), (7716779743, 142), (7747102337, 138), (7793034911, 142),
    (7817256746, 145), (7825518194, 145), (7829910989, 147), (7831448272, 147),
    (7832006200, 147), (7834356221, 147), (7851389063, 158), (7852083588, 158),
    (7857659678, 158), (7870888707, 158), (7884373548, 158), (7899152800, 158),
    (7912577935, 157), (7915901421, 147), (7939700000, 170), (7958700000, 164),
    (7964511972, 164), (7967849270, 147), (8017192943, 143), (8044853035, 143),
    (8060910775, 138), (8085100000, 125), (8089817806, 124), (8096580061, 122),
    (8096742229, 122), (8102800000, 117), (8117852491, 117), (8135088730, 108),
    (8173852075, 102), (8174700000, 102), (8178600000, 102), (8179125032, 94),
    (8200159552, 94), (8209194945, 91), (8218310808, 91), (8238766847, 91),
    (8325327280, 91), (8343319353, 91), (8343786378, 91), (8369442459, 91),
    (8379482677, 91), (8384648263, 91), (8393200797, 91), (8454563873, 91),
    (8461412540, 91), (8461579295, 91), (8480708838, 91), (8541347308, 91),
    (8559682245, 91), (8587904527, 91), (8608772687, 91), (8619075604, 91),
    (8631703617, 91), (8636864387, 91), (8660097409, 91), (8662290370, 91),
    (8681634807, 91), (8695922065, 91), (8706747990, 91), (8708390615, 91),
    (8709330704, 91), (8713587395, 91), (8736786596, 91), (8838020594, 91),
    (8865450032, 91), (8867545858, 91), (8867646692, 91), (8874594977, 91),
    (8890167640, 91), (8892165325, 91), (8893764521, 91), (8938423393, 91),
    (8938841362, 91), (8966578432, 91), (8975598483, 91),
)


def _regdate_interpolate(points: tuple[tuple[int, int], ...], uid: int) -> float:
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


_REGDATE_ANCHORS: dict[int, int] = {25: 1384473600, 217509: 1382662080, 1000000: 1380326400, 2768400: 1383242400, 2768409: 1383264000, 3941874: 1388448000, 6256671: 1390013486, 7679610: 1388448000, 11538510: 1391104800, 11538514: 1391212800, 15835240: 1392832800, 15835244: 1392854400, 23646070: 1393351200, 23646077: 1393372800, 38015510: 1393632000, 44634660: 1396720800, 44634663: 1399334400, 45641754: 1398167867, 46145305: 1400112000, 54845230: 1403287200, 54845238: 1411171200, 60457419: 1408075000, 63263518: 1414368000, 72776425: 1427835600, 95308423: 1428238000, 101260938: 1425600000, 101323197: 1426204800, 103151531: 1433289600, 103204956: 1433032767, 103258382: 1432684800, 109393468: 1438992000, 110306836: 1434314267, 111220210: 1429574400, 112594714: 1439596800, 115831565: 1438524943, 116812045: 1437609600, 122600695: 1437696000, 124872440: 1439838000, 124872445: 1439769600, 125828524: 1444003200, 127929226: 1442623033, 130029930: 1441238400, 133909600: 1444158000, 133909606: 1444176000, 139646974: 1444273680, 143445120: 1448910000, 143445125: 1448928000, 148670295: 1452211200, 152079341: 1453420800, 155983984: 1449370973, 157242073: 1446768000, 171295414: 1457481600, 172144707: 1454045640, 181783990: 1460246400, 188459175: 1457850160, 222021230: 1465326000, 222021233: 1465344000, 225034350: 1466190000, 225034354: 1466208000, 267103082: 1472677200, 278941742: 1473465600, 279333717: 1473142920, 281973858: 1475301600, 285253070: 1476817200, 285253072: 1476748800, 294851030: 1479582000, 294851037: 1479513600, 297621220: 1481828400, 297621225: 1481846400, 310042805: 1487106000, 328122828: 1487205960, 328594461: 1485561600, 337808429: 1487635200, 339965214: 1489772244, 341546272: 1487721600, 352940995: 1487894400, 369669040: 1490814000, 369669043: 1490918400, 399597034: 1496257200, 400169470: 1501441200, 400169472: 1501459200, 409518943: 1506848400, 441343998: 1502841600, 484976017: 1516084200, 501535268: 1518638400, 554990133: 1521187200, 573604341: 1523822400, 601341900: 1531829829, 616816630: 1529625600, 655730151: 1537472400, 660511546: 1555372800, 675879664: 1539510300, 681896077: 1532821500, 695531082: 1541019600, 707751101: 1542304800, 717887953: 1545429600, 727572658: 1543708800, 774723749: 1550821640, 796147074: 1541371800, 805158066: 1563148800, 820050353: 1557302400, 871177218: 1561951200, 920778234: 1568803164, 925078064: 1563290000, 928636984: 1581513420, 970269006: 1574693313, 979195258: 1568332800, 998725855: 1573862400, 1020178246: 1576957200, 1040151903: 1577817000, 1047436723: 1579132800, 1054883348: 1585674420, 1057704545: 1580393640, 1059845356: 1578848886, 1081554131: 1580504400, 1130489378: 1583002800, 1145856008: 1586342040, 1170070589: 1588284000, 1202891775: 1588310640, 1227964864: 1596127860, 1232810985: 1590890220, 1279382482: 1590934320, 1334421204: 1597360114, 1355237859: 1597536000, 1382531194: 1600188120, 1383895297: 1601492400, 1384783988: 1602806400, 1415571066: 1604817900, 1432107681: 1608076800, 1439824385: 1606762800, 1468778795: 1608076800, 1471194210: 1606813200, 1485680298: 1609437600, 1488888443: 1608076800, 1514538817: 1609441200, 1530835010: 1615852800, 1578805533: 1612119600, 1649902679: 1612670965, 1658586909: 1613148540, 1660971491: 1613329440, 1692464211: 1615402500, 1719536397: 1619293500, 1721844091: 1620224820, 1735271901: 1618786994, 1772991138: 1617540360, 1787945512: 1616791827, 1788462219: 1622458800, 1807942741: 1625520300, 1824157837: 1622576984, 1881430862: 1623440640, 1893429550: 1622040000, 1938921569: 1626933294, 1972424006: 1631669400, 1974255900: 1633996800, 1991158335: 1630556060, 2030606431: 1631992680, 2041327411: 1631989620, 2078711279: 1634321820, 2104178931: 1638353220, 2107129437: 1637020800, 2120496865: 1636714020, 2123596685: 1636503180, 2138472342: 1637590800, 2250645697: 1633515812, 3318845111: 1618028800, 4317845111: 1620028800, 5017595749: 1638309600, 5022636255: 1639094400, 5031711230: 1638748800, 5035057026: 1640352847, 5045293264: 1642032000, 5047148663: 1649116800, 5065900297: 1640977200, 5070164216: 1642550400, 5106451106: 1646438400, 5124099423: 1643662800, 5124771193: 1645488000, 5144324763: 1649203200, 5149590651: 1642809600, 5153900870: 1647907200, 5155903109: 1647907200, 5159326926: 1655337600, 5162494923: 1652449800, 5166844465: 1652054400, 5169485538: 1646092800, 5170390109: 1646006400, 5177789190: 1642982400, 5179102906: 1647043200, 5186883095: 1648764360, 5196353812: 1644883200, 5199399786: 1645002097, 5207110227: 1643846400, 5210565134: 1644364800, 5244529493: 1650326400, 5249255238: 1646383782, 5259159476: 1651795200, 5260388619: 1646179200, 5268253519: 1647907200, 5271530336: 1651276800, 5288930461: 1643241600, 5290311052: 1647451401, 5297354132: 1648760400, 5304951856: 1656718440, 5308260177: 1650844800, 5317829834: 1653152820, 5318092331: 1652024220, 5318972588: 1651937341, 5336336790: 1646368100, 5340744210: 1666742400, 5349830748: 1651190400, 5351497367: 1653350400, 5362593868: 1652024520, 5363536419: 1659312000, 5387234031: 1662137700, 5394432429: 1653264000, 5396515972: 1650499200, 5396575599: 1654553525, 5396587273: 1648014800, 5402540458: 1656615600, 5409444610: 1659025020, 5416026704: 1660925460, 5428357996: 1657497600, 5433708969: 1653696000, 5434011049: 1656460800, 5442755368: 1658534400, 5451256696: 1659744000, 5465223076: 1661710860, 5468433192: 1658275200, 5468950164: 1657756800, 5472518401: 1667347200, 5480654757: 1660926300, 5488407539: 1653782400, 5499934702: 1662130740, 5502182459: 1658314837, 5505809357: 1653609600, 5513192189: 1659626400, 5515826405: 1664755200, 5519218712: 1660435200, 5522237606: 1654167240, 5537251684: 1664269800, 5542245357: 1657152000, 5546930145: 1658620800, 5558980075: 1667088000, 5559167331: 1656718560, 5559594088: 1668038400, 5562600534: 1659301200, 5567880858: 1674604800, 5568348673: 1654642200, 5584365163: 1659366750, 5591759222: 1659025500, 5595045952: 1666483200, 5596032583: 1655337600, 5598262640: 1654905600, 5601951167: 1664668800, 5608562550: 1664012820, 5614111200: 1661780160, 5627539474: 1669939200, 5644144970: 1664703130, 5666819340: 1664112240, 5681900282: 1665964800, 5684254605: 1662134040, 5684689868: 1661304720, 5694365966: 1661644800, 5705427359: 1665014400, 5707112959: 1663803300, 5721138769: 1663891200, 5731896020: 1667242122, 5735455201: 1665100800, 5738347976: 1682380800, 5744374534: 1665360000, 5756095415: 1660925940, 5765259845: 1667088000, 5772670706: 1661539140, 5778063231: 1667477640, 5795660441: 1667692800, 5802242180: 1671821040, 5802659303: 1679356800, 5804028268: 1677974400, 5806925457: 1673481600, 5815100469: 1672272000, 5821105646: 1671677863, 5839137822: 1671148800, 5844821188: 1672506000, 5853442730: 1674866100, 5854845236: 1682553600, 5856660622: 1673991930, 5859861622: 1668816000, 5859878513: 1673117760, 5862080962: 1670889600, 5869015726: 1674259200, 5869978651: 1679616000, 5885964106: 1671081840, 5891297818: 1683158400, 5903848700: 1675280531, 5904140174: 1684540800, 5931294587: 1668816000, 5964221956: 1673222400, 5982648124: 1686941700, 5983654523: 1681223063, 5983753471: 1671753600, 5994561143: 1684195200, 6000582627: 1685404800, 6001287799: 1683676800, 6020888206: 1675534800, 6025903944: 1681680600, 6032606998: 1686998640, 6057123350: 1676198350, 6058560984: 1686907980, 6074830852: 1682899200, 6101607245: 1686830760, 6108011341: 1681032060, 6108395402: 1684368000, 6115110768: 1683830609, 6132325730: 1692033840, 6135597783: 1684886400, 6175817126: 1683417600, 6180394472: 1685318400, 6182056052: 1687870740, 6188508923: 1687392000, 6254094947: 1677456000, 6271031786: 1676160000, 6277658932: 1679011200, 6279839148: 1688399160, 6297860552: 1688115560, 6306077724: 1692442920, 6321562426: 1688486760, 6326011828: 1688688000, 6364973680: 1696349340, 6373693377: 1692301478, 6386727079: 1691696880, 6401027363: 1700870400, 6429580803: 1692082680, 6433264186: 1692763697, 6451891234: 1701475200, 6507968649: 1695301080, 6513268158: 1701475200, 6514802524: 1700092800, 6520005489: 1695081600, 6523424924: 1690934400, 6527226055: 1690289160, 6536173556: 1704931200, 6545049031: 1702944000, 6559717847: 1708819200, 6606876583: 1710633600, 6609923725: 1696100400, 6637805363: 1694822400, 6639644540: 1698760800, 6651640269: 1715990400, 6670760749: 1705190400, 6684986493: 1695600000, 6703731755: 1714867200, 6715889959: 1707091200, 6720229740: 1710633600, 6732829831: 1707609600, 6749492866: 1700611200, 6765129195: 1698883200, 6813121418: 1698489600, 6827058708: 1699228800, 6829119388: 1701475200, 6845043276: 1704907301, 6854829938: 1706745600, 6865576492: 1699052400, 6872061796: 1716595200, 6903333095: 1706659200, 6925870357: 1701192327, 6926984452: 1704758400, 6947316117: 1702598400, 7000000000: 1711889200, 7002435197: 1712361600, 7043862629: 1711876800, 7078066115: 1725753600, 7085776398: 1715299200, 7100000000: 1720224000, 7103700000: 1713866400, 7104310277: 1713484800, 7107400000: 1718272800, 7129689557: 1715623200, 7191651807: 1721136600, 7224009547: 1722556800, 7229898489: 1723075200, 7242296450: 1716940800, 7243375923: 1724544000, 7254607307: 1717977600, 7255466649: 1724214000, 7273085448: 1732147200, 7280136256: 1720051200, 7293965553: 1718496000, 7342300216: 1736985600, 7347120414: 1724655600, 7357703634: 1725926400, 7363299295: 1721865600, 7366200000: 1723024800, 7398600000: 1723024800, 7409259451: 1718841600, 7423300000: 1723716000, 7424559477: 1727733600, 7433746267: 1718496000, 7444300000: 1723975200, 7450316621: 1733097600, 7458668365: 1722556800, 7469400000: 1725098400, 7469700000: 1726221600, 7490725809: 1729065600, 7509628470: 1732991400, 7519637470: 1734202800, 7533900000: 1726480800, 7537361651: 1735686000, 7552093513: 1738224000, 7591351660: 1742515200, 7600158321: 1733356800, 7625100000: 1727690400, 7654300000: 1728208800, 7657400000: 1731841200, 7664959631: 1734566400, 7683400000: 1732446000, 7708562823: 1746835200, 7716779743: 1739042100, 7747102337: 1730851200, 7793034911: 1727049600, 7817256746: 1750204800, 7825518194: 1736985600, 7829910989: 1746921600, 7831448272: 1731283200, 7832006200: 1726704000, 7834356221: 1756684800, 7851389063: 1733097600, 7852083588: 1748822400, 7857659678: 1727222400, 7870888707: 1749340800, 7884373548: 1732233600, 7899152800: 1744070400, 7912577935: 1757894400, 7915901421: 1751846400, 7939700000: 1732791600, 7958700000: 1735642800, 7964511972: 1742947200, 7967849270: 1743318900, 8017192943: 1759622400, 8044853035: 1742428800, 8060910775: 1736294400, 8085100000: 1738494000, 8089817806: 1736899200, 8096580061: 1745982867, 8096742229: 1756598400, 8102800000: 1739012400, 8117852491: 1760400000, 8135088730: 1741132800, 8173852075: 1740096000, 8174700000: 1740567600, 8178600000: 1740999600, 8179125032: 1752019200, 8200159552: 1757635200, 8209194945: 1761436800, 8218310808: 1758394800, 8238766847: 1753920000, 8325327280: 1760313600, 8343319353: 1757980800, 8343786378: 1754611200, 8369442459: 1754611200, 8379482677: 1759605750, 8384648263: 1761177600, 8393200797: 1761350400, 8454563873: 1764979200, 8461412540: 1755628800, 8461579295: 1757548800, 8480708838: 1762300800, 8541347308: 1763236800, 8559682245: 1762819200, 8587904527: 1764543600, 8608772687: 1781568000, 8619075604: 1785163223, 8631703617: 1783607260, 8636864387: 1781568000, 8660097409: 1779795758, 8662290370: 1791570660, 8681634807: 1778399645, 8695922065: 1785314402, 8706747990: 1789516800, 8713587395: 1791595249, 8736786596: 1791576199, 8865450032: 1791616340, 8867646692: 1786838400, 8874594977: 1781654400, 8890167640: 1791565889, 8892165325: 1791660642, 8893764521: 1781568000, 8966578432: 1788444779}

def regdate(user_id: object, *, registration_month: object = None, not_after: object = None) -> dict[str, str | int | bool]:
    import datetime
    import math

    if isinstance(user_id, str):
        user_id = user_id.strip()
        if not user_id.isascii() or not user_id.isdecimal():
            raise ValueError("regdate requires a numeric Telegram user ID")
        user_id = int(user_id)
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise TypeError("regdate requires a numeric Telegram user ID")
    if not 0 < user_id < 2**63:
        raise ValueError("regdate requires a positive signed 64-bit Telegram user ID")
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date()
    launch = datetime.date(2013, 8, 14)
    monthlow = monthhigh = None
    if registration_month is not None:
        if not isinstance(registration_month, str) or len(registration_month) != 7 or registration_month[2] != '.' or not (registration_month[:2] + registration_month[3:]).isascii() or not (registration_month[:2] + registration_month[3:]).isdecimal():
            raise ValueError("registration_month must be MM.YYYY")
        month, year = int(registration_month[:2]), int(registration_month[3:])
        monthlow = datetime.date(year, month, 1)
        if monthlow > today:
            raise ValueError("registration_month cannot be in the future")
        nextmonth = datetime.date(year + (month == 12), month % 12 + 1, 1)
        monthhigh = min(nextmonth - datetime.timedelta(days=1), today)
        if monthhigh < launch:
            raise ValueError("registration_month predates Telegram launch")
        monthlow = max(monthlow, launch)
    cap = None
    if not_after is not None:
        if not isinstance(not_after, str):
            raise TypeError("not_after must be YYYY-MM-DD")
        cap = datetime.date.fromisoformat(not_after)
        if cap.isoformat() != not_after or cap > today:
            raise ValueError("not_after must be a non-future YYYY-MM-DD")
    supported = _REGDATE_POINTS[0][0] <= user_id <= _REGDATE_POINTS[-1][0]
    if not supported and monthlow is None:
        raise ValueError("user ID is outside the regdate calibration range")
    conflict = False
    source = "anchor" if user_id in _REGDATE_ANCHORS else "model"
    if supported:
        stamp = max(1376438400, min(_REGDATE_ANCHORS.get(user_id, _regdate_interpolate(_REGDATE_POINTS, user_id)), now.timestamp()))
        error = math.ceil(_regdate_interpolate(_REGDATE_ERRORS, user_id))
        date = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).date()
        low = max(launch, datetime.datetime.fromtimestamp(stamp - error * 86400, datetime.timezone.utc).date())
        high = datetime.datetime.fromtimestamp(min(stamp + error * 86400, now.timestamp()), datetime.timezone.utc).date()
    else:
        assert monthlow is not None and monthhigh is not None
        low, high = monthlow, monthhigh
        date = low + (high - low) // 2
    if monthlow is not None and monthhigh is not None:
        conflict = high < monthlow or low > monthhigh
        low, high = monthlow, monthhigh
        date = date if low <= date <= high else low + (high - low) // 2
        source = "registration_month"
    if cap is not None:
        if cap < low:
            conflict = True
            source = "conflict"
        else:
            high = min(high, cap)
            date = min(date, high)
            source += "+upper_bound"
    return {
        "date": date.isoformat(),
        "earliest": low.isoformat(),
        "latest": high.isoformat(),
        "error_days": max((date - low).days, (high - date).days),
        "age_days": (today - date).days,
        "estimated": True,
        "source": source,
        "conflict": conflict,
        "registration_month": registration_month or "",
        "not_after": not_after or "",
    }


TOOLKIT_FUNCS = {
    "regdate": regdate,
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
