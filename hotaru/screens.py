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
import time
import traceback
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, cast

from goygram.errors import FloodWaitError, MessageIdInvalidError, MessageNotModifiedError
from goygram.sugar import extract_sent_message
from relay.emoji import has_emoji, premium, to_entities, to_rich
from relay.firewall import module_scope, trusted_scope
from relay.rpc import delete_chat_msg

from . import icons
from .callback_codec import open_callback, seal_callback
from .markup import copy_limit, row_limit, kbd_to_tl
from .plainfmt import rich_to_plain

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger(__name__)

_prefix = "~"
_version = 1
_pack = struct.Struct("<BIHB")
_tag = 8
_toast = 200
_ttl = 7 * 86400.0
_kernel = "@kernel"
_cache = 256
_styles = {"primary", "danger", "success", "link"}
_tap = re.compile(r'<tg-button(\s[^>]*?)\sdata-hs="([A-Za-z0-9_-]+)"([^>]*)>')
_rich = re.compile(r"<(?:table|tg-button|tg-button-row|details|h[1-6]|hr|footer|ul|ol|aside|tg-time|tg-math|mark|sup|sub|img|video)\b", re.IGNORECASE)
_mark = re.compile(r"^[^\w\s<]+\s+")


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


def bare(text: str) -> str:
    return _mark.sub("", text, count=1)


def _decorate(item: dict[str, Any], style: Optional[str], icon: Optional[str]) -> dict[str, Any]:
    if style:
        item["style"] = style
    if icon:
        item["icon"] = icon
        item["text"] = bare(str(item.get("text", "")))
    return item


def _clip(text: Optional[str], limit: int = _toast) -> Optional[str]:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
    ttl: Optional[float] = None
    notice: Optional[str] = None

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

    @property
    def expired(self) -> bool:
        return self.touched + self.ttl < time.time()


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
        except Exception:
            pass

    async def toast(self, text: str) -> None:
        await self.answer(text)

    def ref(self, *, follow: bool = False) -> "ScreenRef":
        # pinned to the screen this press renders, unless the flow owns the form
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
        from .branding import banner
        return banner(name)

    @staticmethod
    def screen(text: str, rows: Optional[list[list[dict[str, Any]]]] = None, **options: Any) -> Screen:
        return Screen(text, [row for row in (rows or []) if row], **options)

    @staticmethod
    def _target(fn: Any) -> dict[str, str]:
        if isinstance(fn, str):
            return {"f": fn}
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
    def link(text: str, module_id: str, fn: str, *, style: Optional[str] = None, icon: Optional[str] = None, **args: Any) -> dict[str, Any]:
        return _decorate({"text": text, "k": "go", "m": module_id, "f": fn, "a": _plain(args)}, style, icon)

    def close(self, text: Optional[str] = None) -> dict[str, Any]:
        return _decorate({"text": text or self._t("common.close", "Close"), "k": "close"}, None, "close")

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
    def icon(name: Optional[str]) -> str:
        if not name:
            return ""
        return icons.emoji(name) if premium() else _esc(icons.fallback(name))

    @staticmethod
    def wordmark() -> str:
        return icons.wordmark() if premium() else "<b>HOTARU</b>"

    @classmethod
    def _label(cls, text: str, icon: Optional[str], style: Optional[str] = None) -> str:
        if style in {"primary", "success", "danger"} and premium():
            icon = None  # coloured icons vanish on coloured buttons
        mark = cls.icon(icon)
        body = _esc(bare(text) if mark else text)
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


