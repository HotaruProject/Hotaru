from __future__ import annotations

import contextlib
import contextvars
import os
import sys
from pathlib import Path
from typing import Any, Generator, cast

_active = contextvars.ContextVar("hotaru_module_firewall", default=False)
_trusted = contextvars.ContextVar("hotaru_module_trusted", default=False)
_owner = contextvars.ContextVar("hotaru_module_owner", default="")
_installed = False
_protected: set[str] = set()
_telegram_guard = False


def install_secure_random() -> Any:
    import secrets as _sec
    import random as _rnd
    sysrand = _sec.SystemRandom()
    for name in dir(sysrand):
        if not name.startswith("_") and callable(getattr(sysrand, name)):
            fn = getattr(sysrand, name)
            if not hasattr(_sec, name) or name == "choice":
                setattr(_sec, name, fn)
            setattr(_rnd, name, fn)
    setattr(_sec, "Random", _sec.SystemRandom)
    setattr(_rnd, "Random", _sec.SystemRandom)
    setattr(_sec, "seed", lambda *a, **kw: None)
    setattr(_rnd, "seed", lambda *a, **kw: None)
    for sec_name in ("token_bytes", "token_hex", "token_urlsafe", "randbelow", "randbits", "compare_digest"):
        if hasattr(_sec, sec_name):
            setattr(_rnd, sec_name, getattr(_sec, sec_name))
    sys.modules["random"] = _sec
    sys.modules["secrets"] = _sec
    return _sec


install_secure_random()


def guard_telegram(enabled: bool = True) -> None:
    global _telegram_guard, _installed
    _telegram_guard = enabled
    if enabled and not _installed:
        sys.addaudithook(_audit)
        _installed = True


def install(*paths: str | Path) -> None:
    global _installed
    for path in paths:
        try:
            candidate = Path(path).resolve()
            _protected.add(str(candidate))
            if candidate.suffix == ".vault":
                _protected.add(str(candidate))
        except OSError:
            _protected.add(str(path))
    if _installed:
        return
    sys.addaudithook(_audit)
    _installed = True


def _under_protected(path: str) -> bool:
    try:
        candidate = str(Path(path).resolve())
    except OSError:
        candidate = path
    return any(candidate == root or candidate.startswith(root + os.sep) for root in _protected)


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _telegram_guard and event == "socket.connect" and len(args) > 1 and isinstance(args[1], tuple):
        from .netproxy import is_telegram_address
        target = cast("tuple[Any, ...]", args[1])
        if target and isinstance(target[0], str) and is_telegram_address(target[0]):
            raise PermissionError("direct Telegram connection blocked: tg-proxy is configured")
    if not _active.get() or _trusted.get():
        return
    if event in {"open", "os.open"} and args and isinstance(args[0], (str, bytes)):
        value = os.fsdecode(args[0])
        if _under_protected(value) or value.endswith((".vault", ".session")):
            raise PermissionError("module access to Telegram session storage is denied")
    if event == "import" and args and _active.get():
        name = str(args[0]).split(".", 1)[0].casefold()
        if name in {"goygram", "hotaru", "relay"}:
            raise PermissionError("module cannot import Hotaru/GoyGram internals; use ctx proxies")
    if event == "socket.connect":
        raise PermissionError("direct network access is denied; use ctx.net or ctx.mt")


@contextlib.contextmanager
def module_scope(owner: str = "") -> Generator[None, None, None]:
    token = _active.set(True)
    owner_token = _owner.set(owner)
    try:
        yield
    finally:
        _owner.reset(owner_token)
        _active.reset(token)


def current_module() -> str:
    return _owner.get()


@contextlib.contextmanager
def trusted_scope() -> Generator[None, None, None]:
    token = _trusted.set(True)
    try:
        yield
    finally:
        _trusted.reset(token)
