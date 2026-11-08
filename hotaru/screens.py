from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import inspect
import json
import logging
import re
import secrets
import struct
import tempfile
import time
import traceback
import urllib.parse
import http.client
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, cast

from goygram.errors import FloodWaitError, MessageIdInvalidError, MessageNotModifiedError
from goygram.sugar import extract_sent_message
from relay.emoji import has_emoji, to_entities, to_rich
from relay.firewall import module_scope, trusted_scope
from relay.rpc import delete_chat_msg

from .callback_codec import open_callback, seal_callback
from .markup import copy_limit, row_limit, kbd_to_tl

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger(__name__)

_prefix = "~"
_version = 1
_pack = struct.Struct("<BIHB")
_tag = 8
_toast = 200
_kernel = "@kernel"
_cache = 256
_forms = 4096
_styles = {"primary", "danger", "success", "link"}
_tap = re.compile(r'<tg-button(\s[^>]*?)\sdata-hs="([A-Za-z0-9_-]+)"([^>]*)>')
_rich = re.compile(r"<(?:table|tg-button|tg-button-row|details|h[1-6]|hr|footer|ul|ol|aside|tg-time|tg-math|mark|sup|sub|img|video)\b", re.IGNORECASE)

PAGE_CACHE: dict[str, dict[str, Any]] = {}
_PAGE_CACHE = PAGE_CACHE

_assets = Path(__file__).resolve().parent / "assets"
_banner_cache: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
_banner_lock = asyncio.Lock()


def _resolve_asset(name: str) -> tuple[str, str, Path] | None:
    raw = str(name).strip()
    for ext in (".mp4", ".png", ".jpg", ".jpeg", ".webp"):
        if raw.lower().endswith(ext):
            raw = raw[:-len(ext)]
            break
    clean = re.sub(r"[^a-zA-Z0-9_-]", "", raw).strip().lower()
    if not clean:
        clean = "brand"
    media_id = f"hotaru-{clean}"
    if clean in ("brand", "banner", "logo", "config"):
        filename = "banner.png" if clean != "logo" else "logo.png"
        path = _assets / filename
        return (media_id, "photo", path) if path.exists() else None
    for ext, kind in ((".mp4", "video"), (".png", "photo"), (".jpg", "photo"), (".webp", "photo")):
        path = _assets / f"{clean}{ext}"
        if path.exists():
            return (media_id, kind, path)
    if (_assets / "banner.png").exists():
        return (media_id, "photo", _assets / "banner.png")
    return None


async def _upload_asset(app: Any, media_id: str, kind: str, file_path: Path) -> dict[str, Any]:
    from relay.files import document, put
    up = await put(app, str(file_path), file_name=file_path.name)
    upload = document(up, mime="video/mp4" if kind == "video" else "image/png", force_file=False)
    if kind == "photo":
        media = {"_": "inputMediaUploadedPhoto", "file": upload["file"]}
    else:
        media = upload
        media["attributes"].extend([
            {"_": "documentAttributeAnimated"},
            {"_": "documentAttributeVideo", "duration": 4.0, "w": 1920, "h": 1080, "supports_streaming": True},
        ])
    with trusted_scope():
        raw_res = await app.mt_messages_upload_media(peer={"_": "inputPeerSelf"}, media=media)
    res_dict: dict[str, Any]
    if isinstance(raw_res, dict):
        res_dict = cast('dict[str, Any]', raw_res)
    elif hasattr(raw_res, "to_dict"):
        to_dict_fn = cast(Callable[[], dict[str, Any]], getattr(raw_res, "to_dict"))
        res_dict = to_dict_fn()
    else:
        res_dict = dict(cast('dict[str, Any]', getattr(raw_res, "__dict__", {})))
    if "result" in res_dict:
        inner_raw = res_dict["result"]
        if isinstance(inner_raw, dict):
            res_dict = cast('dict[str, Any]', inner_raw)
        elif hasattr(inner_raw, "to_dict"):
            to_dict_fn = cast(Callable[[], dict[str, Any]], getattr(inner_raw, "to_dict"))
            res_dict = to_dict_fn()
        else:
            res_dict = dict(cast('dict[str, Any]', getattr(inner_raw, "__dict__", {})))
    if kind == "photo":
        photo_raw = res_dict.get("photo")
        photo_dict: dict[str, Any]
        if isinstance(photo_raw, dict):
            photo_dict = cast('dict[str, Any]', photo_raw)
        elif hasattr(photo_raw, "to_dict"):
            to_dict_fn = cast(Callable[[], dict[str, Any]], getattr(photo_raw, "to_dict"))
            photo_dict = to_dict_fn()
        else:
            photo_dict = dict(cast('dict[str, Any]', getattr(photo_raw, "__dict__", {})))
        return {
            "_": "inputRichFilePhoto",
            "id": media_id,
            "photo": {
                "_": "inputPhoto",
                "id": int(photo_dict.get("id", 0)),
                "access_hash": int(photo_dict.get("access_hash", 0)),
                "file_reference": bytes(photo_dict.get("file_reference", b"")),
            },
        }
    doc_raw = res_dict.get("document")
    doc_dict: dict[str, Any]
    if isinstance(doc_raw, dict):
        doc_dict = cast('dict[str, Any]', doc_raw)
    elif hasattr(doc_raw, "to_dict"):
        to_dict_fn = cast(Callable[[], dict[str, Any]], getattr(doc_raw, "to_dict"))
        doc_dict = to_dict_fn()
    else:
        doc_dict = dict(cast('dict[str, Any]', getattr(doc_raw, "__dict__", {})))
    return {
        "_": "inputRichFileDocument",
        "id": media_id,
        "document": {
            "_": "inputDocument",
            "id": int(doc_dict.get("id", 0)),
            "access_hash": int(doc_dict.get("access_hash", 0)),
            "file_reference": bytes(doc_dict.get("file_reference", b"")),
        },
    }

