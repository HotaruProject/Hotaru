from __future__ import annotations

import hashlib
import html as _html_mod
import json
import random
import re
import shlex
import string
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

_BOOT_TS = time.perf_counter()

_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9\-]*)(?:\s[^<>]*)?>")
_TG_TAGS = frozenset({"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote", "tg-spoiler", "tg-emoji", "br"})
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
            new = []
            for s in sections:
                new.extend(s.split(sep))
            sections = new
    return [s.strip() for s in sections if s.strip()]


def args_int(text: str) -> list[int]:
    result = []
    for arg in args_parse(text):
        try:
            result.append(int(arg))
        except ValueError:
            pass
    return result


def args_bool(text: str) -> list[bool]:
    result = []
    for arg in args_parse(text):
        low = arg.lower()
        if low in ("true", "yes", "1", "on", "y", "да", "вкл"):
            result.append(True)
        elif low in ("false", "no", "0", "off", "n", "нет", "выкл"):
            result.append(False)
    return result


def args_float(text: str) -> list[float]:
    result = []
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
    out = []
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


def entity_url(entity: dict | Any, openmessage: bool = False) -> str:
    if isinstance(entity, dict):
        eid = entity.get("user_id") or entity.get("id") or entity.get("channel_id")
        username = entity.get("username")
        kind = entity.get("_", "")
    else:
        eid = getattr(entity, "id", None)
        username = getattr(entity, "username", None)
        kind = type(entity).__name__
    if "user" in str(kind).lower() or (isinstance(entity, dict) and "user" in str(entity.get("_", "")).lower()):
        return f"tg://openmessage?id={eid}" if openmessage else f"tg://user?id={eid}"
    if username:
        return f"tg://resolve?domain={username}"
    return ""


def entity_link(entity: dict | Any, label: str | None = None) -> str:
    url = entity_url(entity)
    if not url:
        return escape(label or "")
    name = label or str(getattr(entity, "username", None) or getattr(entity, "id", ""))
    if isinstance(entity, dict):
        name = label or str(entity.get("username") or entity.get("id") or entity.get("first_name") or "")
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


def chunk(items: list | tuple | str, size: int) -> list:
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


def merge_dicts(a: dict, b: dict, *, deep: bool = True) -> dict:
    for key, a_val in a.items():
        b_val = b.get(key)
        if key not in b:
            b[key] = a_val
        elif deep and isinstance(a_val, dict) and isinstance(b_val, dict):
            b[key] = merge_dicts(a_val, b_val, deep=True)
        elif isinstance(a_val, list) and isinstance(b_val, list):
            b[key] = list(dict.fromkeys(b_val + a_val))
        else:
            b[key] = a_val
    return b


def json_ok(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except Exception:
        return False


def flatten(nested: list) -> list:
    result = []
    for item in nested:
        if isinstance(item, list):
            result.extend(item)
        else:
            result.append(item)
    return result


def censor(obj: Any, fields: list[str] | None = None, replacement: str = "***") -> Any:
    if fields is None:
        fields = ["phone", "password", "token", "secret", "api_hash", "api_id"]
    if isinstance(obj, dict):
        return {k: (replacement if k.lower() in [f.lower() for f in fields] and isinstance(v, str) else censor(v, fields, replacement)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [censor(item, fields, replacement) for item in obj]
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
        return entity.get("channel_id") or entity.get("chat_id") or entity.get("user_id") or entity.get("id")
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
            return media.get("mime_type", "")
    m = getattr(message, "media", None)
    if isinstance(m, dict):
        return m.get("mime_type", "")
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
        return isinstance(media, dict) and media is not None
    return getattr(message, "media", None) is not None


def target_id(message: Any, arg_index: int = 0) -> int | None:
    entities = getattr(message, "entities", None)
    if entities:
        for ent in entities:
            kind = getattr(ent, "_", "") or (ent.get("_", "") if isinstance(ent, dict) else "")
            if "mentionname" in str(kind).lower():
                uid = getattr(ent, "user_id", None) or (ent.get("user_id") if isinstance(ent, dict) else None)
                if uid:
                    return uid
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


def uptime() -> int:
    return round(time.perf_counter() - _BOOT_TS)


def uptime_fmt() -> str:
    total = uptime()
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def duration(seconds: int | float) -> str:
    if seconds < 0:
        return "0s"
    units = [(31536000, "y"), (2592000, "mo"), (86400, "d"), (3600, "h"), (60, "m"), (1, "s")]
    parts = []
    remaining = int(seconds)
    for unit_secs, label in units:
        if remaining >= unit_secs:
            count = remaining // unit_secs
            remaining %= unit_secs
            parts.append(f"{count}{label}")
    return " ".join(parts) if parts else "0s"


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


def table(rows: list[list | tuple | dict], headers: list[str] | None = None) -> str:
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
    lines = []
    for key, value in pairs:
        k = f"<b>{escape(key)}</b>" if bold_keys else escape(key)
        lines.append(f"{k}: <code>{escape(value)}</code>")
    return "<br>".join(lines)


def tree(items: list[str], indent: str = "  ") -> str:
    if not items:
        return ""
    lines = []
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


TOOLKIT_FUNCS = {
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
}

TOOLS = SimpleNamespace(**TOOLKIT_FUNCS)
