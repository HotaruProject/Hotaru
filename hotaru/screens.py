"""Screens: restart-safe inline menus.

A screen is plain data (text + button rows) returned by a module-level
function.  Buttons do not carry closures: each one points at a function
*name* inside a module namespace plus JSON-safe arguments, stored per form in
SQLite.  The callback data itself is a struct-packed, HMAC-signed reference

    "~" + b64url(pack("<BIHB", version, form, generation, button) + tag[:8])

which is 23 ASCII characters, far below Telegram's 64-byte limit.  Menus
therefore survive restarts, expire in one place, and every press is answered.

Handlers look like ``async def view(ctx, nav) -> Screen | str | None``: a
Screen redraws the message, a string becomes a toast, None only acknowledges.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import secrets
import struct
import time
import traceback
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, cast

from goygram.errors import FloodWaitError, MessageIdInvalidError, MessageNotModifiedError
from goygram.sugar import extract_sent_message
from relay.emoji import has_emoji, to_entities, to_rich
from relay.firewall import module_scope, trusted_scope
from relay.rpc import delete_chat_msg

from .markup import COPY_LIMIT, kbd_to_tl

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger(__name__)

MARK = "~"
VERSION = 1
_PACK = struct.Struct("<BIHB")
_TAG = 8
TOAST_LIMIT = 200
DEFAULT_TTL = 7 * 86400.0
KERNEL = "@kernel"
INPUT_ARTICLE = "🔄"
_CACHE = 256


class ScreenError(RuntimeError):
    pass


@dataclass
class Screen:
    text: str
    rows: List[List[Dict[str, Any]]] = field(default_factory=list)
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
    inline_id: Optional[Dict[str, Any]]
    bot: bool
    gen: int
    actions: List[Dict[str, Any]]
    text: str
    rich: bool
    touched: float
    ttl: float
    language: Optional[str] = None

    @property
    def expired(self) -> bool:
        return self.touched + self.ttl < time.time()


def _clip(text: Optional[str], limit: int = TOAST_LIMIT) -> Optional[str]:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _inline_key(value: Any) -> Any:
    if isinstance(value, dict):
        data = cast(Dict[str, Any], value)
        return tuple(data.get(key) for key in ("dc_id", "id", "owner_id", "access_hash"))
    return value


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def module_of(fn: Callable[..., Any]) -> str:
    name = str(getattr(fn, "__globals__", {}).get("__name__", ""))
    if name.startswith("hotaru_module_"):
        return name[len("hotaru_module_"):]
    raise ScreenError(f"screen handler must be a module-level function: {getattr(fn, '__qualname__', fn)!r}")


class Nav:
    """What a screen handler receives next to its module context."""

    def __init__(self, engine: "ScreenEngine", form: Form, action: Dict[str, Any], *, call: Any = None, value: Optional[str] = None, actor: Optional[int] = None) -> None:
        self.engine = engine
        self.form = form
        self.call = call
        self.value = value
        self.actor = actor if actor is not None else form.actor
        self.args: Dict[str, Any] = dict(action.get("a") or {})
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

    async def alert(self, text: str) -> None:
        await self.answer(text, alert=True)

    async def close(self) -> None:
        await self.answer()
        await self.engine.close(self.form)

    def ref(self, *, follow: bool = False) -> "ScreenRef":
        """A handle for redrawing this form later, from a background task.

        By default it is pinned to the screen this press is about to render:
        once the user presses anything else the handle goes stale and its
        edits are skipped.  ``follow=True`` ignores navigation (for flows
        that own the whole form, such as a login wizard).
        """
        return ScreenRef(self.engine, self.form.id, None if follow else (self.form.gen + 1) & 0xFFFF)


class ScreenRef:
    def __init__(self, engine: "ScreenEngine", form_id: int, gen: Optional[int]) -> None:
        self.engine = engine
        self.form_id = form_id
        self.gen = gen

    @property
    def alive(self) -> bool:
        form = self.engine.load(self.form_id)
        return form is not None and not form.expired and (self.gen is None or form.gen == self.gen)

    async def edit(self, screen: Screen) -> bool:
        form = self.engine.load(self.form_id)
        if form is None:
            return False
        async with self.engine._lock(form):
            form = self.engine.load(self.form_id)
            if form is None or (self.gen is not None and form.gen != self.gen):
                return False
            await self.engine._render(form, screen)
            if self.gen is not None:
                self.gen = form.gen
            return True

    async def close(self) -> None:
        form = self.engine.load(self.form_id)
        if form is not None:
            await self.engine.close(form)


class Kit:
    """Button and layout helpers, exposed to modules as ``ctx.screens``."""

    def __init__(self, engine: Optional["ScreenEngine"], ctx: Any) -> None:
        self.engine = engine
        self.ctx = ctx

    def _t(self, key: str, default: str, **params: Any) -> str:
        translate = getattr(self.ctx, "t", None)
        return str(translate(key, default, **params)) if callable(translate) else default.format(**params)

    @staticmethod
    def screen(text: str, rows: Optional[List[List[Dict[str, Any]]]] = None, **options: Any) -> Screen:
        return Screen(text, [row for row in (rows or []) if row], **options)

    @staticmethod
    def _target(fn: Any) -> Dict[str, str]:
        if isinstance(fn, str):
            return {"f": fn}
        if not callable(fn):
            raise ScreenError("screen handler is not callable")
        name = getattr(fn, "__name__", "")
        namespace = getattr(fn, "__globals__", {})
        if namespace.get(name) is not fn:
            raise ScreenError(f"screen handler must be a module-level function: {name}")
        return {"m": module_of(fn), "f": name}

    def button(self, text: str, fn: Any, *, style: Optional[str] = None, **args: Any) -> Dict[str, Any]:
        return self._go(text, fn, style, args)

    def _go(self, text: str, fn: Any, style: Optional[str], args: Dict[str, Any]) -> Dict[str, Any]:
        item: Dict[str, Any] = {"text": text, "k": "go", **self._target(fn), "a": _json_safe(args)}
        if style:
            item["style"] = style
        return item

    go = button

    def primary(self, text: str, fn: Any, **args: Any) -> Dict[str, Any]:
        return self.button(text, fn, style="primary", **args)

    def success(self, text: str, fn: Any, **args: Any) -> Dict[str, Any]:
        return self.button(text, fn, style="success", **args)

    def danger(self, text: str, fn: Any, **args: Any) -> Dict[str, Any]:
        return self.button(text, fn, style="danger", **args)

    def input(self, text: str, fn: Any, placeholder: str = "", *, style: Optional[str] = None, secret: bool = False, **args: Any) -> Dict[str, Any]:
        """A button that asks for text; ``secret`` keeps the typed value out of the result title."""
        item: Dict[str, Any] = {"text": text, "k": "input", "p": placeholder, **self._target(fn), "a": _json_safe(args)}
        if style:
            item["style"] = style
        if secret:
            item["s"] = True
        return item

    @staticmethod
    def url(text: str, url: str) -> Dict[str, Any]:
        return {"text": text, "url": url}

    @staticmethod
    def copy(text: str, value: str) -> Dict[str, Any]:
        return {"text": text, "copy_text": str(value)[:COPY_LIMIT] or " "}

    @staticmethod
    def label(text: str) -> Dict[str, Any]:
        return {"text": text, "disabled": True}

    @staticmethod
    def link(text: str, module_id: str, fn: str, *, style: Optional[str] = None, **args: Any) -> Dict[str, Any]:
        """A button into another module's screen; only owners may follow it."""
        item: Dict[str, Any] = {"text": text, "k": "go", "m": module_id, "f": fn, "a": _json_safe(args)}
        if style:
            item["style"] = style
        return item

    def close(self, text: Optional[str] = None) -> Dict[str, Any]:
        return {"text": text or self._t("ui.close", "✕ Close"), "k": "close"}

    def back(self, fn: Any, text: Optional[str] = None, **args: Any) -> Dict[str, Any]:
        return self.button(text or self._t("ui.back", "‹ Back"), fn, **args)

    def footer(self, back: Any = None, **args: Any) -> List[Dict[str, Any]]:
        row: List[Dict[str, Any]] = []
        if back is not None:
            row.append(self.back(back, **args))
        row.append(self.close())
        return row

    @staticmethod
    def grid(items: List[Dict[str, Any]], columns: int = 2) -> List[List[Dict[str, Any]]]:
        columns = max(1, min(8, columns))
        return [items[index:index + columns] for index in range(0, len(items), columns)]

    def pager(self, fn: Any, page: int, pages: int, **args: Any) -> List[Dict[str, Any]]:
        """``‹  2 / 5  ›`` with fixed positions, so the row never jumps."""
        if pages <= 1:
            return []
        left = self._go("‹", fn, None, {**args, "page": page - 1}) if page > 0 else self.label("·")
        right = self._go("›", fn, None, {**args, "page": page + 1}) if page + 1 < pages else self.label("·")
        return [left, self.label(f"{page + 1} / {pages}"), right]

    @staticmethod
    def page(items: List[Any], page: int, size: int) -> tuple[List[Any], int, int]:
        pages = max(1, (len(items) + size - 1) // size)
        page = max(0, min(int(page), pages - 1))
        return items[page * size:(page + 1) * size], page, pages

    @staticmethod
    def radio(text: str, selected: bool) -> str:
        return ("● " if selected else "○ ") + text

    @staticmethod
    def check(text: str, selected: bool) -> str:
        return ("✓ " if selected else "") + text

    async def show(self, screen: Screen, **options: Any) -> Any:
        if self.engine is None:
            raise ScreenError("screens are unavailable")
        return await self.engine.show(self.ctx, screen, **options)

    async def send(self, chat_id: Any, screen: Screen, **options: Any) -> Any:
        if self.engine is None:
            raise ScreenError("screens are unavailable")
        module_id = str(getattr(self.ctx, "module_id", "") or "")
        return await self.engine.send(module_id, chat_id, screen, **options)


class ScreenEngine:
    def __init__(self, runtime: "Runtime", secret: str) -> None:
        self.runtime = runtime
        self._key = hashlib.sha256(("hotaru-screens:" + secret).encode()).digest()
        self._kernel: Dict[str, Callable[..., Any]] = {}
        self._cache: Dict[int, Form] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        self._targets: Optional[set[tuple[str, int]]] = None
        self._ready = False

    # storage -----------------------------------------------------------

    @property
    def _db(self) -> Any:
        state = getattr(self.runtime, "state", None)
        return getattr(state, "connection", None)

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
            db.commit()
            self._ready = True
        return db

    def _row(self, row: Any) -> Form:
        chat_raw = row[4]
        chat: Any = int(chat_raw) if isinstance(chat_raw, str) and chat_raw.lstrip("-").isdigit() else chat_raw
        return Form(
            id=int(row[0]), module=str(row[1]), actor=int(row[2]), command=row[3], chat=chat,
            message_id=row[5], inline_id=json.loads(row[6]) if row[6] else None, bot=bool(row[7]),
            gen=int(row[8]), actions=json.loads(row[9] or "[]"), text=str(row[10] or ""), rich=bool(row[11]),
            touched=float(row[12]), ttl=float(row[13]), language=row[14],
        )

    _COLUMNS = "id,module,actor,command,chat,message_id,inline_id,bot,gen,actions,text,rich,touched,ttl,language"

    def load(self, form_id: int) -> Optional[Form]:
        form = self._cache.get(form_id)
        if form is not None:
            return form
        row = self._ensure().execute(f"SELECT {self._COLUMNS} FROM screen_forms WHERE id = ?", (form_id,)).fetchone()
        if row is None:
            return None
        form = self._row(row)
        self._remember(form)
        return form

    def _remember(self, form: Form) -> None:
        self._cache[form.id] = form
        while len(self._cache) > _CACHE:
            self._cache.pop(next(iter(self._cache)))

    def _save(self, form: Form) -> None:
        self._ensure().execute(
            "UPDATE screen_forms SET chat=?, message_id=?, inline_id=?, gen=?, actions=?, text=?, rich=?, touched=?, ttl=?, language=? WHERE id=?",
            (None if form.chat is None else str(form.chat), form.message_id, json.dumps(form.inline_id, default=str) if form.inline_id else None, form.gen,
             json.dumps(form.actions, ensure_ascii=False), form.text, int(form.rich), form.touched, form.ttl, form.language, form.id),
        )
        self._ensure().commit()
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
        self._ensure().execute("DELETE FROM screen_forms WHERE id = ?", (form.id,))
        self._ensure().commit()
        self._cache.pop(form.id, None)
        self._locks.pop(form.id, None)

    def drop_module(self, module_id: str) -> int:
        cursor = self._ensure().execute("DELETE FROM screen_forms WHERE module = ?", (module_id,))
        self._ensure().commit()
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

    # tokens ------------------------------------------------------------

    def encode(self, form_id: int, gen: int, index: int) -> str:
        body = _PACK.pack(VERSION, form_id, gen & 0xFFFF, index)
        tag = hmac.new(self._key, body, hashlib.sha256).digest()[:_TAG]
        return MARK + base64.urlsafe_b64encode(body + tag).decode("ascii").rstrip("=")

    def decode(self, token: Any) -> Optional[tuple[int, int, int]]:
        if isinstance(token, (bytes, bytearray)):
            token = bytes(token).decode("ascii", "replace")
        if not isinstance(token, str) or not token.startswith(MARK) or len(token) > 40:
            return None
        try:
            raw = base64.urlsafe_b64decode(token[1:] + "=" * (-len(token[1:]) % 4))
        except Exception:
            return None
        if len(raw) != _PACK.size + _TAG:
            return None
        body, tag = raw[:_PACK.size], raw[_PACK.size:]
        if not hmac.compare_digest(tag, hmac.new(self._key, body, hashlib.sha256).digest()[:_TAG]):
            return None
        version, form_id, gen, index = _PACK.unpack(body)
        if version != VERSION:
            return None
        return form_id, gen, index

    @staticmethod
    def owns(data: Any) -> bool:
        if isinstance(data, (bytes, bytearray)):
            return bytes(data[:1]) == MARK.encode()
        return isinstance(data, str) and data.startswith(MARK)

    # compile -----------------------------------------------------------

    def register(self, name: str, handler: Callable[..., Any]) -> None:
        """Kernel-level handler, addressed as ``{"m": "@kernel", "f": name}``."""
        self._kernel[name] = handler

    def ref(self, form: Form, *, follow: bool = True) -> ScreenRef:
        return ScreenRef(self, form.id, None if follow else form.gen)

    def kernel_button(self, text: str, name: str, *, style: Optional[str] = None, **args: Any) -> Dict[str, Any]:
        item: Dict[str, Any] = {"text": text, "k": "go", "m": KERNEL, "f": name, "a": _json_safe(args)}
        if style:
            item["style"] = style
        return item

    def _compile(self, form: Form, screen: Screen) -> tuple[str, List[List[Dict[str, Any]]], List[Dict[str, Any]], int]:
        gen = (form.gen + 1) & 0xFFFF
        actions: List[Dict[str, Any]] = []
        rows: List[List[Dict[str, Any]]] = []
        for row in screen.rows:
            current: List[Dict[str, Any]] = []
            for spec in row:
                kind = spec.get("k")
                button: Dict[str, Any] = {"text": spec.get("text", "")}
                if spec.get("style"):
                    button["style"] = spec["style"]
                if spec.get("icon_custom_emoji_id"):
                    button["icon_custom_emoji_id"] = spec["icon_custom_emoji_id"]
                if kind in {"go", "input", "close"}:
                    if len(actions) > 255:
                        raise ScreenError("a screen can hold at most 256 actions")
                    action: Dict[str, Any] = {"k": kind}
                    if kind != "close":
                        action.update(m=spec.get("m") or form.module, f=spec["f"], a=spec.get("a") or {})
                    if kind == "input":
                        action["p"] = str(spec.get("p") or "")
                        if spec.get("s"):
                            action["s"] = True
                    token = self.encode(form.id, gen, len(actions))
                    actions.append(action)
                    if kind == "input":
                        button["switch_inline_query_current_chat"] = token + " "
                    else:
                        button["callback_data"] = token
                else:
                    for key in ("url", "copy_text", "disabled", "switch_inline_query", "switch_inline_query_current_chat", "user_id", "web_app"):
                        if key in spec:
                            button[key] = spec[key]
                    if len(button) == 1 or (len(button) == 2 and "style" in button):
                        button["disabled"] = True
                current.append(button)
            if current:
                rows.append(current)
        text = screen.text
        if screen.notice:
            text = f"{screen.notice}\n\n{text}"
        return text, rows, actions, gen

    def _commit(self, form: Form, text: str, actions: List[Dict[str, Any]], gen: int, screen: Screen) -> None:
        form.text = text
        form.actions = actions
        form.gen = gen
        form.rich = bool(screen.rich)
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

    # delivery ----------------------------------------------------------

    def _actor_of(self, source: Any) -> int:
        for value in (getattr(source, "_hotaru_actor_id", None), getattr(source, "from_id", None)):
            if isinstance(value, int) and value > 0:
                return value
        owner = getattr(getattr(self.runtime, "kernel", None), "owner_id", None)
        return int(owner or 0)

    async def show(self, ctx: Any, screen: Screen, *, reply_to: Optional[int] = None, fallback: bool = True, ttl: Optional[float] = None) -> Optional[Form]:
        """Deliver a screen in answer to a command, through the inline bot."""
        source = getattr(ctx, "_delivery_source", None) or getattr(ctx, "message", None)
        module_id = str(getattr(ctx, "module_id", "") or "")
        if source is None or not module_id:
            raise ScreenError("screen has no source message")
        return await self.open(module_id, source, screen, reply_to=reply_to, respond=ctx.respond if fallback else None, ttl=ttl)

    async def open(self, module_id: str, source: Any, screen: Screen, *, reply_to: Optional[int] = None, respond: Optional[Callable[..., Any]] = None, ttl: Optional[float] = None) -> Optional[Form]:
        """Deliver a screen into ``source.chat_id``; ``source`` stands for the command.

        ``source`` needs ``chat_id`` and ``from_id`` (the actor); ``id`` is the
        command message, if any, and ``_hotaru_command`` names the command
        whose permissions guard the buttons.  Without ``respond`` a delivery
        failure is raised instead of falling back to plain text.
        """
        runtime = self.runtime
        if getattr(source, "src", None) == "bot":
            chat = getattr(source, "chat_id", None)
            return await self.send(module_id, chat, screen, actor=self._actor_of(source), command=self._command_of(source), ttl=ttl)
        command = self._command_of(source)
        actor = self._actor_of(source)
        chat = getattr(source, "chat_id", None)
        form = self._create(module_id, actor, command, chat, bot=False, ttl=float(ttl or screen.ttl or DEFAULT_TTL))
        text, rows, actions, gen = self._compile(form, screen)
        try:
            sent, inline_id = await runtime.deliver_inline(source, text, rows, rich=screen.rich, reply_to=reply_to)
        except Exception as exc:
            self.drop(form)
            if respond is None:
                raise
            log.warning("screen delivery failed: %s", type(exc).__name__)
            observatory = runtime.observatory
            if observatory is not None:
                observatory.emit("screens", "delivery_failed", module=module_id, error=type(exc).__name__, detail=str(exc)[:200])
            hint = self._t("ui.inline_unavailable", "<i>Inline menu is unavailable here, showing plain text.</i>")
            await respond(f"{text}\n\n{hint}", parse_mode="HTML")
            return None
        form.message_id = sent.get("id") if isinstance(sent, dict) and isinstance(sent.get("id"), int) else None
        form.inline_id = inline_id if isinstance(inline_id, dict) else None
        self._commit(form, text, actions, gen, screen)
        if form.inline_id is not None and has_emoji(text):
            asyncio.get_running_loop().create_task(self._repaint(form, gen, text, rows, screen.rich))
        return form

    async def _repaint(self, form: Form, gen: int, text: str, rows: List[List[Dict[str, Any]]], rich: bool) -> None:
        """Custom emoji in a fresh inline result only render after one edit."""
        try:
            async with self._lock(form):
                current = self.load(form.id)
                if current is not None and current.gen == gen:
                    await self._edit_message(form, text, rows, rich)
        except Exception as exc:
            log.debug("screen repaint failed: %s", type(exc).__name__)

    async def send(self, module_id: str, chat_id: Any, screen: Screen, *, actor: Optional[int] = None, command: Optional[str] = None, ttl: Optional[float] = None) -> Form:
        """Send a screen from the inline bot itself (e.g. to the owner's PM)."""
        bot_app = self._bot_app()
        owner = getattr(getattr(self.runtime, "kernel", None), "owner_id", None)
        form = self._create(module_id, int(actor or owner or 0), command, chat_id, bot=True, ttl=float(ttl or screen.ttl or DEFAULT_TTL))
        text, rows, actions, gen = self._compile(form, screen)
        plain, entities = to_entities(text)
        data: Dict[str, Any] = {"peer": chat_id, "message": plain, "random_id": secrets.randbits(63), "no_webpage": True}
        if entities:
            data["entities"] = entities
        markup = kbd_to_tl({"inline_keyboard": rows})
        if markup is not None and markup.get("rows"):
            data["reply_markup"] = markup
        try:
            with trusted_scope():
                try:
                    result = await bot_app.mt_messages_send_message(**data)
                except Exception as exc:
                    reopen = getattr(getattr(self.runtime, "inline", None), "reopen_owner_chat", None)
                    blocked = any(item in str(exc).lower() for item in ("chat not found", "blocked", "peer_id_invalid", "input_user_deactivated"))
                    if not blocked or not callable(reopen) or not await cast("Awaitable[bool]", reopen(chat_id)):
                        raise
                    result = await bot_app.mt_messages_send_message(**{**data, "random_id": secrets.randbits(63)})
        except Exception:
            self.drop(form)
            raise
        sent = extract_sent_message(result)
        form.message_id = sent.get("id") if isinstance(sent, dict) and isinstance(sent.get("id"), int) else None
        self._commit(form, text, actions, gen, screen)
        return form

    def _bot_app(self) -> Any:
        inline = getattr(self.runtime, "inline", None)
        bot_app = getattr(inline, "bot_app", None)
        if bot_app is None:
            raise ScreenError("inline bot is not ready")
        return bot_app

    def _command_of(self, source: Any) -> Optional[str]:
        command = getattr(source, "_hotaru_command", None)
        if isinstance(command, str):
            return command
        kernel = getattr(self.runtime, "kernel", None)
        return kernel.command_name(getattr(source, "text", "") or "") if kernel is not None else None

    async def edit(self, form: Form, screen: Screen) -> None:
        """Redraw a form outside of a button press (progress, timers...)."""
        async with self._lock(form):
            await self._render(form, screen)

    async def _render(self, form: Form, screen: Screen) -> None:
        text, rows, actions, gen = self._compile(form, screen)
        await self._edit_message(form, text, rows, screen.rich)
        self._commit(form, text, actions, gen, screen)

    async def _edit_message(self, form: Form, text: str, rows: List[List[Dict[str, Any]]], rich: bool) -> None:
        from .callbacks import CallbackContext

        markup = kbd_to_tl({"inline_keyboard": rows})
        data: Dict[str, Any] = {"no_webpage": True}
        if markup is not None and markup.get("rows"):
            data["reply_markup"] = markup
        elif form.inline_id is None:
            data["reply_markup"] = {"_": "replyInlineMarkup", "rows": []}
        if rich:
            data["rich_message"] = {"_": "inputRichMessageHTML", **to_rich(text)}
            plain = ""
        else:
            plain, entities = to_entities(text)
            if entities:
                data["entities"] = [entity for entity in entities if int(entity.get("length", 0)) > 0]
        bot_app = self._bot_app()
        for attempt in range(2):
            try:
                if form.inline_id is not None:
                    await CallbackContext.edit_inline(bot_app, form.inline_id, plain, data)
                elif form.bot and form.message_id is not None:
                    with trusted_scope():
                        await bot_app.mt_messages_edit_message(peer=form.chat, id=form.message_id, message=plain, **data)
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
        async with self._lock(form):
            await self._close(form)

    async def _close(self, form: Form) -> None:
        runtime = self.runtime
        deleted = False
        try:
            if form.bot and form.message_id is not None:
                with trusted_scope():
                    await delete_chat_msg(self._bot_app(), form.chat, form.message_id)
                deleted = True
            elif form.message_id is not None and form.chat is not None and getattr(runtime, "app", None) is not None:
                with trusted_scope():
                    await delete_chat_msg(runtime.app, form.chat, form.message_id)
                deleted = True
        except Exception as exc:
            log.debug("screen close failed: %s", type(exc).__name__)
        if not deleted:
            try:
                await self._edit_message(form, form.text, [], form.rich)
            except Exception:
                pass
        self.drop(form)

    def _lock(self, form: Form) -> asyncio.Lock:
        lock = self._locks.get(form.id)
        if lock is None:
            if len(self._locks) > 1024:
                for key in [key for key, item in self._locks.items() if not item.locked()]:
                    self._locks.pop(key, None)
            lock = self._locks[form.id] = asyncio.Lock()
        return lock

    # access ------------------------------------------------------------

    def _allowed(self, form: Form, actor: Any, action: Dict[str, Any]) -> bool:
        runtime = self.runtime
        access = getattr(runtime, "access", None)
        if not isinstance(actor, int) or actor <= 0:
            return False
        if access is not None and access.is_owner(actor):
            return True
        target = str(action.get("m") or form.module)
        if actor != form.actor or target != form.module or target == KERNEL or not form.command:
            return False
        kernel = runtime.kernel
        if kernel is None:
            return False
        spec = kernel.registry.resolve_name(form.command)
        if spec is None or spec.module_id != form.module:
            return False
        return bool(kernel.is_authorized(SimpleNamespace(from_id=actor, chat_id=form.chat), spec))

    def _handler(self, action: Dict[str, Any], form: Form) -> Callable[..., Any]:
        module_id = str(action.get("m") or form.module)
        name = str(action.get("f") or "")
        if module_id == KERNEL:
            handler = self._kernel.get(name)
        else:
            modules = getattr(self.runtime, "modules", None)
            active = modules.get(module_id) if modules is not None else None
            context = getattr(active, "context", None)
            namespace = getattr(context, "namespace", None)
            handler = cast(Dict[str, Any], namespace).get(name) if isinstance(namespace, dict) and not name.startswith("__") else None
        if not callable(handler):
            raise ScreenError(f"screen handler is unavailable: {module_id}.{name}")
        return handler

    async def _invoke(self, form: Form, action: Dict[str, Any], event: Any, nav: Nav) -> Any:
        handler = self._handler(action, form)
        module_id = str(action.get("m") or form.module)
        if module_id == KERNEL:
            result = handler(nav)
        else:
            factory = getattr(self.runtime, "context_factory", None)
            if factory is None:
                raise ScreenError("module context is unavailable")
            ctx = factory.create(module_id, event)
            with module_scope(module_id):
                result = handler(ctx, nav)
        if inspect.isawaitable(result):
            with module_scope(module_id if module_id != KERNEL else ""):
                result = await result
        return result

    # button presses ----------------------------------------------------

    async def dispatch(self, callback: Any) -> None:
        decoded = self.decode(getattr(callback, "data", None))
        if decoded is None:
            await self._answer(callback, self._t("ui.expired", "This menu has expired. Open it again."), alert=True)
            return
        form_id, gen, index = decoded
        form = self.load(form_id)
        if form is None or form.expired:
            await self._answer(callback, self._t("ui.expired", "This menu has expired. Open it again."), alert=True)
            if form is not None:
                await self._strip(form, callback)
            return
        async with self._lock(form):
            form = self.load(form_id) or form
            if form.gen != gen or index >= len(form.actions):
                await self._answer(callback, self._t("ui.stale", "The menu has changed, try again."))
                return
            inline_id: Any = getattr(callback, "inline_message_id", None)
            if inline_id is None:
                raw: Any = getattr(callback, "msg_id", None)
                inline_id = cast(Any, raw) if isinstance(raw, dict) else None
            if inline_id is not None:
                if form.inline_id is None:
                    form.inline_id = cast(Dict[str, Any], inline_id)
                elif _inline_key(form.inline_id) != _inline_key(inline_id):
                    await self._answer(callback, self._t("ui.denied", "This button is not for you."), alert=True)
                    return
            elif form.bot and (str(getattr(callback, "chat_id", None)) != str(form.chat) or getattr(callback, "msg_id", None) != form.message_id):
                await self._answer(callback, self._t("ui.denied", "This button is not for you."), alert=True)
                return
            action = form.actions[index]
            actor = getattr(callback, "from_id", None)
            if not self._allowed(form, actor, action):
                await self._answer(callback, self._t("ui.denied", "This button is not for you."), alert=True)
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
                await nav.answer(self._t("ui.expired", "This menu has expired. Open it again."), alert=True)
                return
            except Exception as exc:
                self._report(form, action, exc)
                await nav.answer(self._t("ui.failed", "Something went wrong: {error}", error=type(exc).__name__), alert=True)
                return
            await self._finish(form, action, nav, result)

    async def _finish(self, form: Form, action: Dict[str, Any], nav: Nav, result: Any) -> None:
        """Redraw first, then answer: the button spinner lasts exactly until the menu has changed."""
        if isinstance(result, Screen):
            if self.load(form.id) is None:
                await nav.answer(result.toast, alert=result.alert)
                return
            try:
                await self._render(form, result)
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
            return
        if isinstance(result, str):
            await nav.answer(result)
            return
        await nav.answer()

    async def _answer(self, callback: Any, text: Optional[str] = None, *, alert: bool = False) -> None:
        try:
            await callback.answer(_clip(text), alert=alert)
        except Exception:
            pass

    async def _strip(self, form: Form, callback: Any) -> None:
        if form.inline_id is None:
            raw = getattr(callback, "msg_id", None)
            form.inline_id = raw if isinstance(raw, dict) else None
        try:
            await self._edit_message(form, form.text, [], form.rich)
        except Exception:
            pass
        self.drop(form)

    def _report(self, form: Form, action: Dict[str, Any], exc: BaseException) -> None:
        log.error("screen handler %s.%s failed: %s", action.get("m") or form.module, action.get("f"), type(exc).__name__)
        observatory = getattr(self.runtime, "observatory", None)
        if observatory is not None:
            observatory.emit("screens", "handler_failed", level="error", module=form.module, handler=str(action.get("f")), error=type(exc).__name__, detail=str(exc)[:240], tb=traceback.format_exc()[-4000:])

    # text input --------------------------------------------------------

    def _input_action(self, token: str) -> Optional[tuple[Form, Dict[str, Any]]]:
        decoded = self.decode(token)
        if decoded is None:
            return None
        form_id, gen, index = decoded
        form = self.load(form_id)
        if form is None or form.expired or form.gen != gen or index >= len(form.actions):
            return None
        action = form.actions[index]
        return (form, action) if action.get("k") == "input" else None

    async def input_query(self, query: Any, text: str) -> None:
        from relay.inline_tl import answer_tl
        from goygram.types import InlineObj

        token, _, value = text.partition(" ")
        found = self._input_action(token)
        results: List[Dict[str, Any]] = []
        if found is not None and self._allowed(found[0], getattr(query, "from_id", None), found[1]):
            value = value.strip()
            if found[1].get("s"):
                title = self._t("ui.input_send_secret", "Send ({count} characters)", count=len(value))
            else:
                title = self._t("ui.input_send", "Send: {value}", value=_clip(value, 60)) if value else str(found[1].get("p") or self._t("ui.input_hint", "Type a value"))
            description = str(found[1].get("p") or "") if value else self._t("ui.input_hint", "Type a value")
            if value:
                results.append(InlineObj.article(secrets.token_urlsafe(8), title, INPUT_ARTICLE, description=description, parse_mode="HTML"))
        await answer_tl(query, results=results, cache_time=0, is_personal=True)

    async def input_chosen(self, chosen: Any, text: str) -> None:
        token, _, value = text.partition(" ")
        found = self._input_action(token)
        transfer = getattr(chosen, "msg_id", None)
        if found is None:
            await self._drop_transfer(None, transfer)
            return
        form, action = found
        actor = getattr(chosen, "from_id", None)
        if not self._allowed(form, actor, action):
            await self._drop_transfer(form, transfer)
            return
        await self._drop_transfer(form, transfer)
        security = getattr(self.runtime, "security", None)
        if security is not None:
            from .security import AccessVerdict
            if security.check(chosen, transport="inline", module_id=form.module, authorized=True) is not AccessVerdict.ALLOW:
                return
        await self._submit(form, action, chosen, value.strip(), cast(int, actor))

    async def _submit(self, form: Form, action: Dict[str, Any], event: Any, value: str, actor: int) -> None:
        async with self._lock(form):
            nav = Nav(self, form, action, value=value, actor=actor)
            try:
                result = await self._invoke(form, action, event, nav)
            except Exception as exc:
                self._report(form, action, exc)
                return
            if isinstance(result, str):
                result = None
            if isinstance(result, Screen) and self.load(form.id) is not None:
                try:
                    await self._render(form, result)
                except Exception as exc:
                    self._report(form, action, exc)

    async def _drop_transfer(self, form: Optional[Form], inline_id: Any) -> None:
        try:
            await self.runtime.drop_transfer(SimpleNamespace(chat_id=form.chat if form is not None else None), inline_id)
        except Exception:
            pass

    async def on_message(self, message: Any) -> bool:
        """A reply to a menu message is taken as input for its first input button.

        This is how values longer than an inline query (about 220 characters),
        or multi-line ones, get into a screen.
        """
        from .response import reply_message_id

        text = getattr(message, "text", None)
        target = reply_message_id(message)
        chat = getattr(message, "chat_id", None)
        if not isinstance(text, str) or not text or target is None or chat is None or self._db is None:
            return False
        if (str(chat), int(target)) not in self._reply_targets():
            return False
        row = self._ensure().execute(f"SELECT {self._COLUMNS} FROM screen_forms WHERE chat = ? AND message_id = ? AND bot = 0", (str(chat), target)).fetchone()
        if row is None:
            return False
        form = self._row(row)
        if form.expired:
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