_ICONS: dict[str, str] = {
    "back": '<tg-emoji emoji-id="5258236805890710909">⬅️</tg-emoji>',
    "close": '<tg-emoji emoji-id="5260342697075416641">❌</tg-emoji>',
    "search": '<tg-emoji emoji-id="5429571366384842791">🔎</tg-emoji>',
    "settings": '<tg-emoji emoji-id="5258096772776991776">⚙️</tg-emoji>',
    "edit": '<tg-emoji emoji-id="5258331647358540449">✍️</tg-emoji>',
    "copy": '<tg-emoji emoji-id="5258477770735885832">📄</tg-emoji>',
    "trash": '<tg-emoji emoji-id="5258130763148172425">🗑</tg-emoji>',
    "undo": '<tg-emoji emoji-id="5260687119092817530">🔄</tg-emoji>',
    "redo": '<tg-emoji emoji-id="5258420634785947640">🔄</tg-emoji>',
    "add": '<tg-emoji emoji-id="5258108352008823107">➕</tg-emoji>',
    "check": '<tg-emoji emoji-id="5260726538302660868">✅</tg-emoji>',
    "done": '<tg-emoji emoji-id="5260416304224936047">✅</tg-emoji>',
    "more": '<tg-emoji emoji-id="5260348422266822411">💬</tg-emoji>',
    "open": '<tg-emoji emoji-id="5257991477358763590">↗️</tg-emoji>',
    "link": '<tg-emoji emoji-id="5260730055880876557">⛓</tg-emoji>',
    "save": '<tg-emoji emoji-id="5258336354642697821">⬇️</tg-emoji>',
    "download": '<tg-emoji emoji-id="5258514780469075716">📂</tg-emoji>',
    "upload": '<tg-emoji emoji-id="5260652420052032852">⬆️</tg-emoji>',
    "share": '<tg-emoji emoji-id="5258043150110301407">⬆️</tg-emoji>',
    "login": '<tg-emoji emoji-id="5258215850745275216">➡️</tg-emoji>',
    "exit": '<tg-emoji emoji-id="5258084656674250503">🚪</tg-emoji>',
    "ban": '<tg-emoji emoji-id="5258318620722733379">❌</tg-emoji>',
    "lock": '<tg-emoji emoji-id="5258476306152038031">🔒</tg-emoji>',
    "eye": '<tg-emoji emoji-id="5253959125838090076">👁</tg-emoji>',
    "loading": '<tg-emoji emoji-id="5258012149036365477">📸</tg-emoji>',
    "minus": '<tg-emoji emoji-id="5275969776668134187">⛔️</tg-emoji>',
    "bot": '<tg-emoji emoji-id="5258093637450866522">🤖</tg-emoji>',
    "book": '<tg-emoji emoji-id="5258328383183396223">📖</tg-emoji>',
    "bolt": '<tg-emoji emoji-id="5258152182150077732">⚡️</tg-emoji>',
    "users": '<tg-emoji emoji-id="5258513401784573443">👥</tg-emoji>',
    "user": '<tg-emoji emoji-id="5258011929993026890">👤</tg-emoji>',
    "user_add": '<tg-emoji emoji-id="5258362837411045098">👤</tg-emoji>',
    "account": '<tg-emoji emoji-id="5260399854500191689">👤</tg-emoji>',
    "phone": '<tg-emoji emoji-id="5258337316715373336">🤙</tg-emoji>',
    "camera": '<tg-emoji emoji-id="5258205968025525531">📸</tg-emoji>',
    "file": '<tg-emoji emoji-id="5257965810634202885">📁</tg-emoji>',
    "folder": '<tg-emoji emoji-id="5257969839313526622">📁</tg-emoji>',
    "apps": '<tg-emoji emoji-id="5226513232549664618">🔢</tg-emoji>',
    "info": '<tg-emoji emoji-id="5258503720928288433">ℹ️</tg-emoji>',
    "warning": '<tg-emoji emoji-id="5258474669769497337">❗️</tg-emoji>',
    "clock": '<tg-emoji emoji-id="5258419835922030550">🕔</tg-emoji>',
    "timer": '<tg-emoji emoji-id="5258258882022612173">⏲</tg-emoji>',
    "stats": '<tg-emoji emoji-id="5258330865674494479">🍑</tg-emoji>',
    "chart": '<tg-emoji emoji-id="5258391025281408576">📈</tg-emoji>',
    "translate": '<tg-emoji emoji-id="5260512129240276089">📚</tg-emoji>',
    "tag": '<tg-emoji emoji-id="5296385246579670377">🏷</tg-emoji>',
    "pin": '<tg-emoji emoji-id="5258461531464539536">📌</tg-emoji>',
    "star": '<tg-emoji emoji-id="5258185631355378853">⭐️</tg-emoji>',
    "star_fill": '<tg-emoji emoji-id="5258165702707125574">⭐️</tg-emoji>',
    "premium": '<tg-emoji emoji-id="5280962371207077415">💎</tg-emoji>',
    "idea": '<tg-emoji emoji-id="5258216851472654189">💡</tg-emoji>',
    "home": '<tg-emoji emoji-id="5257963315258204021">🏘</tg-emoji>',
    "send": '<tg-emoji emoji-id="5258073068852485953">✈️</tg-emoji>',
    "checkbox": '<tg-emoji emoji-id="5258453452631056344">❌</tg-emoji>',
    "text": '<tg-emoji emoji-id="5370546867786523009">📝</tg-emoji>',
    "kernel": '<tg-emoji emoji-id="4974681956907221809">▪️</tg-emoji>',
    "module": '<tg-emoji emoji-id="4974508259839836856">▪️</tg-emoji>',
    "terminal": '<tg-emoji emoji-id="5289601856836745198">💻</tg-emoji>',
}

_MODULE_ICONS: dict[str, str] = {
    "help": "book",
    "config": "settings",
    "accounts": "users",
    "acl": "lock",
    "executor": "text",
    "updater": "download",
    "translations": "translate",
    "logger": "file",
    "info": "info",
    "ping": "bolt",
    "power": "exit",
    "settings": "settings",
    "backups": "folder",
    "catalyst": "download",
    "singularity": "bolt",
    "shield": "lock",
}


class ScreenError(RuntimeError):
    pass


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _style(style: Optional[str]) -> str:
    if not style:
        return ""
    if style not in _styles:
        raise ScreenError(f"unknown button style: {style}")
    return f' style="{style}"'


def _decorate(item: dict[str, Any], style: Optional[str], icon: Optional[str]) -> dict[str, Any]:
    if style:
        item["style"] = style
    if icon:
        item["icon"] = icon
    return item


def _clip(text: Optional[str], limit: int = _toast) -> Optional[str]:
    if text is None:
        return None
    from hotaru.plainfmt import to_plain_text
    plain = to_plain_text(str(text))
    return plain if len(plain) <= limit else plain[: limit - 1] + "…"


def _inline_key(value: Any) -> Any:
    if isinstance(value, dict):
        data = cast('dict[str, Any]', value)
        return tuple(data.get(key) for key in ("dc_id", "id", "owner_id", "access_hash"))
    return value


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _module_of(fn: Callable[..., Any]) -> str:
    name = str(getattr(fn, "__globals__", {}).get("__name__", ""))
    if name.startswith("hotaru_module_"):
        return name[len("hotaru_module_"):]
    raise ScreenError(f"screen handler must be a module-level function: {getattr(fn, '__qualname__', fn)!r}")


@dataclass
class Screen:
    text: str
    rows: list[list[dict[str, Any]]] = field(default_factory=lambda: [])
    toast: Optional[str] = None
    alert: bool = False
    rich: bool = False
    notice: Optional[str] = None
    banner: str = ""

    def with_notice(self, text: str) -> "Screen":
        self.notice = text
        return self


@dataclass
class Form:
    id: int
    module: str
    actor: int
    command: Optional[str]
    chat: Any
    message_id: Optional[int]
    inline_id: Optional[dict[str, Any]]
    bot: bool
    gen: int
    actions: list[dict[str, Any]]
    text: str
    rich: bool
    touched: float
    ttl: float
    language: Optional[str] = None


class Nav:
    def __init__(self, engine: "ScreenEngine", form: Form, action: dict[str, Any], *, call: Any = None, value: Optional[str] = None, actor: Optional[int] = None) -> None:
        self.engine = engine
        self.form = form
        self.call = call
        self.value = value
        self.actor = actor if actor is not None else form.actor
        self.args: dict[str, Any] = dict(action.get("a") or {})
        self._answered = False

    def arg(self, name: str, default: Any = None) -> Any:
        return self.args.get(name, default)

    def int(self, name: str, default: int = 0) -> int:
        try:
            return int(self.args.get(name, default))
        except (TypeError, ValueError):
            return default

    @property
    def is_input(self) -> bool:
        return self.value is not None

    async def answer(self, text: Optional[str] = None, *, alert: bool = False) -> None:
        if self._answered or self.call is None:
            return
        self._answered = True
        try:
            await self.call.answer(_clip(text), alert=alert)
        except Exception as exc:
            log.error("screen answer failed: %s: %s", type(exc).__name__, exc)

    async def toast(self, text: str) -> None:
        await self.answer(text)

    def ref(self, *, follow: bool = False) -> "ScreenRef":
        return ScreenRef(self.engine, self.form.id, None if follow else (self.form.gen + 1) & 0xFFFF)


class ScreenRef:
    def __init__(self, engine: "ScreenEngine", form_id: int, gen: Optional[int]) -> None:
        self.engine = engine
        self.form_id = form_id
        self.gen = gen

    async def edit(self, screen: Screen) -> bool:
        form = self.engine.load(self.form_id)
        if form is None:
            return False
        async with self.engine.lock(form):
            form = self.engine.load(self.form_id)
            if form is None or (self.gen is not None and form.gen != self.gen):
                return False
            await self.engine.render(form, screen)
            if self.gen is not None:
                self.gen = form.gen
            return True

    async def close(self) -> None:
        form = self.engine.load(self.form_id)
        if form is not None:
            await self.engine.close(form)


