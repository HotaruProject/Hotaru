from __future__ import annotations

import asyncio
import logging
import re
import secrets
import socket
import ssl
import struct
import tempfile
import time
import urllib.parse
import zlib
from contextlib import AsyncExitStack, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import hotaru as _hotaru_pkg
except Exception:
    _hotaru_pkg = None

try:
    from relay.firewall import trusted_scope
except Exception:
    trusted_scope = nullcontext

try:
    from relay.inline import BotFatherGuard, InlineBotInfo
except Exception:
    BotFatherGuard = None
    InlineBotInfo = None

try:
    from relay.files import put as _relay_put_file
except Exception:
    _relay_put_file = None

log = logging.getLogger("hotaru.singularity.relay")

BOTFATHER_PEER = "@BotFather"
BOTFATHER_USERNAME = "BotFather"
TME_APP_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/([A-Za-z0-9_]+)/([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
WEBAPP_URL_LINE_RE = re.compile(r"Web\s+App\s+URL:\s*(https?://\S+)", re.IGNORECASE)
BOT_TOKEN_RE = re.compile(r"\b(\d{6,14}:[A-Za-z0-9_-]{30,})\b")
FLOOD_WAIT_RE = re.compile(r"FLOOD_WAIT_(\d+)|wait\s+of\s+(\d+)\s+seconds", re.IGNORECASE)

HOTARU_RAW_DEVELOP_URL = (
    "https://raw.githubusercontent.com/HotaruProject/Hotaru/develop/singularity/assets/WebApp.jpg"
)
HOTARU_RAW_MAIN_URL = (
    "https://raw.githubusercontent.com/HotaruProject/Hotaru/main/singularity/assets/WebApp.jpg"
)
HOTARU_WEBAPP_PHOTO_URLS: tuple[str, ...] = (
    HOTARU_RAW_DEVELOP_URL,
    HOTARU_RAW_MAIN_URL,
)
LOCAL_WEBAPP_PHOTO_PATH = Path(__file__).resolve().parent / "assets" / "WebApp.jpg"

_CACHED_REMOTE_BANNER_URL: str | None = None
_LAST_REMOTE_CHECK_TS: float = 0.0


@dataclass(slots=True)
class SanctuaryAppState:
    registered: bool = False
    bot_id: int = 0
    bot_username: str = ""
    short_name: str = "dashboard"
    title: str = "Hotaru"
    app_link: str = ""
    last_tunnel_url: str = ""
    updated_at: int = 0


@dataclass(slots=True)
class RelaySyncReport:
    ok: bool
    app_link: str | None = None
    registered: bool = False
    bot_username: str = ""
    bot_token: str = ""
    short_name: str = "dashboard"
    detail: str = ""


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        val = obj.get(key, default)
        return default if val is None else val
    val = getattr(obj, key, default)
    return default if val is None else val


def _is_valid_image_bytes(data: bytes) -> bool:
    if len(data) < 512:
        return False
    return data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")


def _https_request_sync(url: str, method: str, timeout: float) -> tuple[int, bytes]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return 0, b""
    host = parsed.hostname
    port = parsed.port or 443
    path_part = parsed.path or "/"
    if parsed.query:
        path_part = f"{path_part}?{parsed.query}"
    req_bytes = (
        f"{method} {path_part} HTTP/1.0\r\n"
        f"Host: {host}\r\n"
        "User-Agent: Hotaru-Singularity/1.0\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    with trusted_scope():
        tls_ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as raw_sock:
            with tls_ctx.wrap_socket(raw_sock, server_hostname=host) as tls_sock:
                tls_sock.sendall(req_bytes)
                buf = bytearray()
                limit = 65536 if method == "HEAD" else (2 * 1024 * 1024 + 65536)
                while len(buf) < limit:
                    chunk = tls_sock.recv(16384)
                    if not chunk:
                        break
                    buf.extend(chunk)
    sep = buf.find(b"\r\n\r\n")
    if sep < 0:
        return 0, b""
    first_line = bytes(buf[:sep]).split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
    parts = first_line.split(" ", 2)
    status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    body = b"" if method == "HEAD" else bytes(buf[sep + 4 : sep + 4 + 2 * 1024 * 1024])
    return status, body


def _fetch_url_bytes_sync(url: str, timeout: float = 3.5) -> bytes | None:
    try:
        status, payload = _https_request_sync(url, "GET", timeout)
        if status != 200:
            return None
        return payload if _is_valid_image_bytes(payload) else None
    except Exception:
        return None


def _probe_url_head_sync(url: str, timeout: float = 2.5) -> bool:
    try:
        status, _ = _https_request_sync(url, "HEAD", timeout)
        return status == 200
    except Exception:
        return False


def generate_fallback_png_640x360() -> bytes:
    width = 640
    height = 360
    rows: list[bytes] = []
    cx = width * 0.5
    cy = height * 0.5

    for y in range(height):
        row = bytearray(1 + width * 3)
        row[0] = 0
        dy = (y - cy) / 110.0
        for x in range(width):
            dx = (x - cx) / 110.0
            r2 = dx * dx + dy * dy * 2.4
            if r2 < 0.36:
                c = 4
            else:
                ring = max(0.0, 1.0 - abs(r2 - 0.52) * 3.8)
                disk = max(0.0, 1.0 - abs(dy) * 7.5) * max(0.0, 1.0 - r2 * 0.32)
                lum = min(1.0, ring * 0.85 + disk * 0.55)
                c = int(6 + lum * 225)
            idx = 1 + x * 3
            row[idx] = min(255, c)
            row[idx + 1] = min(255, int(c * 1.03))
            row[idx + 2] = min(255, int(c * 1.08) + 4)
        rows.append(bytes(row))

    raw_data = b"".join(rows)
    compressed = zlib.compress(raw_data, level=6)

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


async def resolve_cover_photo_bytes(allow_remote: bool = True) -> tuple[bytes, str]:
    with trusted_scope():
        if allow_remote:
            loop = asyncio.get_running_loop()
            for url in HOTARU_WEBAPP_PHOTO_URLS:
                fetched = await loop.run_in_executor(None, _fetch_url_bytes_sync, url, 3.0)
                if fetched is not None:
                    ext = "jpg" if fetched.startswith(b"\xff\xd8\xff") else "png"
                    return fetched, f"WebApp.{ext}"

        if LOCAL_WEBAPP_PHOTO_PATH.is_file():
            try:
                local_bytes = LOCAL_WEBAPP_PHOTO_PATH.read_bytes()
                if _is_valid_image_bytes(local_bytes):
                    ext = "jpg" if local_bytes.startswith(b"\xff\xd8\xff") else "png"
                    return local_bytes, f"WebApp.{ext}"
            except OSError:
                pass

        return generate_fallback_png_640x360(), "WebApp.png"


async def resolve_inline_banner_url(tunnel_url: str | None = None) -> str | None:
    global _CACHED_REMOTE_BANNER_URL, _LAST_REMOTE_CHECK_TS
    with trusted_scope():
        if _CACHED_REMOTE_BANNER_URL:
            return _CACHED_REMOTE_BANNER_URL

        now = time.monotonic()
        if now - _LAST_REMOTE_CHECK_TS >= 60.0:
            _LAST_REMOTE_CHECK_TS = now
            loop = asyncio.get_running_loop()
            for candidate in HOTARU_WEBAPP_PHOTO_URLS:
                ok = await loop.run_in_executor(None, _probe_url_head_sync, candidate, 2.0)
                if ok:
                    _CACHED_REMOTE_BANNER_URL = candidate
                    return candidate

        if tunnel_url and tunnel_url.startswith("https://") and LOCAL_WEBAPP_PHOTO_PATH.is_file():
            return f"{tunnel_url.rstrip('/')}/public/WebApp.jpg"
        return None


def get_sanctuary_app_state(state: Any | None) -> SanctuaryAppState:
    if state is None:
        return SanctuaryAppState()
    try:
        ns = state.namespace("singularity") if hasattr(state, "namespace") else None
        registered = False
        bot_id = 0
        bot_username = ""
        short_name = "dashboard"
        title = "Hotaru"
        app_link = ""
        last_url = ""
        updated_at = 0

        if ns is not None:
            registered = bool(ns.get("webapp_registered", False))
            raw_bid = ns.get("webapp_bot_id", 0)
            bot_id = int(raw_bid) if isinstance(raw_bid, int) or str(raw_bid).isdigit() else 0
            bot_username = str(ns.get("webapp_bot_username", "") or "").lstrip("@").strip()
            short_name = str(ns.get("webapp_short_name", "dashboard") or "dashboard").strip()
            title = str(ns.get("webapp_title", "Hotaru") or "Hotaru").strip()
            app_link = str(ns.get("webapp_link", "") or "").strip()
            last_url = str(ns.get("webapp_last_url", "") or "").strip()
            raw_ts = ns.get("webapp_updated_at", 0)
            updated_at = int(raw_ts) if isinstance(raw_ts, (int, float)) else 0

        if not registered and hasattr(state, "get_setting"):
            setting_reg = state.get_setting("singularity-webapp-registered")
            if isinstance(setting_reg, bool) and setting_reg:
                registered = True
            if not app_link:
                app_link = str(state.get_setting("singularity-webapp-link") or "").strip()

        return SanctuaryAppState(
            registered=registered,
            bot_id=bot_id,
            bot_username=bot_username,
            short_name=short_name or "dashboard",
            title=title or "Hotaru",
            app_link=app_link,
            last_tunnel_url=last_url,
            updated_at=updated_at,
        )
    except Exception as exc:
        log.debug("Failed to read sanctuary app state: %s", exc)
        return SanctuaryAppState()


def save_sanctuary_app_state(
    state: Any | None,
    *,
    registered: bool,
    bot_id: int = 0,
    bot_username: str = "",
    short_name: str = "dashboard",
    title: str = "Hotaru",
    app_link: str = "",
    last_tunnel_url: str = "",
) -> SanctuaryAppState:
    clean_bot = bot_username.lstrip("@").strip()
    clean_short = (short_name or "dashboard").strip()
    clean_title = (title or "Hotaru").strip()
    resolved_link = app_link.strip() or (
        f"https://t.me/{clean_bot}/{clean_short}" if (registered and clean_bot) else ""
    )
    now_sec = int(time.time())
    record = SanctuaryAppState(
        registered=bool(registered),
        bot_id=int(bot_id or 0),
        bot_username=clean_bot,
        short_name=clean_short,
        title=clean_title,
        app_link=resolved_link,
        last_tunnel_url=last_tunnel_url.strip(),
        updated_at=now_sec,
    )
    if state is None:
        return record
    try:
        if hasattr(state, "namespace"):
            ns = state.namespace("singularity")
            ns.set("webapp_registered", record.registered)
            ns.set("webapp_bot_id", record.bot_id)
            ns.set("webapp_bot_username", record.bot_username)
            ns.set("webapp_short_name", record.short_name)
            ns.set("webapp_title", record.title)
            ns.set("webapp_link", record.app_link)
            ns.set("webapp_last_url", record.last_tunnel_url)
            ns.set("webapp_updated_at", record.updated_at)
        if hasattr(state, "set_setting"):
            state.set_setting("singularity-webapp-registered", record.registered)
            state.set_setting("singularity-webapp-link", record.app_link)
    except Exception as exc:
        log.warning("Failed to persist sanctuary app state: %s", exc)
    return record


def _unwrap_tl(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    inner = value.get("result")
    return inner if isinstance(inner, dict) else value


def _extract_msg_id(msg: Any) -> int:
    mid = _field(msg, "id") or _field(msg, "message_id")
    return int(mid) if isinstance(mid, int) else 0


def _extract_msg_text(msg: Any) -> str:
    if msg is None:
        return ""
    raw_msg = _field(msg, "message", None)
    if raw_msg is None:
        raw_msg = _field(msg, "text", "")
    if isinstance(raw_msg, dict):
        base = str(raw_msg.get("text") or "")
    else:
        base = str(raw_msg or "")

    entities = _field(msg, "entities", None)
    if isinstance(entities, list) and entities:
        extra_urls: list[str] = []
        for ent in entities:
            ent_url = _field(ent, "url", "")
            if isinstance(ent_url, str) and ent_url and ent_url not in base:
                extra_urls.append(ent_url)
        if extra_urls:
            base = f"{base}\n" + "\n".join(extra_urls) if base else "\n".join(extra_urls)
    return base


def _is_outgoing(msg: Any, self_id: int = 0) -> bool:
    if isinstance(msg, dict):
        if msg.get("out"):
            return True
        sender = msg.get("from_id")
        if isinstance(sender, int) and self_id:
            return sender == self_id
        if isinstance(sender, dict) and self_id:
            return sender.get("user_id") == self_id
        return False
    return bool(getattr(msg, "out", False))


def _coerce_callback_bytes(raw_data: Any) -> bytes | None:
    if isinstance(raw_data, (bytes, bytearray, memoryview)):
        return bytes(raw_data)
    if isinstance(raw_data, str):
        return raw_data.encode("utf-8")
    if isinstance(raw_data, list) and raw_data and all(isinstance(x, int) and 0 <= x <= 255 for x in raw_data):
        return bytes(raw_data)
    return None


def _flatten_buttons(msg: Any) -> list[tuple[int, str, bytes]]:
    if msg is None:
        return []
    markup = _field(msg, "reply_markup") or _field(msg, "buttons")
    if not markup:
        return []
    rows = (
        markup
        if isinstance(markup, list)
        else (
            _field(markup, "rows")
            or _field(markup, "inline_keyboard")
            or _field(markup, "keyboard")
        )
    )
    if not rows:
        return []

    out: list[tuple[int, str, bytes]] = []
    flat_idx = 0
    for row in rows:
        btns = row if isinstance(row, list) else _field(row, "buttons", [])
        for btn in btns or []:
            raw_text = btn if isinstance(btn, str) else _field(btn, "text", "")
            label = str(_field(raw_text, "text", raw_text) or "")
            kind = _field(btn, "type", btn)
            raw_data = _field(kind, "data", None)
            if raw_data is None:
                raw_data = _field(btn, "data", _field(btn, "callback_data", None))
            data_bytes = _coerce_callback_bytes(raw_data)
            if data_bytes is not None:
                out.append((flat_idx, label, data_bytes))
            flat_idx += 1
    return out


def _find_button_by_text(msg: Any, *needles: str) -> tuple[int, str, bytes] | None:
    buttons = _flatten_buttons(msg)
    for needle in needles:
        if not needle:
            continue
        low = needle.lower()
        for entry in buttons:
            if low in entry[1].lower():
                return entry
    return None


def _find_next_page_button(msg: Any) -> tuple[int, str, bytes] | None:
    for entry in _flatten_buttons(msg):
        label = entry[1].strip()
        if label in ("»", ">", ">", "Next", "Next »"):
            return entry
    return None


def _is_url_prompt_echo(text: str) -> bool:
    low = text.strip().lower()
    if not low:
        return False
    if "updated" in low or "success" in low or "invalid" in low or "done" in low:
        return False
    return (
        "please send a new web app url" in low
        or "send me the new url" in low
        or "please send the web app url" in low
    )


async def _sleep_on_flood(exc: BaseException) -> bool:
    text = str(exc)
    m = FLOOD_WAIT_RE.search(text)
    wait_sec = getattr(exc, "seconds", None) or getattr(exc, "value", None)
    if not isinstance(wait_sec, (int, float)) and m:
        raw_val = m.group(1) or m.group(2)
        wait_sec = int(raw_val) if raw_val else None
    if isinstance(wait_sec, (int, float)) and 0 < wait_sec <= 60:
        await asyncio.sleep(float(wait_sec) + 0.5)
        return True
    return False


class _BotFatherRelayDriver:
    def __init__(self, runtime: Any, poll_interval: float = 0.45) -> None:
        self.runtime = runtime
        self.app = getattr(runtime, "app", None)
        self.mt = getattr(self.app, "mt", None) if self.app is not None else getattr(runtime, "mt", None)
        self.poll_interval = poll_interval
        self._peer: Any = None
        self._self_id: int = int(
            getattr(getattr(self.app, "mt", None), "self_id", 0)
            or getattr(getattr(self.app, "session", None), "self_id", 0)
            or getattr(runtime, "owner_id", 0)
            or 0
        )

    @property
    def is_goygram(self) -> bool:
        return self.app is not None and hasattr(self.app, "mt_messages_get_history")

    async def prepare(self) -> bool:
        with trusted_scope():
            if self.is_goygram:
                if self.mt is None:
                    return False
                self._peer = await self.mt.resolve_peer(BOTFATHER_PEER)
                return self._peer is not None
            return self.mt is not None

    async def get_recent_messages(self, limit: int = 8) -> list[Any]:
        with trusted_scope():
            if self.is_goygram:
                raw = await self.app.mt_messages_get_history(
                    peer=self._peer,
                    offset_id=0,
                    offset_date=0,
                    add_offset=0,
                    limit=limit,
                    max_id=0,
                    min_id=0,
                    hash=0,
                )
                body = _unwrap_tl(raw)
                msgs = body.get("messages")
                return [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else []
            if hasattr(self.mt, "get_history"):
                res = await self.mt.get_history(BOTFATHER_USERNAME, limit=limit)
                return list(res) if res else []
            return []

    async def max_message_id(self) -> int:
        msgs = await self.get_recent_messages(limit=4)
        return max((_extract_msg_id(m) for m in msgs), default=0)

    async def send_text(self, text: str) -> int:
        for attempt in range(2):
            try:
                with trusted_scope():
                    if self.is_goygram:
                        before = await self.max_message_id()
                        raw = await self.app.mt_messages_send_message(
                            peer=self._peer,
                            message=text,
                            random_id=secrets.randbits(63),
                        )
                        sent_id = self._extract_sent_id_from_updates(raw)
                        return sent_id or before
                    sent = await self.mt.send_message(BOTFATHER_USERNAME, text)
                    return _extract_msg_id(sent)
            except Exception as exc:
                if attempt == 0 and await _sleep_on_flood(exc):
                    continue
                raise
        return 0

    async def send_cover_photo(self, image_bytes: bytes, file_name: str = "WebApp.jpg") -> int:
        before = await self.max_message_id()
        with trusted_scope():
            if self.is_goygram:
                if _relay_put_file is not None:
                    up = await _relay_put_file(self.app, image_bytes, file_name=file_name)
                elif hasattr(self.app, "charged_upload"):
                    up = await self.app.charged_upload(image_bytes, file_name=file_name)
                else:
                    raise RuntimeError("photo_upload_unsupported")
                input_file = (
                    {
                        "_": "inputFileBig",
                        "id": up["id"],
                        "parts": up["parts"],
                        "name": up["name"],
                    }
                    if up.get("big")
                    else {
                        "_": "inputFile",
                        "id": up["id"],
                        "parts": up["parts"],
                        "name": up["name"],
                        "md5_checksum": up.get("md5", ""),
                    }
                )
                media = {"_": "inputMediaUploadedPhoto", "file": input_file}
                raw = await self.app.mt_messages_send_media(
                    peer=self._peer,
                    media=media,
                    message="",
                    random_id=secrets.randbits(63),
                )
                sent_id = self._extract_sent_id_from_updates(raw)
                return sent_id or before

            suffix = ".jpg" if file_name.lower().endswith(".jpg") else ".png"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(image_bytes)
                tmp_path = Path(tmp.name)
            try:
                if hasattr(self.mt, "send_photo"):
                    sent = await self.mt.send_photo(BOTFATHER_USERNAME, str(tmp_path))
                    return _extract_msg_id(sent) or before
                if hasattr(self.mt, "send_file"):
                    sent = await self.mt.send_file(BOTFATHER_USERNAME, str(tmp_path))
                    return _extract_msg_id(sent) or before
                raise RuntimeError("photo_upload_unsupported")
            finally:
                tmp_path.unlink(missing_ok=True)

    async def click_button(self, msg_id: int, data: bytes) -> str:
        for attempt in range(2):
            try:
                with trusted_scope():
                    if self.is_goygram:
                        raw = await self.app.mt_messages_get_bot_callback_answer(
                            peer=self._peer,
                            msg_id=msg_id,
                            data=data,
                        )
                        body = _unwrap_tl(raw)
                        return str(body.get("message") or "")
                    if hasattr(self.mt, "click_button"):
                        ans = await self.mt.click_button(BOTFATHER_USERNAME, msg_id, data)
                        if isinstance(ans, dict):
                            return str(ans.get("message") or "")
                        return str(getattr(ans, "message", "") or "")
                    return ""
            except Exception as exc:
                if attempt == 0 and await _sleep_on_flood(exc):
                    continue
                raise
        return ""

    async def wait_reply_after(
        self,
        floor_id: int,
        timeout: float = 10.0,
        *,
        skip_url_prompt_echo: bool = False,
    ) -> Any | None:
        deadline = time.monotonic() + timeout
        current_floor = floor_id
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_interval)
            try:
                msgs = await self.get_recent_messages(limit=8)
            except Exception:
                continue
            incoming = [
                m
                for m in msgs
                if _extract_msg_id(m) > current_floor and not _is_outgoing(m, self._self_id)
            ]
            if incoming:
                incoming.sort(key=_extract_msg_id)
                if skip_url_prompt_echo:
                    non_echo = [m for m in incoming if not _is_url_prompt_echo(_extract_msg_text(m))]
                    if not non_echo:
                        current_floor = max(_extract_msg_id(m) for m in incoming)
                        continue
                    return non_echo[-1]
                return incoming[-1]
        return None

    async def wait_edited_or_new(
        self, msg_id: int, previous_text: str, timeout: float = 8.0
    ) -> Any | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_interval)
            try:
                msgs = await self.get_recent_messages(limit=8)
            except Exception:
                continue
            for m in msgs:
                if _is_outgoing(m, self._self_id):
                    continue
                mid = _extract_msg_id(m)
                txt = _extract_msg_text(m)
                if mid > msg_id:
                    return m
                if mid == msg_id and txt and txt != previous_text:
                    return m
        return None

    async def ask(
        self,
        text: str,
        timeout: float = 10.0,
        *,
        min_floor: int = 0,
        skip_url_prompt_echo: bool = False,
    ) -> Any | None:
        sent_id = await self.send_text(text)
        floor = max(sent_id, min_floor)
        return await self.wait_reply_after(
            floor,
            timeout=timeout,
            skip_url_prompt_echo=skip_url_prompt_echo,
        )

    async def wait_app_detail_screen(
        self, list_msg_id: int, previous_text: str, timeout: float = 8.0
    ) -> Any | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_interval)
            try:
                msgs = await self.get_recent_messages(limit=8)
            except Exception:
                continue
            for m in msgs:
                if _is_outgoing(m, self._self_id):
                    continue
                mid = _extract_msg_id(m)
                txt = _extract_msg_text(m)
                has_detail = (
                    "direct link:" in txt.lower()
                    or "web app url:" in txt.lower()
                    or _find_button_by_text(m, "Edit Web App URL") is not None
                )
                if mid > list_msg_id and has_detail:
                    return m
                if mid == list_msg_id and (has_detail or (txt and txt != previous_text)):
                    if has_detail:
                        return m
        return None

    @staticmethod
    def _extract_sent_id_from_updates(raw: Any) -> int:
        body = _unwrap_tl(raw)
        if isinstance(body.get("id"), int):
            return int(body["id"])
        updates = body.get("updates")
        if isinstance(updates, list):
            for upd in updates:
                if not isinstance(upd, dict):
                    continue
                kind = upd.get("_")
                if kind == "updateNewMessage" and isinstance(upd.get("message"), dict):
                    mid = upd["message"].get("id")
                    if isinstance(mid, int):
                        return mid
                if kind == "updateMessageID" and isinstance(upd.get("id"), int):
                    return int(upd["id"])
        return 0


def _persist_inline_bot_credentials(
    runtime: Any,
    state: Any | None,
    bot_id: int,
    username: str,
    token: str,
) -> None:
    clean_user = username.lstrip("@").strip()
    clean_tok = token.strip()
    if state is not None and hasattr(state, "set_setting"):
        try:
            state.set_setting("inline-bot-token", clean_tok)
            state.set_setting("inline-bot-username", clean_user)
            state.set_setting("inline-bot-id", int(bot_id))
        except Exception:
            pass
    inline_mgr = getattr(runtime, "inline", None)
    if inline_mgr is not None and getattr(inline_mgr, "info", None) is None and InlineBotInfo is not None:
        try:
            inline_mgr.info = InlineBotInfo(clean_tok, clean_user, int(bot_id))
        except Exception:
            pass


async def _provision_bot_via_driver(
    driver: _BotFatherRelayDriver,
    runtime: Any,
    state: Any | None,
) -> tuple[int, str, str]:
    await driver.send_text("/cancel")
    await asyncio.sleep(driver.poll_interval)

    mybots_msg = await driver.ask("/mybots", timeout=9.0)
    if mybots_msg is not None:
        current_msg = mybots_msg
        for _ in range(5):
            btns = _flatten_buttons(current_msg)
            hotaru_btn = next(
                (b for b in btns if "hotaru" in b[1].lower() and b[1].lower().lstrip("@").endswith("bot")),
                None,
            )
            if hotaru_btn is not None:
                cand_username = hotaru_btn[1].strip().lstrip("@")
                prev_text = _extract_msg_text(current_msg)
                mid = _extract_msg_id(current_msg)
                await driver.click_button(mid, hotaru_btn[2])
                bot_menu = await driver.wait_edited_or_new(mid, prev_text, timeout=7.0)
                token = ""
                if bot_menu is not None:
                    tok_btn = _find_button_by_text(bot_menu, "API Token")
                    if tok_btn is not None:
                        menu_text = _extract_msg_text(bot_menu)
                        menu_id = _extract_msg_id(bot_menu)
                        await driver.click_button(menu_id, tok_btn[2])
                        tok_msg = await driver.wait_edited_or_new(menu_id, menu_text, timeout=7.0)
                        tok_match = BOT_TOKEN_RE.search(_extract_msg_text(tok_msg))
                        if tok_match:
                            token = tok_match.group(1)
                if not token:
                    await driver.send_text("/cancel")
                    await asyncio.sleep(driver.poll_interval)
                    r_tok = await driver.ask("/token", timeout=7.0)
                    if r_tok is not None:
                        ans_tok = await driver.ask(f"@{cand_username}", timeout=7.0)
                        tok_match = BOT_TOKEN_RE.search(_extract_msg_text(ans_tok))
                        if tok_match:
                            token = tok_match.group(1)
                if token:
                    bot_id = int(token.split(":", 1)[0])
                    _persist_inline_bot_credentials(runtime, state, bot_id, cand_username, token)
                    return bot_id, cand_username, token
                break
            next_btn = _find_next_page_button(current_msg)
            if next_btn is None:
                break
            prev_text = _extract_msg_text(current_msg)
            mid = _extract_msg_id(current_msg)
            await driver.click_button(mid, next_btn[2])
            nxt_msg = await driver.wait_edited_or_new(mid, prev_text, timeout=7.0)
            if nxt_msg is None:
                break
            current_msg = nxt_msg

    await driver.send_text("/cancel")
    await asyncio.sleep(driver.poll_interval)

    r_new = await driver.ask("/newbot", timeout=9.0)
    if r_new is None:
        return 0, "", ""
    new_low = _extract_msg_text(r_new).lower()
    if "cannot create" in new_low or "too many" in new_low or "up to 20" in new_low:
        await driver.send_text("/cancel")
        return 0, "", ""
    await driver.ask("Hotaru Relay", timeout=8.0)

    for _ in range(5):
        cand_username = f"hotaru_{secrets.token_hex(3)}_bot"
        reply = await driver.ask(cand_username, timeout=9.0)
        txt = _extract_msg_text(reply)
        tok_match = BOT_TOKEN_RE.search(txt)
        if tok_match:
            token = tok_match.group(1)
            bot_id = int(token.split(":", 1)[0])
            r_in = await driver.ask("/setinline", timeout=8.0)
            if r_in is not None:
                await driver.ask(f"@{cand_username}", timeout=8.0)
                await driver.ask("hotaru:~$", timeout=8.0)
            _persist_inline_bot_credentials(runtime, state, bot_id, cand_username, token)
            return bot_id, cand_username, token
        low = txt.lower()
        if "taken" in low or "occupied" in low or "sorry" in low:
            continue
        break

    await driver.send_text("/cancel")
    return 0, "", ""


async def ensure_inline_bot_ready(
    runtime: Any,
    *,
    poll_interval: float = 0.45,
) -> tuple[int, str, str]:
    with trusted_scope():
        state = getattr(runtime, "state", None)
        inline_mgr = getattr(runtime, "inline", None)

        if inline_mgr is not None:
            info = getattr(inline_mgr, "info", None)
            if info is not None and getattr(info, "username", None) and getattr(info, "token", None):
                return (
                    int(getattr(info, "bot_id", 0) or 0),
                    str(info.username).lstrip("@").strip(),
                    str(info.token).strip(),
                )
            bg_task = getattr(inline_mgr, "_task", None)
            ready_ev = getattr(inline_mgr, "ready", None)
            if (
                bg_task is not None
                and hasattr(bg_task, "done")
                and not bg_task.done()
                and ready_ev is not None
                and hasattr(ready_ev, "wait")
                and not ready_ev.is_set()
            ):
                try:
                    await asyncio.wait_for(ready_ev.wait(), timeout=8.0)
                except Exception:
                    pass
                info = getattr(inline_mgr, "info", None)
                if info is not None and getattr(info, "username", None) and getattr(info, "token", None):
                    return (
                        int(getattr(info, "bot_id", 0) or 0),
                        str(info.username).lstrip("@").strip(),
                        str(info.token).strip(),
                    )
            if hasattr(inline_mgr, "ensure_bot"):
                try:
                    ensured = await inline_mgr.ensure_bot(allow_create=True)
                    if ensured is None and state is not None and hasattr(state, "set_setting"):
                        state.set_setting("inline-bot-token", None)
                        state.set_setting("inline-bot-username", None)
                        state.set_setting("inline-bot-id", None)
                        ensured = await inline_mgr.ensure_bot(allow_create=True)
                    if ensured is not None:
                        if hasattr(inline_mgr, "start"):
                            try:
                                await inline_mgr.start()
                            except Exception:
                                pass
                        return (
                            int(getattr(ensured, "bot_id", 0) or 0),
                            str(ensured.username).lstrip("@").strip(),
                            str(ensured.token).strip(),
                        )
                except Exception as exc:
                    log.debug("InlineManager.ensure_bot fallback triggered: %s", exc)

        if state is not None and hasattr(state, "get_setting"):
            saved_tok = str(state.get_setting("inline-bot-token") or "").strip()
            saved_usr = str(state.get_setting("inline-bot-username") or "").lstrip("@").strip()
            saved_bid = state.get_setting("inline-bot-id")
            if saved_tok and saved_usr:
                bid = (
                    int(saved_bid)
                    if isinstance(saved_bid, int) or str(saved_bid).isdigit()
                    else int(saved_tok.split(":", 1)[0])
                )
                return bid, saved_usr, saved_tok

        driver = _BotFatherRelayDriver(runtime, poll_interval=poll_interval)
        async with AsyncExitStack() as stack:
            if driver.is_goygram and BotFatherGuard is not None:
                try:
                    await stack.enter_async_context(BotFatherGuard(driver.app))
                except Exception:
                    pass
            if await driver.prepare():
                bid, buser, btok = await _provision_bot_via_driver(driver, runtime, state)
                if btok and inline_mgr is not None and hasattr(inline_mgr, "start"):
                    try:
                        await inline_mgr.start()
                    except Exception:
                        pass
                return bid, buser, btok
        return 0, "", ""


def _rank_myapps_candidates(
    msg: Any,
    *,
    title: str,
    short_name: str,
    clean_bot: str,
) -> list[tuple[int, str, bytes]]:
    buttons = _flatten_buttons(msg)
    if not buttons:
        return []

    title_low = title.lower()
    short_low = short_name.lower()
    bot_low = clean_bot.lower()

    def _score(entry: tuple[int, str, bytes]) -> tuple[int, int]:
        idx, label, _ = entry
        low = label.lower().strip()
        if (
            "back" in low
            or "cancel" in low
            or low in ("»", "«", ">", "<", ">", "<", "next", "prev", "next »", "« prev")
        ):
            return (-10, -idx)
        score = 0
        if bot_low and bot_low in low:
            score += 10
        if short_low and short_low in low:
            score += 5
        if title_low and title_low in low:
            score += 5
        if "hotaru" in low or "singularity" in low:
            score += 4
        return (score, -idx)

    ranked = [b for b in buttons if _score(b)[0] >= 0]
    ranked.sort(key=_score, reverse=True)
    return ranked


async def _open_myapps_page(
    driver: _BotFatherRelayDriver,
    page_index: int,
) -> Any | None:
    msg = await driver.ask("/myapps", timeout=9.0)
    if msg is None:
        return None
    for _ in range(page_index):
        nxt = _find_next_page_button(msg)
        if nxt is None:
            return None
        prev_txt = _extract_msg_text(msg)
        mid = _extract_msg_id(msg)
        await driver.click_button(mid, nxt[2])
        edited = await driver.wait_edited_or_new(mid, prev_txt, timeout=7.0)
        if edited is None:
            return None
        msg = edited
    return msg


async def _sync_existing_via_myapps(
    driver: _BotFatherRelayDriver,
    *,
    clean_bot: str,
    tunnel_url: str,
    short_name: str,
    title: str,
) -> RelaySyncReport | None:
    first_page = await _open_myapps_page(driver, 0)
    if first_page is None:
        return RelaySyncReport(ok=False, bot_username=clean_bot, detail="botfather_no_response")

    initial_text = _extract_msg_text(first_page)
    if "no web apps" in initial_text.lower() or "currently have no" in initial_text.lower():
        return None

    for page_idx in range(8):
        page_msg = first_page if page_idx == 0 else await _open_myapps_page(driver, page_idx)
        if page_msg is None:
            break

        has_next_page = _find_next_page_button(page_msg) is not None
        candidates = _rank_myapps_candidates(
            page_msg,
            title=title,
            short_name=short_name,
            clean_bot=clean_bot,
        )

        for cand_idx in range(len(candidates)):
            if cand_idx > 0:
                page_msg = await _open_myapps_page(driver, page_idx)
                if page_msg is None:
                    break
                refreshed = _rank_myapps_candidates(
                    page_msg,
                    title=title,
                    short_name=short_name,
                    clean_bot=clean_bot,
                )
                if cand_idx >= len(refreshed):
                    break
                _, _, btn_data = refreshed[cand_idx]
            else:
                _, _, btn_data = candidates[0]

            list_msg_id = _extract_msg_id(page_msg)
            list_text = _extract_msg_text(page_msg)
            await driver.click_button(list_msg_id, btn_data)

            detail_msg = await driver.wait_app_detail_screen(
                list_msg_id, list_text, timeout=8.0
            )
            if detail_msg is None:
                continue

            detail_text = _extract_msg_text(detail_msg)
            link_match = TME_APP_LINK_RE.search(detail_text)
            matched_slug = short_name
            matched_link: str | None = None
            if link_match:
                link_bot, link_slug = link_match.group(1), link_match.group(2)
                if link_bot.casefold() != clean_bot.casefold():
                    continue
                matched_slug = link_slug
                matched_link = f"https://t.me/{link_bot}/{link_slug}"
            else:
                low_detail = detail_text.lower()
                if f"@{clean_bot.lower()}" not in low_detail and f"/{clean_bot.lower()}/" not in low_detail:
                    continue

            url_match = WEBAPP_URL_LINE_RE.search(detail_text)
            if url_match and url_match.group(1).rstrip("/") == tunnel_url.rstrip("/"):
                return RelaySyncReport(
                    ok=True,
                    app_link=matched_link or f"https://t.me/{clean_bot}/{matched_slug}",
                    registered=True,
                    bot_username=clean_bot,
                    short_name=matched_slug,
                    detail="adopted_unchanged",
                )

            edit_btn = _find_button_by_text(
                detail_msg, "Edit Web App URL", "Web App URL", "Edit URL"
            )
            if edit_btn is None:
                all_btns = _flatten_buttons(detail_msg)
                if len(all_btns) >= 5 and "url" in all_btns[4][1].lower():
                    edit_btn = all_btns[4]
            if edit_btn is None:
                continue

            detail_msg_id = _extract_msg_id(detail_msg)
            floor_before_click = max(detail_msg_id, await driver.max_message_id())
            cb_toast = await driver.click_button(detail_msg_id, edit_btn[2])

            prompt_timeout = (
                min(3.5, max(0.8, driver.poll_interval * 5))
                if "url" in cb_toast.lower()
                else 5.0
            )
            prompt_msg = await driver.wait_reply_after(
                floor_before_click, timeout=prompt_timeout
            )
            prompt_floor = (
                _extract_msg_id(prompt_msg)
                if prompt_msg is not None
                else floor_before_click
            )

            done_reply = await driver.ask(
                tunnel_url,
                timeout=9.0,
                min_floor=prompt_floor,
                skip_url_prompt_echo=True,
            )
            done_text = _extract_msg_text(done_reply)
            done_low = done_text.lower()
            if "invalid" in done_low and "url" in done_low:
                await driver.send_text("/cancel")
                return RelaySyncReport(
                    ok=False,
                    bot_username=clean_bot,
                    detail="invalid_url_rejected",
                )

            final_link = matched_link or f"https://t.me/{clean_bot}/{matched_slug}"
            if done_reply is not None and (
                "updated" in done_low
                or "success" in done_low
                or "good" in done_low
                or "done" in done_low
                or "t.me/" in done_low
                or tunnel_url.lower().rstrip("/") in done_low
            ):
                return RelaySyncReport(
                    ok=True,
                    app_link=final_link,
                    registered=True,
                    bot_username=clean_bot,
                    short_name=matched_slug,
                    detail="updated_existing",
                )

        if not has_next_page:
            break

    return None


async def _register_new_via_newapp(
    driver: _BotFatherRelayDriver,
    *,
    clean_bot: str,
    tunnel_url: str,
    short_name: str,
    title: str,
    description: str,
    allow_remote_photo: bool = True,
) -> RelaySyncReport:
    r_new = await driver.ask("/newapp", timeout=9.0)
    if r_new is None:
        return RelaySyncReport(ok=False, bot_username=clean_bot, detail="botfather_no_response")

    bot_btn = _find_button_by_text(r_new, f"@{clean_bot}", clean_bot)
    if bot_btn is not None:
        floor_id = max(_extract_msg_id(r_new), await driver.max_message_id())
        await driver.click_button(_extract_msg_id(r_new), bot_btn[2])
        r_bot = await driver.wait_reply_after(floor_id, timeout=8.0)
    else:
        r_bot = await driver.ask(f"@{clean_bot}", timeout=8.0)

    if r_bot is None or "invalid bot" in _extract_msg_text(r_bot).lower():
        await driver.send_text("/cancel")
        return RelaySyncReport(ok=False, bot_username=clean_bot, detail="invalid_bot_selected")

    await driver.ask(title, timeout=8.0)
    await driver.ask(description, timeout=8.0)

    cover_bytes, cover_name = await resolve_cover_photo_bytes(allow_remote=allow_remote_photo)
    photo_floor = await driver.send_cover_photo(cover_bytes, file_name=cover_name)
    photo_reply = await driver.wait_reply_after(photo_floor, timeout=12.0)
    if photo_reply is None or "640x360" in _extract_msg_text(photo_reply).lower():
        await driver.send_text("/cancel")
        return RelaySyncReport(ok=False, bot_username=clean_bot, detail="cover_photo_rejected")

    await driver.ask("/empty", timeout=8.0)

    url_reply = await driver.ask(
        tunnel_url,
        timeout=8.0,
        skip_url_prompt_echo=True,
    )
    if url_reply is None or "invalid" in _extract_msg_text(url_reply).lower():
        await driver.send_text("/cancel")
        return RelaySyncReport(ok=False, bot_username=clean_bot, detail="invalid_url_rejected")

    slug_candidates = [short_name]
    for fallback_slug in ("singularity", "hotaru", f"hotaru_{secrets.token_hex(2)}"):
        if fallback_slug.lower() not in [s.lower() for s in slug_candidates]:
            slug_candidates.append(fallback_slug)

    for candidate_slug in slug_candidates:
        final_reply = await driver.ask(candidate_slug, timeout=10.0)
        final_text = _extract_msg_text(final_reply)
        final_low = final_text.lower()
        link_match = TME_APP_LINK_RE.search(final_text)
        if link_match:
            resolved_bot = link_match.group(1)
            resolved_slug = link_match.group(2)
            return RelaySyncReport(
                ok=True,
                app_link=f"https://t.me/{resolved_bot}/{resolved_slug}",
                registered=True,
                bot_username=clean_bot,
                short_name=resolved_slug,
                detail="created_new",
            )
        if "sorry" in final_low or "occupied" in final_low or "taken" in final_low or "already" in final_low:
            continue
        if "done" in final_low or "available" in final_low:
            return RelaySyncReport(
                ok=True,
                app_link=f"https://t.me/{clean_bot}/{candidate_slug}",
                registered=True,
                bot_username=clean_bot,
                short_name=candidate_slug,
                detail="created_new",
            )

    await driver.send_text("/cancel")
    return RelaySyncReport(ok=False, bot_username=clean_bot, detail="short_name_unavailable")


async def configure_botfather_webapp(
    runtime: Any,
    bot_username: str | None = None,
    tunnel_url: str = "",
    *,
    bot_id: int = 0,
    short_name: str = "dashboard",
    title: str = "Hotaru",
    description: str = "Hotaru Kernel & Observatory Telemetry",
    poll_interval: float = 0.45,
    allow_remote_photo: bool = True,
) -> RelaySyncReport:
    with trusted_scope():
        if not tunnel_url.startswith("https://"):
            return RelaySyncReport(ok=False, detail="https_url_required")

        state = getattr(runtime, "state", None)
        clean_bot = (bot_username or "").lstrip("@").strip()
        resolved_token = ""

        if not clean_bot:
            ensured_bid, ensured_usr, ensured_tok = await ensure_inline_bot_ready(
                runtime, poll_interval=poll_interval
            )
            bot_id = bot_id or ensured_bid
            clean_bot = ensured_usr
            resolved_token = ensured_tok

        if not clean_bot:
            return RelaySyncReport(ok=False, detail="missing_bot_username")

        db_state = get_sanctuary_app_state(state)
        same_bot = bool(
            db_state.bot_username
            and db_state.bot_username.casefold() == clean_bot.casefold()
            and (not bot_id or not db_state.bot_id or db_state.bot_id == bot_id)
        )

        if db_state.registered and not same_bot:
            db_state = save_sanctuary_app_state(
                state,
                registered=False,
                bot_id=bot_id,
                bot_username=clean_bot,
                short_name=short_name,
                title=title,
            )

        effective_short = (
            db_state.short_name
            if (same_bot and db_state.short_name)
            else short_name
        )
        effective_title = db_state.title or title or "Hotaru"

        if (
            db_state.registered
            and same_bot
            and db_state.last_tunnel_url.rstrip("/") == tunnel_url.rstrip("/")
            and db_state.app_link
        ):
            return RelaySyncReport(
                ok=True,
                app_link=db_state.app_link,
                registered=True,
                bot_username=clean_bot,
                bot_token=resolved_token,
                short_name=effective_short,
                detail="already_up_to_date",
            )

        driver = _BotFatherRelayDriver(runtime, poll_interval=poll_interval)

        async with AsyncExitStack() as stack:
            if driver.is_goygram and BotFatherGuard is not None:
                try:
                    await stack.enter_async_context(BotFatherGuard(driver.app))
                except Exception as exc:
                    log.debug("BotFatherGuard skipped: %s", exc)

            if not await driver.prepare():
                return RelaySyncReport(ok=False, bot_username=clean_bot, detail="mt_unavailable")

            try:
                await driver.send_text("/cancel")
                await asyncio.sleep(driver.poll_interval)

                synced = await _sync_existing_via_myapps(
                    driver,
                    clean_bot=clean_bot,
                    tunnel_url=tunnel_url,
                    short_name=effective_short,
                    title=effective_title,
                )
                if synced is not None and synced.ok:
                    link_match = TME_APP_LINK_RE.search(synced.app_link or "")
                    resolved_short = (
                        link_match.group(2)
                        if link_match
                        else synced.short_name or effective_short
                    )
                    saved = save_sanctuary_app_state(
                        state,
                        registered=True,
                        bot_id=bot_id,
                        bot_username=clean_bot,
                        short_name=resolved_short,
                        title=effective_title,
                        app_link=synced.app_link or f"https://t.me/{clean_bot}/{resolved_short}",
                        last_tunnel_url=tunnel_url,
                    )
                    return RelaySyncReport(
                        ok=True,
                        app_link=saved.app_link,
                        registered=True,
                        bot_username=clean_bot,
                        bot_token=resolved_token,
                        short_name=resolved_short,
                        detail=synced.detail,
                    )
                if synced is not None and not synced.ok:
                    return synced

                if db_state.registered:
                    save_sanctuary_app_state(
                        state,
                        registered=False,
                        bot_id=bot_id,
                        bot_username=clean_bot,
                        short_name=short_name,
                        title=title,
                    )

                await driver.send_text("/cancel")
                await asyncio.sleep(driver.poll_interval)

                created = await _register_new_via_newapp(
                    driver,
                    clean_bot=clean_bot,
                    tunnel_url=tunnel_url,
                    short_name=short_name,
                    title=title or "Hotaru",
                    description=description,
                    allow_remote_photo=allow_remote_photo,
                )
                if created.ok:
                    link_match = TME_APP_LINK_RE.search(created.app_link or "")
                    resolved_short = (
                        link_match.group(2)
                        if link_match
                        else created.short_name or short_name
                    )
                    saved = save_sanctuary_app_state(
                        state,
                        registered=True,
                        bot_id=bot_id,
                        bot_username=clean_bot,
                        short_name=resolved_short,
                        title=title or "Hotaru",
                        app_link=created.app_link or f"https://t.me/{clean_bot}/{resolved_short}",
                        last_tunnel_url=tunnel_url,
                    )
                    return RelaySyncReport(
                        ok=True,
                        app_link=saved.app_link,
                        registered=True,
                        bot_username=clean_bot,
                        bot_token=resolved_token,
                        short_name=resolved_short,
                        detail="created_new",
                    )
                return created
            except Exception as exc:
                log.warning("BotFather Mini App sync failed: %s", exc)
                return RelaySyncReport(ok=False, bot_username=clean_bot, detail=str(exc))


async def update_bot_menu_button(runtime: Any, owner_id: int | None, tunnel_url: str) -> bool:
    if not tunnel_url.startswith("https://"):
        return False
    inline_mgr = getattr(runtime, "inline", None)
    bot_app = getattr(inline_mgr, "bot_app", None) if inline_mgr is not None else None
    app_obj = getattr(runtime, "app", None)
    legacy_bot = getattr(app_obj, "bot", None) if app_obj is not None else None

    try:
        with trusted_scope():
            if bot_app is not None and hasattr(bot_app, "mt_bots_set_bot_menu_button"):
                user_input: dict[str, Any] = {"_": "inputUserEmpty"}
                bot_mt = getattr(bot_app, "mt", None)
                if owner_id and bot_mt is not None:
                    ent = getattr(bot_mt, "entities", {}).get(("user", owner_id))
                    if isinstance(ent, dict) and ent.get("access_hash"):
                        user_input = {
                            "_": "inputUser",
                            "user_id": owner_id,
                            "access_hash": int(ent["access_hash"]),
                        }
                await bot_app.mt_bots_set_bot_menu_button(
                    user_id=user_input,
                    button={
                        "_": "botMenuButton",
                        "text": "Singularity",
                        "url": tunnel_url,
                    },
                )
                return True

            if legacy_bot is not None and hasattr(legacy_bot, "call"):
                await legacy_bot.call(
                    "bots.setBotMenuButton",
                    {
                        "user_id": {"_": "inputUserEmpty"},
                        "button": {
                            "_": "botMenuButton",
                            "text": "Singularity",
                            "url": tunnel_url,
                        },
                    },
                )
                return True
    except Exception as exc:
        log.debug("setBotMenuButton skipped: %s", exc)
    return False


async def sync_owner_menu_button(runtime: Any, tunnel_url: str) -> bool:
    cfg = getattr(runtime, "config", None)
    owner_id = getattr(runtime, "owner_id", None) or getattr(cfg, "owner_id", None)
    return await update_bot_menu_button(runtime, owner_id, tunnel_url)


WebAppDbState = SanctuaryAppState
BotFatherWebAppResult = RelaySyncReport
get_webapp_db_state = get_sanctuary_app_state
save_webapp_db_state = save_sanctuary_app_state
