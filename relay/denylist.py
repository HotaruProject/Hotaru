from __future__ import annotations

BLOCKED_HOSTS = ("my.telegram.org",)
BLOCKED_PEER_IDS = (777000,)
BLOCKED_PEER_STRINGS = ("+777000", "777000")


def is_blocked_host(hostname: str | None) -> bool:
    if not isinstance(hostname, str):
        return True
    host = hostname.strip().lower().rstrip(".")
    if not host:
        return True
    return host in BLOCKED_HOSTS or any(host.endswith("." + blocked) for blocked in BLOCKED_HOSTS)


def is_blocked_peer(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value in BLOCKED_PEER_IDS
    if isinstance(value, str):
        return value.strip() in BLOCKED_PEER_STRINGS
    return False


_PEER_ATTRS = ("user_id", "chat_id", "peer_id", "channel_id", "from_id")


def payload_hits_blocked(payload: object) -> bool:
    stack: list[object] = [payload]
    visited = 0
    while stack and visited < 4096:
        item = stack.pop()
        visited += 1
        if isinstance(item, dict):
            for value in item.values():
                if is_blocked_peer(value):
                    return True
                stack.append(value)
        elif isinstance(item, (list, tuple)):
            for value in item:
                if is_blocked_peer(value):
                    return True
                stack.append(value)
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            for attr in _PEER_ATTRS:
                value = getattr(item, attr, None)
                if is_blocked_peer(value):
                    return True
    return False
