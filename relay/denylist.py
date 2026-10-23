from __future__ import annotations

from typing import Callable, cast
from goygram.ext import loads

BLOCKED_HOSTS = ("my.telegram.org",)
BLOCKED_PEER_IDS = ()
BLOCKED_PEER_STRINGS = ()

READONLY_HOSTS = ("my.telegram.org",)
READONLY_PEER_IDS = (777000,)
READONLY_PEER_STRINGS = ("+777000", "777000")


def _host_in(host: str, hosts: tuple[str, ...]) -> bool:
    return host in hosts or any(host.endswith("." + blocked) for blocked in hosts)


def is_blocked_host(hostname: str | None) -> bool:
    if not isinstance(hostname, str):
        return True
    try:
        host = hostname.strip().encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return True
    if not host:
        return True
    return _host_in(host, BLOCKED_HOSTS)


def is_readonly_host(hostname: str | None) -> bool:
    if not isinstance(hostname, str):
        return True
    try:
        host = hostname.strip().encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return True
    if not host:
        return True
    return _host_in(host, READONLY_HOSTS)


def _peer_in(value: object, ids: tuple[int, ...], strings: tuple[str, ...]) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value in ids
    if isinstance(value, str):
        value = value.strip()
        try:
            return int(value) in ids
        except ValueError:
            return value in strings
    return False


def is_blocked_peer(value: object) -> bool:
    return _peer_in(value, BLOCKED_PEER_IDS, BLOCKED_PEER_STRINGS)


def is_readonly_peer(value: object) -> bool:
    return _peer_in(value, READONLY_PEER_IDS, READONLY_PEER_STRINGS)


_PEER_ATTRS = frozenset({"peer", "peers", "user", "users", "user_id", "user_ids", "chat_id", "peer_id", "from_id", "to_id", "to_peer", "from_peer", "saved_peer_id", "reply_to_peer_id", "receiver_id", "bot", "bot_id"})


def _payload_hits(payload: object, checker: Callable[[object], bool]) -> bool:
    stack: list[tuple[object, bool]] = [(payload, False)]
    visited = 0
    seen: set[tuple[int, bool]] = set()
    while stack:
        item, peer = stack.pop()
        visited += 1
        if visited + len(stack) > 4096:
            raise ValueError("Telegram payload complexity limit exceeded")
        if peer and checker(item):
            return True
        if item is None or isinstance(item, (str, int, float, bool)):
            continue
        identity = (id(item), peer)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(item, dict):
            fields = cast('dict[object, object]', item)
            kind = str(fields.get("_", "")).lower()
            for key, value in fields.items():
                target = key in _PEER_ATTRS or key == "id" and kind in {"user", "userempty"}
                if key == "chat_id" and kind in {"peerchat", "inputpeerchat"}:
                    target = False
                stack.append((value, target))
                if visited + len(stack) > 4096:
                    raise ValueError("Telegram payload complexity limit exceeded")
        elif isinstance(item, (list, tuple)):
            values = cast('list[object] | tuple[object, ...]', item)
            if visited + len(stack) + len(values) > 4096:
                raise ValueError("Telegram payload complexity limit exceeded")
            stack.extend((value, peer) for value in values)
        elif isinstance(item, (bytes, bytearray)):
            try:
                decoded = loads(bytes(item))
            except (ValueError, TypeError, RuntimeError):
                continue
            stack.append((decoded, peer))
        else:
            fields = getattr(item, "__dict__", None)
            if isinstance(fields, dict):
                stack.append((cast('dict[object, object]', fields), peer))
            for attr in _PEER_ATTRS:
                attr_value: object = getattr(item, attr, None)
                stack.append((attr_value, True))
    return False


def payload_hits_blocked(payload: object) -> bool:
    return _payload_hits(payload, is_blocked_peer)


def payload_hits_readonly(payload: object) -> bool:
    return _payload_hits(payload, is_readonly_peer)
