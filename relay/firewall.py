from __future__ import annotations

import contextlib
import contextvars
import os
import socket
import sys
from pathlib import Path
from typing import Any, Iterator

_active = contextvars.ContextVar("hotaru_module_firewall", default=False)
_trusted = contextvars.ContextVar("hotaru_module_trusted", default=False)
_installed = False
_protected: set[str] = set()


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
def module_scope() -> Iterator[None]:
    token = _active.set(True)
    try:
        yield
    finally:
        _active.reset(token)


@contextlib.contextmanager
def trusted_scope() -> Iterator[None]:
    token = _trusted.set(True)
    try:
        yield
    finally:
        _trusted.reset(token)
