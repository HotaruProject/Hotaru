from __future__ import annotations

import asyncio
import base64
import io
import importlib
import logging
import math
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

from rich.align import Align
from rich.console import Console
from rich.text import Text
from goygram.errors import RPCError

from . import clack


AUTH_TIMEOUT = 30.0
SESSION_ERRORS = frozenset({
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID",
    "AUTH_KEY_PERM_EMPTY",
    "AUTH_KEY_DUPLICATED",
    "SESSION_REVOKED",
    "SESSION_EXPIRED",
})

log = logging.getLogger(__name__)


async def check_session(app: Any) -> bool:
    try:
        await asyncio.wait_for(
            app.mt_users_get_users(id=[{"_": "inputUserSelf"}], retry=0),
            timeout=AUTH_TIMEOUT,
        )
    except RPCError as exc:
        if exc.message not in SESSION_ERRORS:
            raise
        mt = app.mt
        await asyncio.wait_for(mt.close(), timeout=AUTH_TIMEOUT)
        if app.session.path is not None:
            app.session.path.unlink(missing_ok=True)
        app.session.data.clear()
        app.core.self_id = 0
        mt.self_id = 0
        mt.auth_key = None
        mt.auth_ready.clear()
        mt.server_salt = b"\x00" * 8
        mt.session_id = secrets.token_bytes(8)
        mt.seq = 0
        mt._init_done = False
        mt.buf.clear()
        mt.dc_auth_keys.clear()
        mt.entities.clear()
        mt.entity_usernames.clear()
        log.warning("Session invalid; deleted. Log in again.")
        return False
    return True


def _console() -> Console:
    return Console(highlight=False)


def _width(console: Console) -> int:
    return max(20, min(int(console.size.width) - 2, 72))


_QUASAR_RAMP = " .·:-=+*oO#%@"
_QUASAR_STYLES = ["", "dim #8a4433", "#c05a3a", "#ff6b4a", "bold #ffb08a"]


def _quasar(columns: int) -> Text:
    cols = max(24, columns)
    rows = max(8, round(cols / 4.4))
    cx = (cols - 1) / 2
    cy = (rows - 1) / 2
    half = cols / 2
    shadow = max(0.13, 2.2 / half)
    ring = max(0.05, 1.1 / half)
    jet = max(0.030, 0.7 / half)
    thick = max(0.14, 1.8 / half)
    body = Text()
    for row in range(rows):
        dy = (row - cy) * 2
        ny = (row - cy) / max(1.0, rows / 2)
        cells: list[tuple[str, int]] = []
        for col in range(cols):
            dx = col - cx
            nx = dx / half
            r = math.hypot(dx, dy) / half
            theta = math.atan2(dy, dx)
            b = 0.0

            warped = dy + 0.07 * nx
            dl = math.hypot(dx, warped / thick) / half
            in_disk = dl < 1.0
            base = 0.0
            if in_disk:
                base = (1.0 - dl) ** 0.55
                base *= 0.75 + 0.25 * math.cos(2 * theta + 5.0 * dl)
                base *= 1.0 if dx > 0 else 0.82
                if dl < 0.35:
                    base *= 1.3
                b = max(b, base)

            if r < shadow:
                b = max(b, base * 0.9) if (in_disk and warped > 0) else 0.0
            elif r < shadow + ring:
                b = max(b, 0.9 - 0.5 * (r - shadow) / ring)

            if abs(ny) > 0.22:
                wob = 0.02 * math.sin(6.0 * ny)
                width = jet + 0.55 * jet * abs(ny)
                if abs(nx - wob) < width:
                    b = max(b, (0.95 - 0.6 * abs(ny)) * (1 - abs(nx - wob) / width))

            dust = ((col * 7919 + row * 104729) % 997) / 997
            if b < 0.13 and 0.3 < r < 1.05 and dust < 0.035 * (1.05 - r) * min(1.0, cols / 48):
                b = max(b, 0.10)

            if b <= 0.06:
                cells.append((" ", 0))
                continue
            level = 1 if b < 0.22 else 2 if b < 0.42 else 3 if b < 0.68 else 4
            cells.append((_QUASAR_RAMP[min(len(_QUASAR_RAMP) - 1, max(1, int(b * (len(_QUASAR_RAMP) - 1))))], level))

        run, run_level = "", 0
        for char, level in cells:
            if level != run_level:
                if run:
                    body.append(run, style=_QUASAR_STYLES[run_level])
                run, run_level = "", level
            run += char
        if run:
            body.append(run, style=_QUASAR_STYLES[run_level])
        if row < rows - 1:
            body.append("\n")
    return body


