from __future__ import annotations

import asyncio
import base64
import io
import sys
import time
from pathlib import Path
from typing import Any

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text


def _console() -> Console:
    return Console(highlight=False)


def _width(console: Console) -> int:
    return max(40, min(int(console.size.width) - 2, 72))


_ASCII_WIDE = """\
██╗  ██╗ ██████╗ ████████╗ █████╗ ██████╗ ██╗   ██╗
██║  ██║██╔═══██╗╚══██╔══╝██╔══██╗██╔══██╗██║   ██║
███████║██║   ██║   ██║   ███████║██████╔╝██║   ██║
██╔══██║██║   ██║   ██║   ██╔══██║██╔══██╗██║   ██║
██║  ██║╚██████╔╝   ██║   ██║  ██║██║  ██║╚██████╔╝
╚═╝  ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝"""

_ASCII_NARROW = r"""
 _   _  ___ _____ _   ___ _   _
| |_| |/ _ \_   _/ \ | _ \ | | |
|  _  | (_) || |/ _ \|   / |_| |
|_| |_|\___/ |_/_/ \_\___/ \___/""".strip()


def _banner(console: Console) -> None:
    w = _width(console)
    art = _ASCII_WIDE if w >= 54 else _ASCII_NARROW
    body = Text(art, style="bold #ff6b4a")
    body.append("\nsetup", style="dim")
    console.print(Panel(Align.center(body), width=w, border_style="#ff6b4a", padding=(1, 1)))


def _ask(console: Console, label: str, *, password: bool = False, default: str | None = None) -> str:
    kwargs: dict[str, Any] = {"password": password, "show_default": default is not None}
    if default is not None:
        kwargs["default"] = default
    return str(Prompt.ask(f"[#ff6b4a]{label}[/]", console=console, **kwargs)).strip()


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
    console = _console()
    _banner(console)
    console.print("[dim]my.telegram.org → API ID / hash[/]")
    api_id = int(_ask(console, "API ID"))
    api_hash = _ask(console, "API hash", password=True)
    prefix = _ask(console, "Prefix", default="!") or "!"
    bot = _ask(console, "Inline bot username (empty = auto)", default="")
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