class ScreenEngine:
    def __init__(self, runtime: "Runtime", secret: str) -> None:
        self.runtime = runtime
        self._key = hashlib.sha256(("hotaru-screens:" + secret).encode()).digest()
        self._kernel: dict[str, Callable[..., Any]] = {}
        self._cache: dict[int, Form] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._targets: Optional[set[tuple[str, int]]] = None
        self._ready = False

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
            # Keep old config menus working after the merge.
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

    def _create(self, module: str, actor: int, command: Optional[str], chat: Any, *, bot: bool, ttl: float) -> Form:
        db = self._ensure()
        now = time.time()
        db.execute("DELETE FROM screen_forms WHERE touched + ttl < ?", (now,))
        cursor = db.execute(
            "INSERT INTO screen_forms(module, actor, command, chat, bot, touched, ttl, language) VALUES (?,?,?,?,?,?,?,?)",
            (module, int(actor), command, None if chat is None else str(chat), int(bot), now, ttl, self._language()),
        )
        db.commit()
        form_id = int(cursor.lastrowid or 0)
        if not 0 < form_id < 2 ** 32:
            raise ScreenError("screen id space is exhausted")
        form = self.load(form_id)
        assert form is not None
        return form

    def drop(self, form: Form) -> None:
        self._target(form, False)
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
                icon = icons.get(spec.get("icon"))
                if icon is not None and not premium():
                    button["text"] = f"{icon[1]} {button['text']}".strip()
                elif icon is not None and not spec.get("style"):
                    button["icon_custom_emoji_id"] = str(icon[0])
                kind = spec.get("k")
                if kind in {"go", "input", "close"}:
                    token = self._action(form, spec, actions, gen)
                    button["switch_inline_query_current_chat" if kind == "input" else "callback_data"] = token + " " if kind == "input" else token
                else:
                    for key in ("url", "copy_text", "disabled", "switch_inline_query", "switch_inline_query_current_chat", "user_id", "web_app"):
                        if key in spec:
                            button[key] = spec[key]
                    if not set(button) - {"text", "style"}:
                        button["disabled"] = True
                current.append(button)
            if current:
                rows.append(current)
        text = f"{screen.notice}\n\n{screen.text}" if screen.notice else screen.text

        def swap(match: "re.Match[str]") -> str:
            raw = match.group(2)
            try:
                spec = cast('dict[str, Any]', json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))))
            except Exception as exc:
                raise ScreenError("malformed in-text button") from exc
            if spec.get("k") not in {"go", "input"}:
                raise ScreenError("malformed in-text button")
            token = self._action(form, spec, actions, gen)
            target = f'query="{token} "' if spec.get("k") == "input" else f'data="{token}"'
            return f"<tg-button{match.group(1)} {target}{match.group(3)}>"

        text = _tap.sub(swap, text)
        screen.rich = bool(screen.rich or _rich.search(text))
        return text, rows, actions, gen

    def _commit(self, form: Form, text: str, actions: list[dict[str, Any]], gen: int, screen: Screen) -> None:
        form.text, form.actions, form.gen, form.rich = text, actions, gen, bool(screen.rich)
        form.touched = time.time()
        if screen.ttl is not None:
            form.ttl = float(screen.ttl)
        self._save(form)
        self._target(form, any(action.get("k") == "input" for action in actions))

    def _target(self, form: Form, enabled: bool) -> None:
        if self._targets is None or form.bot or form.message_id is None:
            return
        key = (str(form.chat), int(form.message_id))
        if enabled:
            self._targets.add(key)
        else:
            self._targets.discard(key)

    def _reply_targets(self) -> set[tuple[str, int]]:
        if self._targets is None:
            rows = self._ensure().execute("SELECT chat, message_id FROM screen_forms WHERE bot = 0 AND message_id IS NOT NULL AND actions LIKE '%\"k\": \"input\"%' AND touched + ttl >= ?", (time.time(),)).fetchall()
            self._targets = {(str(row[0]), int(row[1])) for row in rows}
        return self._targets

    def _actor_of(self, source: Any) -> int:
        for value in (getattr(source, "_hotaru_actor_id", None), getattr(source, "from_id", None)):
            if isinstance(value, int) and value > 0:
                return value
        return int(getattr(getattr(self.runtime, "kernel", None), "owner_id", None) or 0)

    def _command_of(self, source: Any) -> Optional[str]:
        command = getattr(source, "_hotaru_command", None)
        if isinstance(command, str):
            return command
        kernel = getattr(self.runtime, "kernel", None)
        return kernel.command_name(getattr(source, "text", "") or "") if kernel is not None else None

    def _bot_app(self) -> Any:
        app = getattr(getattr(self.runtime, "inline", None), "bot_app", None)
        if app is None:
            raise ScreenError("inline bot is not ready")
        return app

    async def _rich(self, text: str) -> dict[str, Any]:
        branding = getattr(self.runtime, "branding", None)
        if branding is not None:
            return await branding.rich(text)
        return {"_": "inputRichMessageHTML", **to_rich(text)}

    async def show(self, ctx: Any, screen: Screen, *, reply_to: Optional[int] = None, fallback: bool = True, ttl: Optional[float] = None) -> Optional[Form]:
        source = getattr(ctx, "_delivery_source", None) or getattr(ctx, "message", None)
        module_id = str(getattr(ctx, "module_id", "") or "")
        if source is None or not module_id:
            raise ScreenError("screen has no source message")
        return await self.open(module_id, source, screen, reply_to=reply_to, respond=ctx.respond if fallback else None, ttl=ttl)

    async def open(self, module_id: str, source: Any, screen: Screen, *, reply_to: Optional[int] = None, respond: Optional[Callable[..., Any]] = None, ttl: Optional[float] = None) -> Optional[Form]:
        runtime = self.runtime
        if getattr(source, "src", None) == "bot":
            return await self.send(module_id, getattr(source, "chat_id", None), screen, actor=self._actor_of(source), command=self._command_of(source), ttl=ttl)
        form = self._create(module_id, self._actor_of(source), self._command_of(source), getattr(source, "chat_id", None), bot=False, ttl=float(ttl or screen.ttl or _ttl))
        text, rows, actions, gen = self._compile(form, screen)
        try:
            sent, nonce = await runtime.deliver_inline(source, text, rows, rich=screen.rich, reply_to=reply_to)
        except Exception as exc:
            self.drop(form)
            if respond is None:
                raise
            log.warning("screen delivery failed: %s", type(exc).__name__)
            if runtime.observatory is not None:
                runtime.observatory.emit("screens", "delivery_failed", module=module_id, error=type(exc).__name__, detail=str(exc)[:200])
            hint = self._t("ui.inline_unavailable", "<i>Inline menu is unavailable here, showing plain text.</i>")
            await respond(f"{rich_to_plain(text) if screen.rich else text}\n\n{hint}", parse_mode="HTML")
            return None
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
            if repaint and form.gen == gen:
                # custom emoji in a fresh inline result render only after an edit
                try:
                    await self._edit(form, form.text, rows, form.rich)
                except Exception as exc:
                    log.debug("screen repaint failed: %s", type(exc).__name__)

    async def send(self, module_id: str, chat_id: Any, screen: Screen, *, actor: Optional[int] = None, command: Optional[str] = None, ttl: Optional[float] = None) -> Form:
        app = self._bot_app()
        owner = getattr(getattr(self.runtime, "kernel", None), "owner_id", None)
        form = self._create(module_id, int(actor or owner or 0), command, chat_id, bot=True, ttl=float(ttl or screen.ttl or _ttl))
        text, rows, actions, gen = self._compile(form, screen)
        data: dict[str, Any] = {"peer": chat_id, "random_id": secrets.randbits(63), "no_webpage": True}
        if screen.rich:
            data.update(message="", rich_message=await self._rich(text))
        else:
            plain, entities = to_entities(text)
            data["message"] = plain
            if entities:
                data["entities"] = entities
        markup = kbd_to_tl({"inline_keyboard": rows})
        if markup is not None and markup.get("rows"):
            data["reply_markup"] = markup
        try:
            with trusted_scope():
                try:
                    result = await app.mt_messages_send_message(**data)
                except Exception as exc:
                    reopen = getattr(getattr(self.runtime, "inline", None), "reopen_owner_chat", None)
                    blocked = any(item in str(exc).lower() for item in ("chat not found", "blocked", "peer_id_invalid", "input_user_deactivated"))
                    if not blocked or not callable(reopen) or not await cast('Awaitable[bool]', reopen(chat_id)):
                        raise
                    result = await app.mt_messages_send_message(**{**data, "random_id": secrets.randbits(63)})
        except Exception:
            self.drop(form)
            raise
        sent = extract_sent_message(result)
        form.message_id = sent.get("id") if isinstance(sent, dict) and isinstance(sent.get("id"), int) else None
        self._commit(form, text, actions, gen, screen)
        return form

    async def render(self, form: Form, screen: Screen) -> None:
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
        plain = ""
        if rich:
            data["rich_message"] = await self._rich(text)
        else:
            plain, entities = to_entities(text)
            if entities:
                data["entities"] = entities
        app = self._bot_app()
        for attempt in range(2):
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
        except Exception:
            try:
                await self._edit(form, form.text, [], form.rich)
            except Exception:
                pass
        self.drop(form)

    def lock(self, form: Form) -> asyncio.Lock:
        lock = self._locks.get(form.id)
        if lock is None:
            if len(self._locks) > 1024:
                for key in [key for key, item in self._locks.items() if not item.locked()]:
                    self._locks.pop(key, None)
            lock = self._locks[form.id] = asyncio.Lock()
        return lock

    def _allowed(self, form: Form, actor: Any, action: dict[str, Any]) -> bool:
        runtime = self.runtime
        if not isinstance(actor, int) or actor <= 0:
            return False
        if runtime.access is not None and runtime.access.is_owner(actor):
            return True
        target = str(action.get("m") or form.module)
        if actor != form.actor or target != form.module or not form.command or runtime.kernel is None:
            return False
        spec = runtime.kernel.registry.resolve_name(form.command)
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
        denied = self._t("ui.denied", "This button is not for you.")
        decoded = self.decode(getattr(callback, "data", None))
        form = self.load(decoded[0]) if decoded is not None else None
        if decoded is None or form is None or form.expired:
            await self._answer(callback, expired, alert=True)
            if form is not None:
                await self._strip(form, callback)
            return
        _, gen, index = decoded
        async with self.lock(form):
            form = self.load(form.id) or form
            if form.gen != gen or index >= len(form.actions):
                await self._answer(callback, self._t("ui.stale", "The menu has changed, try again."))
                return
            inline_id: Any = getattr(callback, "inline_message_id", None)
            if inline_id is None and isinstance(getattr(callback, "msg_id", None), dict):
                inline_id = callback.msg_id
            if inline_id is not None:
                if form.inline_id is None:
                    form.inline_id = cast('dict[str, Any]', inline_id)
                elif _inline_key(form.inline_id) != _inline_key(inline_id):
                    await self._answer(callback, denied, alert=True)
                    return
            elif form.bot and (str(getattr(callback, "chat_id", None)) != str(form.chat) or getattr(callback, "msg_id", None) != form.message_id):
                await self._answer(callback, denied, alert=True)
                return
            action = form.actions[index]
            actor = getattr(callback, "from_id", None)
            if not self._allowed(form, actor, action):
                await self._answer(callback, denied, alert=True)
                return
            security = getattr(self.runtime, "security", None)
            if security is not None:
                from .security import AccessVerdict
                if security.check(callback, transport="mt", module_id=form.module, authorized=True) is not AccessVerdict.ALLOW:
                    await self._answer(callback, self._t("ui.slow_down", "Too fast, wait a second."))
                    return
            if action.get("k") == "close":
                await self._answer(callback)
                await self._close(form)
                return
            nav = Nav(self, form, action, call=callback, actor=cast(int, actor))
            try:
                result = await self._invoke(form, action, callback, nav)
            except ScreenError:
                await nav.answer(expired, alert=True)
                return
            except Exception as exc:
                self._report(form, action, exc)
                await nav.answer(self._t("ui.failed", "Something went wrong: {error}", error=type(exc).__name__), alert=True)
                return
            await self._finish(form, action, nav, result)

    async def _finish(self, form: Form, action: dict[str, Any], nav: Nav, result: Any) -> None:
        # redraw before answering, so the spinner lasts until the menu changes
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
        except Exception:
            pass

    async def _strip(self, form: Form, callback: Any) -> None:
        if form.inline_id is None and isinstance(getattr(callback, "msg_id", None), dict):
            form.inline_id = callback.msg_id
        try:
            await self._edit(form, form.text, [], form.rich)
        except Exception:
            pass
        self.drop(form)

    def _report(self, form: Form, action: dict[str, Any], exc: BaseException) -> None:
        log.error("screen handler %s.%s failed: %s", action.get("m") or form.module, action.get("f"), type(exc).__name__)
        if self.runtime.observatory is not None:
            self.runtime.observatory.emit("screens", "handler_failed", level="error", module=form.module, handler=str(action.get("f")), error=type(exc).__name__, detail=str(exc)[:240], tb=traceback.format_exc()[-4000:])

    def _input_action(self, token: str) -> Optional[tuple[Form, dict[str, Any]]]:
        decoded = self.decode(token)
        form = self.load(decoded[0]) if decoded is not None else None
        if decoded is None or form is None or form.expired or form.gen != decoded[1] or decoded[2] >= len(form.actions):
            return None
        action = form.actions[decoded[2]]
        return (form, action) if action.get("k") == "input" else None

    async def input_query(self, query: Any, text: str) -> None:
        from goygram.types import InlineObj
        from relay.inline_tl import answer_tl

        token, _, value = text.partition(" ")
        value = value.strip()
        found = self._input_action(token)
        results: list[dict[str, Any]] = []
        if value and found is not None and self._allowed(found[0], getattr(query, "from_id", None), found[1]):
            if found[1].get("s"):
                title = self._t("ui.input_send_secret", "Send ({count} characters)", count=len(value))
            else:
                title = self._t("ui.input_send", "Send: {value}", value=_clip(value, 60))
            results.append(InlineObj.article(secrets.token_urlsafe(8), title, "🔄", description=str(found[1].get("p") or ""), parse_mode="HTML"))
        await answer_tl(query, results=results, cache_time=0, is_personal=True)

    async def input_chosen(self, chosen: Any, text: str) -> None:
        token, _, value = text.partition(" ")
        found = self._input_action(token)
        actor = getattr(chosen, "from_id", None)
        await self._drop_transfer(found[0] if found is not None else None, getattr(chosen, "msg_id", None))
        if found is None or not self._allowed(found[0], actor, found[1]):
            return
        security = getattr(self.runtime, "security", None)
        if security is not None:
            from .security import AccessVerdict
            if security.check(chosen, transport="inline", module_id=found[0].module, authorized=True) is not AccessVerdict.ALLOW:
                return
        await self._submit(found[0], found[1], chosen, value.strip(), cast(int, actor))

    async def _submit(self, form: Form, action: dict[str, Any], event: Any, value: str, actor: int) -> None:
        async with self.lock(form):
            try:
                result = await self._invoke(form, action, event, Nav(self, form, action, value=value, actor=actor))
                if isinstance(result, Screen) and self.load(form.id) is not None:
                    await self.render(form, result)
            except Exception as exc:
                self._report(form, action, exc)

    async def _drop_transfer(self, form: Optional[Form], inline_id: Any) -> None:
        try:
            await self.runtime.drop_transfer(SimpleNamespace(chat_id=form.chat if form is not None else None), inline_id)
        except Exception:
            pass

    async def on_message(self, message: Any) -> bool:
        # replies to a menu feed its input button: long and multi-line values
        from .response import reply_message_id

        text = getattr(message, "text", None)
        target = reply_message_id(message)
        chat = getattr(message, "chat_id", None)
        if not isinstance(text, str) or not text or target is None or chat is None or self._db is None:
            return False
        if (str(chat), int(target)) not in self._reply_targets():
            return False
        row = self._ensure().execute(f"SELECT {self._columns} FROM screen_forms WHERE chat = ? AND message_id = ? AND bot = 0", (str(chat), target)).fetchone()
        form = self._row(row) if row is not None else None
        if form is None or form.expired:
            return False
        action = next((item for item in form.actions if item.get("k") == "input"), None)
        actor = getattr(message, "from_id", None)
        if action is None or not self._allowed(form, actor, action):
            return False
        try:
            with trusted_scope():
                await delete_chat_msg(self.runtime.app, chat, int(getattr(message, "id", 0) or getattr(message, "msg_id", 0)))
        except Exception:
            pass
        await self._submit(form, action, message, text.strip(), cast(int, actor))
        return True