def _banner(console: Console) -> None:
    console.print(Align.center(_quasar(_width(console))))
    console.print(Align.center(Text("Hotaru Userbot", style="dim")))


def _ask(
    label: str,
    *,
    password: bool = False,
    default: str | None = None,
    placeholder: str | None = None,
    validate: Callable[[str], str | None] | None = None,
) -> str:
    if password:
        return str(clack.password(label, validate=validate)).strip()
    return str(clack.text(label, placeholder=placeholder, default_value=default, validate=validate)).strip()


async def _spin(message: str, awaitable: Awaitable[Any]) -> Any:
    spin = clack.spinner()
    spin.start(message)
    task = asyncio.ensure_future(awaitable)
    try:
        with clack.Raw() as keys:
            while not task.done():
                spin.tick()
                if clack.cancel_pressed(keys):
                    spin.cancel("Cancelled.")
                    task.cancel()
                    raise clack.Cancelled
                await asyncio.sleep(0.02)
        result = task.result()
    except BaseException:
        spin.clear()
        raise
    spin.stop(message)
    return result


def _api_id_problem(raw: str) -> str | None:
    return None if raw.strip().isdigit() else "API ID is a number, e.g. 1234567"


def _api_hash_problem(raw: str) -> str | None:
    return None if len(raw.strip()) >= 16 else "API hash looks too short"


def _prefix_problem(raw: str) -> str | None:
    return None if len(raw) <= 1 else "Prefix is a single character"


def _code_problem(raw: str) -> str | None:
    return None if raw.strip().isdigit() else "Telegram codes are digits"


def _phone_problem(raw: str) -> str | None:
    try:
        _phone(raw)
    except ValueError as exc:
        return str(exc)
    return None


def _phone(raw: str) -> str:
    text = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    if not text.startswith("+"):
        text = "+" + text.lstrip("+")
    if len(text) < 8:
        raise ValueError("phone is too short")
    return text


def collect_settings(state: Any) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("runtime settings are missing; run from an interactive TTY")
    _banner(_console())
    clack.intro(clack.style("bgCyan", clack.style("black", " hotaru setup ")))
    clack.log.info("my.telegram.org → API ID / hash")
    api_id = int(_ask("API ID", placeholder="1234567", validate=_api_id_problem))
    api_hash = _ask("API hash", password=True, validate=_api_hash_problem)
    prefix = _ask("Prefix", placeholder="!", default="!", validate=_prefix_problem) or "!"
    bot = _ask("Inline bot username", placeholder="empty = auto")
    import secrets
    values = {
        "api-id": api_id,
        "api-hash": api_hash,
        "bot-token": None,
        "owner-id": None,
        "prefix": prefix[:1],
        "session-name": f"hotaru-pending-{secrets.token_hex(8)}",
        "session-dir": ".",
        "backup-keep": 7,
    }
    for key, value in values.items():
        state.set_setting(key, value)
    wanted = bot.lstrip("@")
    if wanted:
        if not wanted.lower().endswith("bot"):
            wanted += "_bot" if "_" in wanted or wanted.isalnum() else "bot"
        state.set_setting("inline-bot-username-wanted", wanted)
    clack.note(
        f"API ID     {api_id}\nprefix     {prefix}\ninline bot {wanted or 'auto'}\nsession    {values['session-dir']}",
        "settings saved",
    )
    clack.outro("Starting Hotaru.")