class Kit:
    def __init__(self, engine: Optional["ScreenEngine"], ctx: Any) -> None:
        self.engine = engine
        self.ctx = ctx

    def _t(self, key: str, default: str) -> str:
        translate = getattr(self.ctx, "t", None)
        return str(translate(key, default)) if callable(translate) else default

    @staticmethod
    def banner(name: str = "brand") -> str:
        if not name:
            name = "brand"
        clean_name = str(name).strip()
        if clean_name.startswith("https://") or clean_name.startswith("http://"):
            tag = "video" if clean_name.lower().split("?")[0].endswith(".mp4") else "img"
            return f'<{tag} src="{clean_name}"/>\n'
        entry = _resolve_asset(clean_name)
        if entry is None:
            return ""
        media_id, kind, _ = entry
        tag = "video" if kind == "video" else "img"
        return f'<{tag} src="tg://{kind}?id={media_id}"/>\n'

    @staticmethod
    def screen(text: str, rows: Optional[list[list[dict[str, Any]]]] = None, **options: Any) -> Screen:
        if options.get("banner"):
            options.setdefault("rich", True)
        return Screen(text, [row for row in (rows or []) if row], **options)

    @staticmethod
    def _target(fn: Any) -> dict[str, str]:
        if isinstance(fn, str):
            return {"m": _kernel, "f": fn}
        if not callable(fn):
            raise ScreenError("screen handler is not callable")
        name = str(getattr(fn, "__name__", ""))
        if getattr(fn, "__globals__", {}).get(name) is not fn:
            raise ScreenError(f"screen handler must be a module-level function: {name}")
        return {"m": _module_of(cast('Callable[..., Any]', fn)), "f": name}

    def _go(self, text: str, fn: Any, args: dict[str, Any], style: Optional[str] = None, icon: Optional[str] = None) -> dict[str, Any]:
        return _decorate({"text": text, "k": "go", **self._target(fn), "a": _plain(args)}, style, icon)

    def button(self, text: str, fn: Any, *, style: Optional[str] = None, icon: Optional[str] = None, **args: Any) -> dict[str, Any]:
        return self._go(text, fn, args, style, icon)

    def primary(self, text: str, fn: Any, *, icon: Optional[str] = None, **args: Any) -> dict[str, Any]:
        return self.button(text, fn, style="primary", icon=icon, **args)

    def success(self, text: str, fn: Any, *, icon: Optional[str] = None, **args: Any) -> dict[str, Any]:
        return self.button(text, fn, style="success", icon=icon, **args)

    def danger(self, text: str, fn: Any, *, icon: Optional[str] = None, **args: Any) -> dict[str, Any]:
        return self.button(text, fn, style="danger", icon=icon, **args)

    def input(self, text: str, fn: Any, placeholder: str = "", *, style: Optional[str] = None, icon: Optional[str] = None, secret: bool = False, **args: Any) -> dict[str, Any]:
        item: dict[str, Any] = {"text": text, "k": "input", "p": placeholder, **self._target(fn), "a": _plain(args)}
        if secret:
            item["s"] = True
        return _decorate(item, style, icon)

    @staticmethod
    def copy(text: str, value: str, *, icon: Optional[str] = "copy") -> dict[str, Any]:
        return _decorate({"text": text, "copy_text": str(value)[:copy_limit] or " "}, None, icon)

    @staticmethod
    def label(text: str) -> dict[str, Any]:
        return {"text": text, "disabled": True}

    @staticmethod
    def url(text: str, link: str, *, style: Optional[str] = None, icon: Optional[str] = None) -> dict[str, Any]:
        return _decorate({"text": text, "url": str(link)}, style, icon)

    @staticmethod
    def webapp(text: str, link: str, *, style: Optional[str] = None, icon: Optional[str] = None) -> dict[str, Any]:
        return _decorate({"text": text, "web_app": {"url": str(link)}}, style, icon)

    @staticmethod
    def link(text: str, module_id: str, fn: str, *, style: Optional[str] = None, icon: Optional[str] = None, **args: Any) -> dict[str, Any]:
        return _decorate({"text": text, "k": "go", "m": module_id, "f": fn, "a": _plain(args)}, style, icon)

    def close(self, text: Optional[str] = None) -> dict[str, Any]:
        return _decorate({"text": "✕ Close" if not text or text in {"Close", "Закрыть", "✕ Закрыть", "閉じる", "Жабу", "关闭", "Закрити"} else text, "k": "close"}, "danger", None)

    def back(self, fn: Any, text: Optional[str] = None, **args: Any) -> dict[str, Any]:
        return self.button(text or self._t("common.back", "Back"), fn, icon="back", **args)

    def footer(self, back: Any = None, **args: Any) -> list[dict[str, Any]]:
        return ([self.back(back, **args)] if back is not None else []) + [self.close()]

    @staticmethod
    def grid(items: list[dict[str, Any]], columns: int = 2) -> list[list[dict[str, Any]]]:
        columns = max(1, min(row_limit, columns))
        return [items[i:i + columns] for i in range(0, len(items), columns)]

    def pager(self, fn: Any, page: int, pages: int, **args: Any) -> list[dict[str, Any]]:
        if pages <= 1:
            return []
        left = self._go("‹", fn, {**args, "page": page - 1}) if page > 0 else self.label("·")
        right = self._go("›", fn, {**args, "page": page + 1}) if page + 1 < pages else self.label("·")
        return [left, self.label(f"{page + 1} / {pages}"), right]

    @staticmethod
    def page(items: list[Any], page: int, size: int) -> tuple[list[Any], int, int]:
        pages = max(1, (len(items) + size - 1) // size)
        page = max(0, min(int(page), pages - 1))
        return items[page * size:(page + 1) * size], page, pages

    @staticmethod
    def radio(text: str, selected: bool) -> str:
        return ("● " if selected else "○ ") + text

    @staticmethod
    def check(text: str, selected: bool) -> str:
        return ("✓ " if selected else "") + text


    @staticmethod
    def icon(name: Optional[str] = None) -> str:
        if not name:
            return ""
        if name.startswith("<tg-emoji") or name.startswith("<a "):
            return name
        return _ICONS.get(name, "")

    @staticmethod
    def module_icon(module_id: str) -> str:
        icon_name = _MODULE_ICONS.get(module_id, "folder")
        return _ICONS.get(icon_name, _ICONS["folder"])

    @staticmethod
    def wordmark() -> str:
        from relay.toolkit import TOOLS
        return TOOLS.wordmark()

    @classmethod
    def _label(cls, text: str, icon: Optional[str] = None, style: Optional[str] = None) -> str:
        mark = cls.icon(icon)
        body = _esc(text)
        return f"{mark} {body}" if mark and body else (mark or body or " ")

    def _spec(self, kind: str, fn: Any, args: dict[str, Any]) -> str:
        spec: dict[str, Any] = {"k": kind, "a": _plain(args), **self._target(fn)}
        raw = json.dumps(spec, ensure_ascii=False, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def tap(self, text: str, fn: Any, *, style: Optional[str] = "link", icon: Optional[str] = None, **args: Any) -> str:
        return f'<tg-button type="callback_data"{_style(style)} data-hs="{self._spec("go", fn, args)}">{self._label(text, icon, style)}</tg-button>'

    @classmethod
    def tap_copy(cls, text: str, value: str, *, icon: Optional[str] = "copy") -> str:
        target = html.escape(str(value)[:copy_limit] or " ", quote=True)
        return f'<tg-button type="copy_text" text="{target}">{cls._label(text, icon)}</tg-button>'

    @classmethod
    def badge(cls, text: str, style: Optional[str] = None, *, icon: Optional[str] = None) -> str:
        return f'<tg-button type="disabled"{_style(style)}>{cls._label(text, icon, style)}</tg-button>'

    @staticmethod
    def row(*buttons: str, align: str = "left") -> str:
        items = [item for item in buttons if item]
        return "".join(f'<tg-button-row align="{align}">' + "".join(items[i:i + row_limit]) + "</tg-button-row>" for i in range(0, len(items), row_limit))

    @staticmethod
    def table(rows: list[list[str]], *, keys: bool = False) -> str:
        body = "".join("<tr>" + "".join(f"<th>{cell}</th>" if keys and i == 0 else f"<td>{cell}</td>" for i, cell in enumerate(row)) + "</tr>" for row in rows)
        return f"<table bordered compact>{body}</table>"

    @staticmethod
    def details(summary: str, body: str, *, open: bool = False) -> str:
        return f"<details{' open' if open else ''}><summary>{summary}</summary>{body}</details>"

    async def show(self, screen: Screen, **options: Any) -> Any:
        if self.engine is None:
            raise ScreenError("screens are unavailable")
        return await self.engine.show(self.ctx, screen, **options)

    async def send(self, chat_id: Any, screen: Screen, **options: Any) -> Any:
        if self.engine is None:
            raise ScreenError("screens are unavailable")
        return await self.engine.send(str(getattr(self.ctx, "module_id", "") or ""), chat_id, screen, **options)


class _ReentrantLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._count = 0

    async def acquire(self) -> None:
        current = asyncio.current_task()
        if self._owner is current:
            self._count += 1
            return
        await self._lock.acquire()
        self._owner = current
        self._count = 1

    def release(self) -> None:
        current = asyncio.current_task()
        if self._owner is not current:
            raise RuntimeError("cannot release un-acquired lock")
        self._count -= 1
        if self._count == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def locked(self) -> bool:
        return self._lock.locked()


class ScreenEngine:
    def __init__(self, runtime: "Runtime", secret: str) -> None:
        self.runtime = runtime
        self._key = hashlib.sha256(("hotaru-screens:" + secret).encode()).digest()
        self._kernel: dict[str, Callable[..., Any]] = {}
        self._cache: dict[int, Form] = {}
        self._locks: dict[int, _ReentrantLock] = {}
        self._ready = False
        self.register("paginate", self._kernel_paginate)

    @property
    def kit(self) -> Kit:
        return Kit(self, getattr(self, "runtime", None))

    def _kernel_paginate(self, nav: Nav) -> Any:
        key = nav.arg("k")
        entry = _PAGE_CACHE.get(key)
        if not entry:
            return self._t("ui.expired", "This menu has expired. Open it again.")
        pages = entry["pages"]
        page = max(0, min(nav.int("page"), len(pages) - 1))
        rows = [self.kit.pager("paginate", page, len(pages), k=key), [self.kit.close()]]
        return Screen(pages[page], rows, rich=entry.get("rich", True))

    _columns = "id,module,actor,command,chat,message_id,inline_id,bot,gen,actions,text,rich,touched,ttl,language"

    @property
    def _db(self) -> Any:
        return getattr(getattr(self.runtime, "state", None), "connection", None)

    def _ensure(self) -> Any:
        db = self._db
        if db is None:
            raise ScreenError("screen storage is unavailable")
        if not self._ready:
            db.execute(
                "CREATE TABLE IF NOT EXISTS screen_forms ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, module TEXT NOT NULL, actor INTEGER NOT NULL, "
                "command TEXT, chat TEXT, message_id INTEGER, inline_id TEXT, bot INTEGER NOT NULL DEFAULT 0, "
                "gen INTEGER NOT NULL DEFAULT 0, actions TEXT NOT NULL DEFAULT '[]', text TEXT NOT NULL DEFAULT '', "
                "rich INTEGER NOT NULL DEFAULT 0, touched REAL NOT NULL, ttl REAL NOT NULL, language TEXT)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS screen_forms_message ON screen_forms(chat, message_id)")
            for form_id, raw in db.execute("SELECT id, actions FROM screen_forms WHERE module = 'config'").fetchall():
                actions = json.loads(raw or "[]")
                for action in actions:
                    if action.get("m") == "config":
                        action["m"] = "settings"
                db.execute("UPDATE screen_forms SET module = 'settings', actions = ? WHERE id = ?", (json.dumps(actions, ensure_ascii=False), form_id))
            db.commit()
            self._ready = True
        return db

    @staticmethod
    def _row(row: Any) -> Form:
        chat: Any = int(row[4]) if isinstance(row[4], str) and row[4].lstrip("-").isdigit() else row[4]
        return Form(
            id=int(row[0]), module=str(row[1]), actor=int(row[2]), command=row[3], chat=chat,
            message_id=row[5], inline_id=json.loads(row[6]) if row[6] else None, bot=bool(row[7]),
            gen=int(row[8]), actions=json.loads(row[9] or "[]"), text=str(row[10] or ""), rich=bool(row[11]),
            touched=float(row[12]), ttl=float(row[13]), language=row[14],
        )

    def load(self, form_id: int) -> Optional[Form]:
        form = self._cache.get(form_id)
        if form is not None:
            return form
        row = self._ensure().execute(f"SELECT {self._columns} FROM screen_forms WHERE id = ?", (form_id,)).fetchone()
        if row is None:
            return None
        form = self._row(row)
        self._remember(form)
        return form

    def _remember(self, form: Form) -> None:
        self._cache[form.id] = form
        while len(self._cache) > _cache:
            self._cache.pop(next(iter(self._cache)))

    def _save(self, form: Form) -> None:
        db = self._ensure()
        db.execute(
            "UPDATE screen_forms SET chat=?, message_id=?, inline_id=?, gen=?, actions=?, text=?, rich=?, touched=?, ttl=?, language=? WHERE id=?",
            (None if form.chat is None else str(form.chat), form.message_id, json.dumps(form.inline_id, default=str) if form.inline_id else None, form.gen,
             json.dumps(form.actions, ensure_ascii=False), form.text, int(form.rich), form.touched, form.ttl, form.language, form.id),
        )
        db.commit()
        self._remember(form)

    def _create(self, module: str, actor: int, command: Optional[str], chat: Any, *, bot: bool) -> Form:
        db = self._ensure()
        now = time.time()
        rows = int(db.execute("SELECT COUNT(*) FROM screen_forms").fetchone()[0] or 0)
        if rows >= _forms:
            db.execute("DELETE FROM screen_forms WHERE id IN (SELECT id FROM screen_forms ORDER BY touched ASC LIMIT ?)", (rows - _forms + 1,))
        cursor = db.execute(
            "INSERT INTO screen_forms(module, actor, command, chat, bot, touched, ttl, language) VALUES (?,?,?,?,?,?,?,?)",
            (module, int(actor), command, None if chat is None else str(chat), int(bot), now, 0.0, self._language()),
        )
        db.commit()
        form_id = int(cursor.lastrowid or 0)
        if not 0 < form_id < 2 ** 32:
            raise ScreenError("screen id space is exhausted")
        form = self.load(form_id)
        assert form is not None
        return form

    def drop(self, form: Form) -> None:
        db = self._ensure()
        db.execute("DELETE FROM screen_forms WHERE id = ?", (form.id,))
        db.commit()
        self._cache.pop(form.id, None)
        self._locks.pop(form.id, None)

    def drop_module(self, module_id: str) -> int:
        db = self._ensure()
        cursor = db.execute("DELETE FROM screen_forms WHERE module = ?", (module_id,))
        db.commit()
        for key in [key for key, form in self._cache.items() if form.module == module_id]:
            self._cache.pop(key, None)
        return int(cursor.rowcount or 0)

    def _language(self) -> Optional[str]:
        try:
            return str(self.runtime.language())
        except Exception:
            return None

    def _t(self, key: str, default: str, **params: Any) -> str:
        try:
            return self.runtime.t(key, default, **params)
        except Exception:
            return default.format(**params)

    def encode(self, form_id: int, gen: int, index: int) -> str:
        body = _pack.pack(_version, form_id, gen & 0xFFFF, index)
        return _prefix + seal_callback(self._key, body, aad=b"hotaru-screens")

    def decode(self, token: Any) -> Optional[tuple[int, int, int]]:
        if isinstance(token, (bytes, bytearray)):
            token = bytes(token).decode("ascii", "replace")
        if not isinstance(token, str) or not token.startswith(_prefix) or len(token) > 64:
            return None
        if len(token) == 49:
            body = open_callback(self._key, token[1:], aad=b"hotaru-screens")
            if body is None or len(body) != _pack.size:
                return None
            version, form_id, gen, index = _pack.unpack(body)
            return (form_id, gen, index) if version == _version else None
        try:
            raw = base64.urlsafe_b64decode(token[1:] + "=" * (-len(token[1:]) % 4))
        except Exception:
            return None
        if len(raw) != _pack.size + _tag:
            return None
        body, tag = raw[:_pack.size], raw[_pack.size:]
        if not hmac.compare_digest(tag, hmac.new(self._key, body, hashlib.sha256).digest()[:_tag]):
            return None
        version, form_id, gen, index = _pack.unpack(body)
        if version != _version:
            return None
        return form_id, gen, index

    @staticmethod
    def owns(data: Any) -> bool:
        if isinstance(data, (bytes, bytearray)):
            return bytes(data[:1]) == _prefix.encode()
        return isinstance(data, str) and data.startswith(_prefix)

    def register(self, name: str, handler: Callable[..., Any]) -> None:
        self._kernel[name] = handler

    def ref(self, form: Form) -> ScreenRef:
        return ScreenRef(self, form.id, None)

    def kernel_button(self, text: str, name: str, *, style: Optional[str] = None, **args: Any) -> dict[str, Any]:
        return _decorate({"text": text, "k": "go", "m": _kernel, "f": name, "a": _plain(args)}, style, None)

    def _action(self, form: Form, spec: dict[str, Any], actions: list[dict[str, Any]], gen: int) -> str:
        if len(actions) > 255:
            raise ScreenError("a screen can hold at most 256 actions")
        kind = str(spec.get("k"))
        action: dict[str, Any] = {"k": kind}
        if kind != "close":
            action.update(m=spec.get("m") or form.module, f=spec["f"], a=spec.get("a") or {})
        if kind == "input":
            action["p"] = str(spec.get("p") or "")
            if spec.get("s"):
                action["s"] = True
        actions.append(action)
        return self.encode(form.id, gen, len(actions) - 1)

    def _compile(self, form: Form, screen: Screen) -> tuple[str, list[list[dict[str, Any]]], list[dict[str, Any]], int]:
        gen = (form.gen + 1) & 0xFFFF
        actions: list[dict[str, Any]] = []
        rows: list[list[dict[str, Any]]] = []
        for row in screen.rows:
            current: list[dict[str, Any]] = []
            for spec in row:
                button: dict[str, Any] = {"text": spec.get("text", "")}
                for key in ("style", "icon_custom_emoji_id"):
                    if spec.get(key):
                        button[key] = spec[key]
                if spec.get("icon"):
                    icon_val = str(spec["icon"])
                    if icon_val in _ICONS:
                        match = re.search(r'emoji-id="(\d+)"', _ICONS[icon_val])
                        if match and not spec.get("style"):
                            button["icon_custom_emoji_id"] = match.group(1)
                    elif icon_val.isdigit() and not spec.get("style"):
                        button["icon_custom_emoji_id"] = icon_val
                kind = spec.get("k")
                if kind == "close":
                    button["style"] = "danger"
                    if not button.get("text") or button.get("text") in {"Close", "Закрыть", "✕ Закрыть", "閉じる", "Жабу", "关闭", "Закрити"}:
                        button["text"] = "✕ Close"
                if kind in {"go", "input", "close"}:
                    token = self._action(form, spec, actions, gen)
                    button["switch_inline_query_current_chat" if kind == "input" else "callback_data"] = "hotaru-input:" + token + " " if kind == "input" else token
                else:
                    for key in ("callback_data", "url", "copy_text", "disabled", "switch_inline_query", "switch_inline_query_current_chat", "user_id", "web_app"):
                        if key in spec:
                            button[key] = spec[key]
                    if not set(button) - {"text", "style"}:
                        button["disabled"] = True
                current.append(button)
            if current:
                rows.append(current)
        if screen.banner:
            banner_prefix = screen.banner if screen.banner.endswith("\n") else (screen.banner + "\n")
            stripped_text = screen.text.lstrip("\n")
            text = f"{banner_prefix}{stripped_text}"
        else:
            text = screen.text
        text = f"{screen.notice}\n{text}" if screen.notice else text

        def swap(match: "re.Match[str]") -> str:
            raw = match.group(2)
            try:
                spec = cast('dict[str, Any]', json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))))
            except Exception as exc:
                raise ScreenError("malformed in-text button") from exc
            if spec.get("k") not in {"go", "input"}:
                raise ScreenError("malformed in-text button")
            token = self._action(form, spec, actions, gen)
            target = f'query="hotaru-input:{token} "' if spec.get("k") == "input" else f'data="{token}"'
            return f"<tg-button{match.group(1)} {target}{match.group(3)}>"

        text = _tap.sub(swap, text)
        screen.rich = bool(screen.rich or _rich.search(text))
        return text, rows, actions, gen

    def _commit(self, form: Form, text: str, actions: list[dict[str, Any]], gen: int, screen: Screen) -> None:
        form.text, form.actions, form.gen, form.rich = text, actions, gen, bool(screen.rich)
        form.touched = time.time()
        self._save(form)

    def _actor_of(self, source: Any) -> int:
        for value in (getattr(source, "_hotaru_actor_id", None), getattr(source, "from_id", None)):
            if isinstance(value, int) and value > 0:
                return value
        user = cast(Any, getattr(source, "from_user", None) or getattr(source, "sender", None))
        if isinstance(user, dict):
            u_dict = cast('dict[str, Any]', user)
            uid = u_dict.get("id")
            if isinstance(uid, int) and uid > 0:
                return uid
        elif user is not None:
            uid = getattr(user, "id", None)
            if isinstance(uid, int) and uid > 0:
                return uid
        chat = cast(Any, getattr(source, "chat", None))
        if isinstance(chat, dict):
            c_dict = cast('dict[str, Any]', chat)
            if c_dict.get("type") == "private":
                cid_val = c_dict.get("id")
                if isinstance(cid_val, int) and cid_val > 0:
                    return cid_val
        elif chat is not None:
            if getattr(chat, "type", None) == "private":
                cid_val = getattr(chat, "id", None)
                if isinstance(cid_val, int) and cid_val > 0:
                    return cid_val
        cid = getattr(source, "chat_id", None)
        if isinstance(cid, int) and cid > 0:
            return cid
        return int(getattr(getattr(self.runtime, "kernel", None), "owner_id", None) or 0)

    def _command_of(self, source: Any) -> Optional[str]:
        command = getattr(source, "_hotaru_command", None)
        if isinstance(command, str) and command:
            return command
        text = str(getattr(source, "text", "") or "").strip()
        if text.startswith("/"):
            return text[1:].split()[0].split("@")[0].casefold()
        kernel = getattr(self.runtime, "kernel", None)
        return kernel.command_name(text) if kernel is not None else None

    def _bot_app(self) -> Any:
        app = getattr(getattr(self.runtime, "inline", None), "bot_app", None)
        if app is None:
            raise ScreenError("inline bot is not ready")
        return app

    async def rich(self, text: str) -> dict[str, Any]:
        return await self._rich_impl(text)

    _rich = rich

    async def _rich_impl(self, text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"_": "inputRichMessageHTML", **to_rich(text)}
        matches = re.findall(r'tg://(photo|video)\?id=(hotaru-[a-zA-Z0-9_-]+)', text)
        web_matches = re.findall(r'<(img|video)\s+src="(https?://[^"\'>]+)"\s*/?>', text)
        needed: list[tuple[str, str, Any, bool]] = []
        for kind, mid in matches:
            if mid.startswith("hotaru-web-"):
                continue
            asset_name = mid.removeprefix("hotaru-")
            entry = _resolve_asset(asset_name)
            if entry is not None and not any(item[0] == mid for item in needed):
                needed.append((entry[0], entry[1], entry[2], False))
        for tag, url in web_matches:
            kind = "video" if (tag == "video" or url.lower().split("?")[0].endswith(".mp4")) else "photo"
            h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
            mid = f"hotaru-web-{h}"
            if not any(item[0] == mid for item in needed):
                needed.append((mid, kind, url, True))
        if not needed:
            if "html" in payload:
                payload["html"] = re.sub(r'<(?:img|video)\s+src="https?://[^"]*"\s*/?>', "", payload["html"])
            return payload
        try:
            app = self._bot_app()
        except Exception:
            app = getattr(self.runtime, "app", None)
        if app is None:
            return payload
        files: list[dict[str, Any]] = []
        async with _banner_lock:
            for media_id, kind, path_or_url, is_web in needed:
                key = (id(app), media_id)
                cached = _banner_cache.get(key)
                state_obj = getattr(self.runtime, "state", None)
                if cached is None and state_obj is not None:
                    try:
                        state_val = state_obj.get_setting("banner:" + media_id)
                        if isinstance(state_val, dict):
                            entry_stored = dict(state_val)
                            for sub in ("photo", "document"):
                                if sub in entry_stored and isinstance(entry_stored[sub], dict):
                                    ref_b64 = entry_stored[sub].get("file_reference")
                                    if isinstance(ref_b64, str):
                                        entry_stored[sub]["file_reference"] = base64.b64decode(ref_b64.encode("ascii"))
                            cached = (time.monotonic(), entry_stored)
                            _banner_cache[key] = cached
                    except Exception:
                        pass
                if cached is not None and time.monotonic() - cached[0] < 86400:
                    files.append(cached[1])
                    if is_web:
                        tag = "video" if kind == "video" else "img"
                        payload["html"] = re.sub(
                            rf'<(?:img|video)\s+src="{re.escape(str(path_or_url))}"\s*/?>',
                            f'<{tag} src="tg://{kind}?id={media_id}"/>',
                            payload.get("html", "")
                        )
                    continue
                temp_p: Path | None = None
                try:
                    if is_web:
                        url_name = Path(urllib.parse.urlparse(str(path_or_url)).path).name
                        local_asset = _assets / url_name if url_name else None
                        if local_asset is not None and local_asset.exists():
                            actual_path = local_asset
                        else:
                            content = b""
                            curr_url = str(path_or_url)
                            for _ in range(4):
                                p_url = urllib.parse.urlparse(curr_url)
                                p_port = p_url.port or (443 if p_url.scheme == "https" else 80)
                                p_path = p_url.path or "/"
                                if p_url.query:
                                    p_path = f"{p_path}?{p_url.query}"
                                c_cls = http.client.HTTPSConnection if p_url.scheme == "https" else http.client.HTTPConnection
                                conn = c_cls(p_url.hostname or "", port=p_port, timeout=12.0)
                                try:
                                    conn.request("GET", p_path, headers={"User-Agent": "Hotaru/1.0"})
                                    h_resp = conn.getresponse()
                                    if h_resp.status in (301, 302, 303, 307, 308):
                                        redir = h_resp.getheader("Location")
                                        if redir:
                                            curr_url = urllib.parse.urljoin(curr_url, redir)
                                            continue
                                    content = h_resp.read()
                                    break
                                finally:
                                    conn.close()
                            ext = ".mp4" if kind == "video" else ".png"
                            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as handle:
                                temp_p = Path(handle.name)
                                temp_p.write_bytes(content)
                            actual_path = temp_p
                    else:
                        actual_path = cast('Path', path_or_url)
                    entry = await asyncio.wait_for(_upload_asset(app, media_id, kind, actual_path), timeout=2.5)
                    _banner_cache[key] = (time.monotonic(), entry)
                    state_obj = getattr(self.runtime, "state", None)
                    if state_obj is not None:
                        try:
                            def _enc(o: Any) -> Any:
                                return base64.b64encode(bytes(o)).decode("ascii") if isinstance(o, (bytes, bytearray)) else str(o)
                            save_entry = json.loads(json.dumps(entry, default=_enc))
                            state_obj.set_setting("banner:" + media_id, save_entry)
                        except Exception:
                            pass
                    files.append(entry)
                    if is_web:
                        tag = "video" if kind == "video" else "img"
                        payload["html"] = re.sub(
                            rf'<(?:img|video)\s+src="{re.escape(str(path_or_url))}"\s*/?>',
                            f'<{tag} src="tg://{kind}?id={media_id}"/>',
                            payload.get("html", "")
                        )
                except Exception as exc:
                    log.warning("screen banner upload failed for %s (%s): %s: %s", media_id, path_or_url, type(exc).__name__, exc)
                    tag = "video" if kind == "video" else "img"
                    if "html" in payload:
                        payload["html"] = payload["html"].replace(f'<{tag} src="tg://{kind}?id={media_id}"/>', "")
                        if is_web:
                            payload["html"] = re.sub(rf'<(?:img|video)\s+src="{re.escape(str(path_or_url))}"\s*/?>', "", payload["html"])
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("screens", "banner_failed", asset=media_id, error=type(exc).__name__)
                finally:
                    if temp_p is not None:
                        temp_p.unlink(missing_ok=True)
        if files:
            payload["files"] = files
        if "html" in payload:
            payload["html"] = re.sub(r'<(?:img|video)\s+src="https?://[^"]*"\s*/?>', "", payload["html"])
        return payload

    async def show(self, ctx: Any, screen: Screen, *, reply_to: Optional[int] = None, fallback: bool = True) -> Optional[Form]:
        source = getattr(ctx, "_delivery_source", None) or getattr(ctx, "message", None)
        module_id = str(getattr(ctx, "module_id", "") or "")
        if source is None or not module_id:
            raise ScreenError("screen has no source message")
        return await self.open(module_id, source, screen, reply_to=reply_to, respond=ctx.respond if fallback else None)

    async def open(self, module_id: str, source: Any, screen: Screen, *, reply_to: Optional[int] = None, respond: Optional[Callable[..., Any]] = None) -> Optional[Form]:
        runtime = self.runtime
        if getattr(source, "src", None) == "bot":
            return await self.send(module_id, getattr(source, "chat_id", None), screen, actor=self._actor_of(source), command=self._command_of(source))
        form = self._create(module_id, self._actor_of(source), self._command_of(source), getattr(source, "chat_id", None), bot=False)
        if screen.banner:
            screen.rich = True
        text, rows, actions, gen = self._compile(form, screen)
        try:
            try:
                sent, nonce = await runtime.deliver_inline(source, text, rows, rich=screen.rich, reply_to=reply_to)
            except Exception as exc:
                if not screen.banner:
                    raise
                log.warning("screen banner failed for module %s: %s: %s", module_id, type(exc).__name__, exc)
                if runtime.observatory is not None:
                    runtime.observatory.emit("screens", "banner_failed", module=module_id, error=type(exc).__name__, detail=str(exc))
                screen.banner = ""
                screen.rich = False
                text, rows, actions, gen = self._compile(form, screen)
                sent, nonce = await runtime.deliver_inline(source, text, rows, rich=screen.rich, reply_to=reply_to)
        except Exception as exc:
            self.drop(form)
            log.error("screen delivery failed for module %s: %s: %s", module_id, type(exc).__name__, exc)
            if runtime.observatory is not None:
                runtime.observatory.emit("screens", "delivery_failed", module=module_id, error=type(exc).__name__, detail=str(exc))
            raise
        form.message_id = sent.get("id") if isinstance(sent, dict) and isinstance(sent.get("id"), int) else None
        self._commit(form, text, actions, gen, screen)
        asyncio.get_running_loop().create_task(self._bind(form.id, nonce, gen, rows, has_emoji(text)))
        return form

    async def _bind(self, form_id: int, nonce: str, gen: int, rows: list[list[dict[str, Any]]], repaint: bool) -> None:
        inline_id = await self.runtime.chosen_inline_id(nonce)
        form = self.load(form_id)
        if form is None or not isinstance(inline_id, dict):
            return
        async with self.lock(form):
            if form.inline_id is None:
                form.inline_id = cast('dict[str, Any]', inline_id)
                self._save(form)
            if repaint and form.gen == gen and not form.rich:
                try:
                    await self._edit(form, form.text, rows, form.rich)
                except Exception as exc:
                    log.error("screen repaint failed: %s: %s\n%s", type(exc).__name__, exc, traceback.format_exc())

    async def send(self, module_id: str, chat_id: Any, screen: Screen, *, actor: Optional[int] = None, command: Optional[str] = None, reply_to: Optional[int] = None) -> Form:
        app = self._bot_app()
        owner = getattr(getattr(self.runtime, "kernel", None), "owner_id", None)
        form = self._create(module_id, int(actor or owner or 0), command, chat_id, bot=True)
        text, rows, actions, gen = self._compile(form, screen)
        async def _send() -> Any:
            data: dict[str, Any] = {"peer": chat_id, "random_id": secrets.randbits(63), "no_webpage": True}
            if reply_to is not None:
                data["reply_to"] = {"_": "inputReplyToMessage", "reply_to_msg_id": int(reply_to)}
            plain, entities = to_entities(text)
            data["message"] = plain
            if entities:
                data["entities"] = entities
            if screen.rich:
                data["rich_message"] = await self._rich(text)
            markup = kbd_to_tl({"inline_keyboard": rows})
            if markup is not None and markup.get("rows"):
                data["reply_markup"] = markup
            with trusted_scope():
                try:
                    return await app.mt_messages_send_message(**data)
                except Exception as exc:
                    reopen = getattr(getattr(self.runtime, "inline", None), "reopen_owner_chat", None)
                    blocked = any(item in str(exc).lower() for item in ("chat not found", "blocked", "peer_id_invalid", "input_user_deactivated"))
                    if not blocked or not callable(reopen) or not await cast('Awaitable[bool]', reopen(chat_id)):
                        raise
                    return await app.mt_messages_send_message(**data)
        try:
            try:
                try:
                    result = await _send()
                except Exception as exc:
                    if not screen.rich:
                        raise
                    log.error("screen rich delivery failed for module %s: %s: %s", module_id, type(exc).__name__, exc)
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("screens", "rich_failed", module=module_id, error=type(exc).__name__, detail=str(exc))
                    screen.rich = False
                    result = await _send()
            except Exception as exc:
                if not screen.banner:
                    raise
                log.warning("screen banner failed for module %s: %s: %s", module_id, type(exc).__name__, exc)
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("screens", "banner_failed", module=module_id, error=type(exc).__name__, detail=str(exc))
                screen.banner = ""
                screen.rich = False
                text, rows, actions, gen = self._compile(form, screen)
                result = await _send()
        except Exception as exc:
            self.drop(form)
            log.error("screen delivery failed for module %s: %s: %s", module_id, type(exc).__name__, exc)
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("screens", "delivery_failed", module=module_id, error=type(exc).__name__, detail=str(exc))
            raise
        sent = extract_sent_message(result)
        form.message_id = sent.get("id") if isinstance(sent, dict) and isinstance(sent.get("id"), int) else None
        self._commit(form, text, actions, gen, screen)
        return form

    async def render(self, form: Form, screen: Screen) -> None:
        if screen.banner:
            screen.rich = True
        text, rows, actions, gen = self._compile(form, screen)
        try:
            await self._edit(form, text, rows, screen.rich)
        except Exception as exc:
            if not screen.banner:
                raise
            log.warning("screen banner failed for module %s: %s: %s", form.module, type(exc).__name__, exc)
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("screens", "banner_failed", module=form.module, error=type(exc).__name__, detail=str(exc))
            screen.banner = ""
            screen.rich = False
            text, rows, actions, gen = self._compile(form, screen)
            await self._edit(form, text, rows, screen.rich)
        self._commit(form, text, actions, gen, screen)

    async def _edit(self, form: Form, text: str, rows: list[list[dict[str, Any]]], rich: bool) -> None:
        from .callbacks import CallbackContext

        markup = kbd_to_tl({"inline_keyboard": rows})
        data: dict[str, Any] = {"no_webpage": True}
        if markup is not None and markup.get("rows"):
            data["reply_markup"] = markup
        elif form.inline_id is None:
            data["reply_markup"] = {"_": "replyInlineMarkup", "rows": []}
        plain, entities = to_entities(text)
        if entities:
            data["entities"] = entities
        if rich:
            data["rich_message"] = await self._rich(text)
        app = self._bot_app()
        for attempt in range(3):
            try:
                if form.inline_id is not None:
                    await CallbackContext.edit_inline(app, form.inline_id, plain, data)
                elif form.bot and form.message_id is not None:
                    with trusted_scope():
                        await app.mt_messages_edit_message(peer=form.chat, id=form.message_id, message=plain, **data)
                else:
                    raise ScreenError("form has no editable identity yet")
                return
            except MessageNotModifiedError:
                return
            except FloodWaitError as exc:
                if attempt or exc.seconds > 8:
                    raise
                await asyncio.sleep(exc.seconds + 0.5)
            except Exception as exc:
                if "rich_message" not in data:
                    raise
                log.error("screen rich edit failed for module %s: %s: %s", form.module, type(exc).__name__, exc)
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("screens", "rich_failed", module=form.module, error=type(exc).__name__, detail=str(exc))
                data.pop("rich_message", None)

    async def close(self, form: Form) -> None:
        async with self.lock(form):
            await self._close(form)

    async def _close(self, form: Form) -> None:
        app = self._bot_app() if form.bot else getattr(self.runtime, "app", None)
        try:
            if form.message_id is None or form.chat is None or app is None:
                raise ScreenError("form has no message")
            with trusted_scope():
                await delete_chat_msg(app, form.chat, form.message_id)
        except Exception as exc:
            log.error("screen delete failed for module %s: %s: %s", form.module, type(exc).__name__, exc)
            try:
                await self._edit(form, form.text, [], form.rich)
            except Exception as inner:
                log.error("screen cleanup failed for module %s: %s: %s", form.module, type(inner).__name__, inner)
        self.drop(form)

    def lock(self, form: Form) -> _ReentrantLock:
        lock = self._locks.get(form.id)
        if lock is None:
            if len(self._locks) > 1024:
                for key in [key for key, item in self._locks.items() if not item.locked()]:
                    self._locks.pop(key, None)
            lock = self._locks[form.id] = _ReentrantLock()
        return lock

    def _allowed(self, form: Form, actor: Any, action: dict[str, Any]) -> bool:
        runtime = self.runtime
        if not isinstance(actor, int) or actor <= 0:
            return False
        if runtime.access is not None and runtime.access.is_owner(actor):
            return True
        owner_id = getattr(getattr(runtime, "kernel", None), "owner_id", None)
        if owner_id is not None and actor == owner_id:
            return True
        target = str(action.get("m") or form.module)
        if actor != form.actor or target != form.module or not form.command or runtime.kernel is None:
            return False
        spec = runtime.kernel.registry.resolve_name(form.command)
        if spec is None and hasattr(runtime.kernel.registry, "resolve_bot_name"):
            spec = runtime.kernel.registry.resolve_bot_name(form.command)
        if spec is None or spec.module_id != form.module:
            return False
        return bool(runtime.kernel.is_authorized(SimpleNamespace(from_id=actor, chat_id=form.chat), spec))

    def _handler(self, action: dict[str, Any], form: Form) -> Callable[..., Any]:
        module_id = str(action.get("m") or form.module)
        name = str(action.get("f") or "")
        if module_id == _kernel:
            handler = self._kernel.get(name)
        else:
            active = self.runtime.modules.get(module_id) if self.runtime.modules is not None else None
            namespace = getattr(getattr(active, "context", None), "namespace", None)
            if isinstance(namespace, dict) and cast("dict[str, Any]", namespace).get("__sandbox__") and name.isidentifier() and not name.startswith("__"):
                sandbox = self.runtime.sandbox
                if sandbox is None or module_id != form.module:
                    raise ScreenError(f"screen handler is unavailable: {module_id}.{name}")

                async def sandbox_handler(ctx: Any, nav: Nav) -> Any:
                    if self.runtime.modules is None or self.runtime.modules.get(module_id) is not active:
                        raise ScreenError(f"screen handler is unavailable: {module_id}.{name}")
                    source = SimpleNamespace(from_id=nav.actor, chat_id=form.chat, message_id=form.message_id, id=form.message_id, _hotaru_command=form.command)
                    payload: dict[str, Any] = {"chat_id": form.chat, "message_id": form.message_id, "source": "screen", "language": form.language}
                    if self.runtime.lexicon is not None:
                        payload["translations"] = self.runtime.lexicon.bundle(form.language or self.runtime.language())
                    result = await sandbox.call(module_id, form.command or name, [], payload, source=source, target=name, context=ctx, nav=nav)
                    if isinstance(result, dict):
                        result = cast("dict[str, Any]", result)
                        if isinstance(result.get("text"), str) and isinstance(result.get("rows", []), list):
                            normalize = cast(Callable[..., Any], getattr(sandbox, "_sandbox_buttons"))
                            rows = normalize(module_id, result.get("rows") or [], form.chat, nav.actor)
                            return Screen(result["text"], rows, toast=result.get("toast"), alert=bool(result.get("alert")), rich=bool(result.get("rich")), notice=result.get("notice"), banner=str(result.get("banner") or ""))
                    return cast(Any, result)

                return sandbox_handler
            handler = cast('dict[str, Any]', namespace).get(name) if isinstance(namespace, dict) and not name.startswith("__") else None
        if not callable(handler):
            raise ScreenError(f"screen handler is unavailable: {module_id}.{name}")
        return cast('Callable[..., Any]', handler)

    async def _invoke(self, form: Form, action: dict[str, Any], event: Any, nav: Nav) -> Any:
        handler = self._handler(action, form)
        module_id = str(action.get("m") or form.module)
        if module_id == _kernel:
            result = handler(nav)
        else:
            factory = getattr(self.runtime, "context_factory", None)
            if factory is None:
                raise ScreenError("module context is unavailable")
            with module_scope(module_id):
                result = handler(factory.create(module_id, event), nav)
        if inspect.isawaitable(result):
            with module_scope(module_id if module_id != _kernel else ""):
                result = await result
        return result

    async def dispatch(self, callback: Any) -> None:
        expired = self._t("ui.expired", "This menu has expired. Open it again.")
        decoded = self.decode(getattr(callback, "data", None))
        form = self.load(decoded[0]) if decoded is not None else None
        log.info(
            "screen dispatch data=%r decoded=%s form=%s inline_id=%s chat=%s msg=%s actor=%s",
            getattr(callback, "data", None),
            decoded,
            form.id if form is not None else None,
            str(getattr(callback, "inline_message_id", None))[:120],
            getattr(callback, "chat_id", None),
            str(getattr(callback, "msg_id", None))[:120],
            getattr(callback, "from_id", None),
        )
        if decoded is None or form is None:
            log.info("screen dispatch expired: decoded=%s form=%s", decoded, form is not None)
            await self._answer(callback, expired, alert=True)
            if form is not None:
                await self._strip(form, callback)
            return
        _, gen, index = decoded
        async with self.lock(form):
            form = self.load(form.id) or form
            if form.gen != gen or index >= len(form.actions):
                log.info("screen dispatch stale: form=%s gen=%s/%s index=%s actions=%s", form.id, gen, form.gen, index, len(form.actions))
                await self._answer(callback, self._t("ui.stale", "The menu has changed, try again."))
                return
            inline_id: Any = getattr(callback, "inline_message_id", None)
            if inline_id is None and isinstance(getattr(callback, "msg_id", None), dict):
                inline_id = callback.msg_id
            if inline_id is not None:
                if form.inline_id is None:
                    form.inline_id = cast('dict[str, Any]', inline_id)
                    self._save(form)
                elif _inline_key(form.inline_id) != _inline_key(inline_id):
                    log.info("screen dispatch inline mismatch: form=%s expected=%s got=%s", form.id, _inline_key(form.inline_id), _inline_key(inline_id))
                    return
            elif form.bot and (str(getattr(callback, "chat_id", None)) != str(form.chat) or getattr(callback, "msg_id", None) != form.message_id):
                log.info("screen dispatch bot message mismatch: form=%s chat=%s/%s msg=%s/%s", form.id, getattr(callback, "chat_id", None), form.chat, getattr(callback, "msg_id", None), form.message_id)
                return
            action = form.actions[index]
            actor = getattr(callback, "from_id", None)
            if not self._allowed(form, actor, action):
                log.info("screen dispatch actor denied: form=%s actor=%s module=%s command=%s", form.id, actor, form.module, form.command)
                return
            security = getattr(self.runtime, "security", None)
            if security is not None:
                from .security import AccessVerdict
                if security.check(callback, transport="mt", module_id=form.module, authorized=True) is not AccessVerdict.ALLOW:
                    log.info("screen dispatch rate limited: form=%s actor=%s", form.id, actor)
                    await self._answer(callback, self._t("ui.slow_down", "Too fast, wait a second."))
                    return
            if action.get("k") == "close":
                log.info("screen dispatch close: form=%s", form.id)
                await self._answer(callback)
                await self._close(form)
                return
            log.info("screen dispatch action: form=%s handler=%s.%s kind=%s", form.id, action.get("m") or form.module, action.get("f"), action.get("k"))
            nav = Nav(self, form, action, call=callback, actor=cast(int, actor))
            try:
                result = await self._invoke(form, action, callback, nav)
            except ScreenError as exc:
                log.error("screen dispatch handler unavailable: %s", exc)
                await nav.answer(expired, alert=True)
                return
            except Exception as exc:
                self._report(form, action, exc)
                await nav.answer(self._t("ui.failed", "Something went wrong: {error}", error=type(exc).__name__), alert=True)
                return
            log.info("screen dispatch result: form=%s handler=%s type=%s", form.id, action.get("f"), type(result).__name__)
            await self._finish(form, action, nav, result)

    async def _finish(self, form: Form, action: dict[str, Any], nav: Nav, result: Any) -> None:
        if not isinstance(result, Screen):
            await nav.answer(result if isinstance(result, str) else None)
            return
        if self.load(form.id) is None:
            await nav.answer(result.toast, alert=result.alert)
            return
        try:
            await self.render(form, result)
        except FloodWaitError as exc:
            await nav.answer(self._t("ui.flood", "Telegram asks to wait {seconds} s.", seconds=exc.seconds), alert=True)
            return
        except MessageIdInvalidError:
            self.drop(form)
            await nav.answer(self._t("ui.expired", "This menu has expired. Open it again."), alert=True)
            return
        except Exception as exc:
            self._report(form, action, exc)
            await nav.answer(self._t("ui.failed", "Something went wrong: {error}", error=type(exc).__name__), alert=True)
            return
        await nav.answer(result.toast, alert=result.alert)

    @staticmethod
    async def _answer(callback: Any, text: Optional[str] = None, *, alert: bool = False) -> None:
        try:
            await callback.answer(_clip(text), alert=alert)
        except Exception as exc:
            log.error("screen callback answer failed: %s: %s", type(exc).__name__, exc)

    async def _strip(self, form: Form, callback: Any) -> None:
        if form.inline_id is None and isinstance(getattr(callback, "msg_id", None), dict):
            form.inline_id = callback.msg_id
        try:
            await self._edit(form, form.text, [], form.rich)
        except Exception as exc:
            log.error("screen strip failed for module %s: %s: %s", form.module, type(exc).__name__, exc)
        self.drop(form)

    def _report(self, form: Form, action: dict[str, Any], exc: BaseException) -> None:
        log.error(
            "screen handler %s.%s failed: %s: %s\n%s",
            action.get("m") or form.module,
            action.get("f"),
            type(exc).__name__,
            exc,
            traceback.format_exc(),
        )
        if self.runtime.observatory is not None:
            self.runtime.observatory.emit("screens", "handler_failed", level="error", module=form.module, handler=str(action.get("f")), error=type(exc).__name__, detail=str(exc)[:240], tb=traceback.format_exc()[-4000:])

    def _input_action(self, token: str) -> Optional[tuple[Form, dict[str, Any]]]:
        decoded = self.decode(token)
        form = self.load(decoded[0]) if decoded is not None else None
        if decoded is None or form is None or form.gen != decoded[1] or decoded[2] >= len(form.actions):
            return None
        action = form.actions[decoded[2]]
        return (form, action) if action.get("k") == "input" else None

    def input_request(self, token: str) -> Optional[tuple[Any, ...]]:
        found = self._input_action(token)
        if found is None:
            return None
        form, action = found
        source = SimpleNamespace(from_id=form.actor, chat_id=form.chat, _hotaru_command=form.command, _hotaru_input_secret=bool(action.get("s")))
        return self._submit, token, source, None, action.get("p", ""), None

    async def _submit(self, call: Any, value: str, token: str) -> None:
        found = self._input_action(token)
        if found is None:
            return
        async with self.lock(found[0]):
            found = self._input_action(token)
            if found is None:
                return
            form, action = found
            actor = getattr(call.query, "from_id", None)
            if not self._allowed(form, actor, action):
                return
            action["k"] = "submitted"
            self._save(form)
            try:
                result = await self._invoke(form, action, call.query, Nav(self, form, action, value=value.strip(), actor=cast(int, actor)))
                if isinstance(result, Screen) and self.load(form.id) is not None:
                    await self.render(form, result)
            except Exception as exc:
                self._report(form, action, exc)