def _save_vault(app: Any, vault: Path, session_name: str, api_id: int, api_hash: str, user: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
    from goygram.security import _current_dc_id, _extract_auth_blob, _field, _write_vault
    auth_blob = _extract_auth_blob(extra or {}) or getattr(app.mt, "auth_key", None)
    if user is None or auth_blob is None:
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


async def _phone_login(console: Console, app: Any, api_id: int, api_hash: str) -> dict[str, Any]:
    from goygram import ext as rx
    from goygram.security import _extract_error, _extract_phone_code_hash, _extract_user, _mt_req_with_migrate
    while True:
        raw = _ask(console, "Phone")
        try:
            phone = _phone(raw)
        except ValueError as exc:
            console.print(f"[red]{exc}[/]")
            continue
        settings = rx.serialize_constructor("codeSettings", {"flags": 0})
        sent = await _mt_req_with_migrate(app, "auth_send_code", phone_number=phone, api_id=api_id, api_hash=api_hash, settings=settings)
        err = _extract_error(sent) if isinstance(sent, dict) else str(sent)
        if err and "SESSION_PASSWORD_NEEDED" not in err:
            console.print(f"[red]{err}[/]")
            continue
        code_hash = _extract_phone_code_hash(sent) if isinstance(sent, dict) else None
        if not code_hash:
            console.print("[red]no code hash[/]")
            continue
        while True:
            code = _ask(console, "Code")
            try:
                sign = await _mt_req_with_migrate(app, "auth_sign_in", phone_number=phone, phone_code=code, phone_code_hash=code_hash, api_id=api_id, api_hash=api_hash)
                sign_err = ""
            except Exception as exc:
                sign, sign_err = None, str(exc)
            else:
                if not isinstance(sign, dict):
                    console.print("[red]bad signIn[/]")
                    continue
                sign_err = _extract_error(sign) or ""
            if "PHONE_CODE_INVALID" in sign_err or "CODE_INVALID" in sign_err:
                console.print("[red]wrong code[/]")
                continue
            final = sign
            if "SESSION_PASSWORD_NEEDED" in sign_err:
                while True:
                    pwd = _ask(console, "2FA password", password=True)
                    try:
                        check = await _mt_req_with_migrate(app, "auth_check_password", password=pwd, api_id=api_id, api_hash=api_hash)
                    except Exception as exc:
                        console.print(f"[red]{exc}[/]")
                        continue
                    err2 = _extract_error(check) if isinstance(check, dict) else "bad 2FA"
                    if err2:
                        console.print(f"[red]{err2}[/]")
                        continue
                    final = check
                    break
            elif sign_err:
                console.print(f"[red]{sign_err}[/]")
                continue
            user = _extract_user(final) if isinstance(final, dict) else None
            if not user:
                console.print("[red]no user in session[/]")
                continue
            return {"user": user, "raw": final if isinstance(final, dict) else {}}


async def _qr_login(console: Console, app: Any, api_id: int, api_hash: str) -> dict[str, Any] | None:
    from goygram.errors import GoyGramError
    from goygram.security import _extract_error, _extract_user, _mt_req_with_migrate
    w = _width(console)
    while True:
        try:
            res = await _mt_req_with_migrate(app, "auth_export_login_token", api_id=api_id, api_hash=api_hash, except_ids=[])
        except Exception as exc:
            console.print(f"[red]{exc}[/]")
            return None
        if not isinstance(res, dict) or not res.get("ok"):
            console.print(f"[red]{res}[/]")
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
            if w < 42 or "tg://login" in art:
                console.print(Panel(url, width=w, border_style="#ff6b4a", title="scan in Telegram"))
            else:
                console.print(Panel(Align.center(art), width=w, border_style="#ff6b4a", title="scan in Telegram"))
            expires = float(res.get("expires") or (time.time() + 30))
            app.mt.qr_update_ev.clear()
            while time.time() < expires:
                try:
                    await asyncio.wait_for(app.mt.qr_update_ev.wait(), timeout=max(0.2, expires - time.time()))
                except asyncio.TimeoutError:
                    break
                app.mt.qr_update_ev.clear()
                try:
                    poll = await _mt_req_with_migrate(app, "auth_export_login_token", api_id=api_id, api_hash=api_hash, except_ids=[])
                except GoyGramError as exc:
                    if "SESSION_PASSWORD_NEEDED" not in str(exc):
                        continue
                    while True:
                        pwd = _ask(console, "2FA password", password=True)
                        try:
                            check = await _mt_req_with_migrate(app, "auth_check_password", password=pwd, api_id=api_id, api_hash=api_hash)
                        except Exception as exc2:
                            console.print(f"[red]{exc2}[/]")
                            continue
                        err = _extract_error(check) if isinstance(check, dict) else "bad 2FA"
                        if err:
                            console.print(f"[red]{err}[/]")
                            continue
                        user = _extract_user(check)
                        if not user:
                            continue
                        return {"user": user, "raw": check if isinstance(check, dict) else {}}
                    break
                if isinstance(poll, dict) and poll.get("type") == "loginTokenSuccess":
                    user = _extract_user(poll)
                    if user:
                        return {"user": user, "raw": poll}
            continue
        if res.get("type") == "loginTokenSuccess":
            user = _extract_user(res)
            if user:
                return {"user": user, "raw": res}
        return None


async def sign_in(runtime: Any) -> dict[str, str]:
    console = _console()
    _banner(console)
    app = runtime.app.core
    config = runtime.config
    api_id = int(config.api_id)
    api_hash = str(config.api_hash)
    vault = runtime.app.session.path
    session_name = runtime.app.core.session_name
    if vault is None:
        raise RuntimeError("session path is missing")
    await app.mt.ensure_auth_key()
    method = Prompt.ask("[#ff6b4a]Login[/]", console=console, choices=["qr", "phone"], default="qr")
    packed = None
    if method == "qr":
        packed = await _qr_login(console, app, api_id, api_hash)
        if packed is None:
            console.print("[yellow]QR failed, phone login[/]")
    if packed is None:
        packed = await _phone_login(console, app, api_id, api_hash)
    _save_vault(app, vault, session_name, api_id, api_hash, packed["user"], packed.get("raw"))
    if hasattr(runtime.app, "session"):
        runtime.app.session.data = getattr(app, "session", runtime.app.session).data if getattr(app, "session", None) else packed["raw"]
        if not runtime.app.session.data:
            runtime.app.session.data = {
                "user": packed["user"],
                "auth_key": app.mt.auth_key.hex() if app.mt.auth_key else "",
            }
    console.print("[bold #ff6b4a]ok[/]")
    return {"source": "hotaru"}