def save_vault(app: Any, vault: Path, session_name: str, api_id: int, api_hash: str, user: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
    security = importlib.import_module("goygram.security")
    _current_dc_id = cast(Callable[[Any], int | None], getattr(security, "_current_dc_id"))
    _extract_auth_blob = cast(Callable[[dict[str, Any]], bytes | None], getattr(security, "_extract_auth_blob"))
    _field = cast(Callable[..., Any], getattr(security, "_field"))
    _write_vault = cast(Callable[[Path, dict[str, Any], str], None], getattr(security, "_write_vault"))
    auth_blob = _extract_auth_blob(extra or {}) or getattr(app.mt, "auth_key", None)
    if auth_blob is None:
        raise RuntimeError("authorization did not return a session")
    payload = {
        "phone": user.get("phone", ""),
        "user": user,
        "auth_key": auth_blob.hex() if isinstance(auth_blob, (bytes, bytearray)) else str(auth_blob),
        "server_salt": app.mt.server_salt.hex() if getattr(app.mt, "server_salt", None) else "",
        "dc": _field(extra or {}, "dc_id", "dc") or _current_dc_id(app),
        "api_id": api_id,
        "api_hash": api_hash,
    }
    _write_vault(vault, payload, Path(session_name).name)
    uid = user.get("id", 0)
    if uid:
        app.self_id = uid
        app.mt.self_id = uid
    if hasattr(app, "session") and getattr(app, "session", None) is not None:
        app.session.data = payload


async def _phone_login(app: Any, api_id: int, api_hash: str) -> dict[str, Any]:
    from goygram import ext as rx
    security = importlib.import_module("goygram.security")
    _extract_error = cast(Callable[[dict[str, Any]], str | None], getattr(security, "_extract_error"))
    _extract_phone_code_hash = cast(Callable[[dict[str, Any]], str | None], getattr(security, "_extract_phone_code_hash"))
    _extract_user = cast(Callable[[Any], dict[str, Any] | None], getattr(security, "_extract_user"))
    _mt_req_with_migrate = cast(Callable[..., Awaitable[dict[str, Any]]], getattr(security, "_mt_req_with_migrate"))
    while True:
        phone = _phone(_ask("Phone number", placeholder="+1 555 000 1234", validate=_phone_problem))
        settings = rx.serialize_constructor("codeSettings", {"flags": 0})
        sent = await _spin("Sending the code", _mt_req_with_migrate(app, "auth_send_code", phone_number=phone, api_id=api_id, api_hash=api_hash, settings=settings))
        err = _extract_error(sent)
        if err and "SESSION_PASSWORD_NEEDED" not in err:
            clack.log.error(err)
            continue
        code_hash = _extract_phone_code_hash(sent)
        if not code_hash:
            clack.log.error("no code hash")
            continue
        while True:
            code = _ask("Code", placeholder="12345", validate=_code_problem)
            try:
                sign = await _spin("Signing in", _mt_req_with_migrate(app, "auth_sign_in", phone_number=phone, phone_code=code, phone_code_hash=code_hash, api_id=api_id, api_hash=api_hash))
                sign_err = ""
            except Exception as exc:
                sign, sign_err = None, str(exc)
            else:
                sign_err = _extract_error(sign) or ""
            if "PHONE_CODE_INVALID" in sign_err or "CODE_INVALID" in sign_err:
                clack.log.error("wrong code")
                continue
            final = sign
            if "SESSION_PASSWORD_NEEDED" in sign_err:
                while True:
                    pwd = _ask("2FA password", password=True)
                    try:
                        check = await _spin("Checking the password", _mt_req_with_migrate(app, "auth_check_password", password=pwd, api_id=api_id, api_hash=api_hash))
                    except Exception as exc:
                        clack.log.error(str(exc))
                        continue
                    err2 = _extract_error(check)
                    if err2:
                        clack.log.error(err2)
                        continue
                    final = check
                    break
            elif sign_err:
                clack.log.error(sign_err)
                continue
            user = _extract_user(final)
            if not user:
                clack.log.error("no user in session")
                continue
            return {"user": user, "raw": final}


async def _qr_login(app: Any, api_id: int, api_hash: str) -> dict[str, Any] | None:
    from goygram.errors import GoyGramError
    security = importlib.import_module("goygram.security")
    _extract_error = cast(Callable[[dict[str, Any]], str | None], getattr(security, "_extract_error"))
    _extract_user = cast(Callable[[Any], dict[str, Any] | None], getattr(security, "_extract_user"))
    _mt_req_with_migrate = cast(Callable[..., Awaitable[dict[str, Any]]], getattr(security, "_mt_req_with_migrate"))
    while True:
        try:
            res = await _spin("Requesting a login token", _mt_req_with_migrate(app, "auth_export_login_token", api_id=api_id, api_hash=api_hash, except_ids=[]))
        except Exception as exc:
            clack.log.error(str(exc))
            return None
        if not res.get("ok"):
            clack.log.error(str(res))
            return None
        if res.get("type") == "loginToken":
            token = res["token"]
            b64 = base64.urlsafe_b64encode(token).decode().rstrip("=")
            url = f"tg://login?token={b64}"
            art = url
            try:
                import qrcode
                buf = io.StringIO()
                qr = qrcode.QRCode(border=1)
                qr.add_data(url)
                qr.print_ascii(out=buf)
                art = buf.getvalue()
            except Exception:
                pass
            clack.note(url if "tg://login" in art else art, "scan in Telegram")
            expires = float(res.get("expires") or (time.time() + 30))
            app.mt.qr_update_ev.clear()
            spin = clack.spinner()
            spin.start("Waiting for the scan")
            with clack.Raw() as keys:
                try:
                    while time.time() < expires:
                        try:
                            await asyncio.wait_for(app.mt.qr_update_ev.wait(), timeout=0.1)
                        except asyncio.TimeoutError:
                            spin.tick()
                            if clack.cancel_pressed(keys):
                                spin.cancel("Cancelled.")
                                raise clack.Cancelled
                            continue
                        spin.tick()
                        app.mt.qr_update_ev.clear()
                        try:
                            poll = await _mt_req_with_migrate(app, "auth_export_login_token", api_id=api_id, api_hash=api_hash, except_ids=[])
                        except GoyGramError as exc:
                            if "SESSION_PASSWORD_NEEDED" not in str(exc):
                                continue
                            spin.stop("Scanned")
                            while True:
                                pwd = _ask("2FA password", password=True)
                                try:
                                    check = await _spin("Checking the password", _mt_req_with_migrate(app, "auth_check_password", password=pwd, api_id=api_id, api_hash=api_hash))
                                except Exception as exc2:
                                    clack.log.error(str(exc2))
                                    continue
                                err = _extract_error(check)
                                if err:
                                    clack.log.error(err)
                                    continue
                                user = _extract_user(check)
                                if not user:
                                    continue
                                return {"user": user, "raw": check}
                            break
                        if poll.get("type") == "loginTokenSuccess":
                            user = _extract_user(poll)
                            if user:
                                spin.stop("Scanned")
                                return {"user": user, "raw": poll}
                    spin.stop("Code expired, new one")
                finally:
                    spin.clear()
            continue
        if res.get("type") == "loginTokenSuccess":
            user = _extract_user(res)
            if user:
                return {"user": user, "raw": res}
        return None


async def sign_in(runtime: Any) -> dict[str, str]:
    _banner(_console())
    clack.intro(clack.style("bgCyan", clack.style("black", " hotaru login ")))
    app = runtime.app.core
    config = runtime.config
    api_id = int(config.api_id)
    api_hash = str(config.api_hash)
    vault = runtime.app.session.path
    session_name = runtime.app.core.session_name
    if vault is None:
        raise RuntimeError("session path is missing")
    await _spin("Connecting to Telegram", app.mt.ensure_auth_key())
    method = clack.select(
        "How do you want to sign in?",
        [
            {"value": "qr", "label": "QR code", "hint": "scan with the Telegram app"},
            {"value": "phone", "label": "Phone number"},
        ],
        initial="qr",
    )
    packed = None
    if method == "qr":
        packed = await _qr_login(app, api_id, api_hash)
        if packed is None:
            clack.log.warn("QR failed, phone login")
    if packed is None:
        packed = await _phone_login(app, api_id, api_hash)
    from .accounts import parse_user_id
    uid = parse_user_id(config.session_name)
    if uid is not None and packed["user"].get("id") != uid:
        raise RuntimeError(f"Wrong account; log in as user {uid}.")
    save_vault(app, vault, session_name, api_id, api_hash, packed["user"], packed.get("raw"))
    if hasattr(runtime.app, "session"):
        runtime.app.session.data = getattr(app, "session", runtime.app.session).data if getattr(app, "session", None) else packed["raw"]
        if not runtime.app.session.data:
            runtime.app.session.data = {
                "user": packed["user"],
                "auth_key": app.mt.auth_key.hex() if app.mt.auth_key else "",
            }
    clack.outro("Signed in. Starting Hotaru.")
    return {"source": "hotaru"}
