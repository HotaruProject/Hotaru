from __future__ import annotations

import base64
import ipaddress
import socket
import struct
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from .denylist import BLOCKED_HOSTS, is_blocked_host

PROXY_SCHEMES = frozenset({"socks4", "socks4a", "socks5", "socks5h", "http"})
TELEGRAM_PROXY_SCHEMES = frozenset({"socks5", "socks5h", "http"})
TELEGRAM_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "91.105.192.0/23", "91.108.4.0/22", "91.108.8.0/22", "91.108.12.0/22", "91.108.16.0/22",
    "91.108.20.0/22", "91.108.56.0/22", "95.161.64.0/20", "149.154.160.0/20", "185.76.151.0/24",
    "2001:67c:4e8::/48", "2001:b28:f23c::/47", "2001:b28:f23f::/48", "2a0a:f280::/32",
))
_HEADER_LIMIT = 16384

Resolver = Callable[[str, int], List[str]]
Address = Tuple[str, int]
Step = Tuple["ProxySpec", str, int]


@dataclass(frozen=True)
class ProxySpec:
    scheme: str
    host: str
    port: int
    username: Optional[str] = field(default=None, repr=False)
    password: Optional[str] = field(default=None, repr=False)

    def redacted(self) -> str:
        auth = "***@" if self.username or self.password else ""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{auth}{host}:{self.port}"

    def __str__(self) -> str:
        return self.redacted()


def parse_proxy(value: object, schemes: frozenset[str] = PROXY_SCHEMES) -> ProxySpec:
    if isinstance(value, ProxySpec):
        if value.scheme not in schemes:
            raise ValueError(f"unsupported proxy scheme: {value.scheme}")
        return value
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("proxy must be a URL string")
    if any(ch.isspace() or not ch.isprintable() for ch in value):
        raise ValueError("proxy URL must not contain whitespace or control characters")
    parsed = urllib.parse.urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in schemes:
        raise ValueError(f"unsupported proxy scheme: {scheme or 'none'} (use {', '.join(sorted(schemes))})")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("proxy URL must not contain a path, query or fragment")
    host = parsed.hostname
    if not host:
        raise ValueError("proxy URL must contain a host")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("proxy URL contains an invalid port") from None
    if not port:
        raise ValueError("proxy URL must contain a port")
    try:
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        raise ValueError("proxy host is invalid") from None
    username = urllib.parse.unquote(parsed.username) if parsed.username is not None else None
    password = urllib.parse.unquote(parsed.password) if parsed.password is not None else None
    for secret in (username, password):
        if secret is not None and (len(secret.encode("utf-8")) > 255 or "\0" in secret):
            raise ValueError("proxy credentials are too long or invalid")
    if scheme in {"socks4", "socks4a"} and password:
        raise ValueError("socks4 proxies support only a user id, not a password")
    return ProxySpec(scheme, host, port, username or None, password or None)


def is_telegram_address(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in TELEGRAM_NETWORKS)


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("proxy closed the connection during handshake")
        data.extend(chunk)
    return bytes(data)


def _socks5(sock: socket.socket, spec: ProxySpec, host: str, port: int) -> None:
    methods = b"\x00\x02" if spec.username or spec.password else b"\x00"
    sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
    version, method = _recv_exact(sock, 2)
    if version != 5 or method == 0xFF:
        raise ConnectionError("SOCKS5 proxy rejected the authentication methods")
    if method == 0x02:
        user = (spec.username or "").encode("utf-8")
        secret = (spec.password or "").encode("utf-8")
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret)
        if _recv_exact(sock, 2)[1] != 0:
            raise ConnectionError("SOCKS5 proxy authentication failed")
    elif method != 0x00:
        raise ConnectionError("SOCKS5 proxy selected an unsupported method")
    if _is_ip(host):
        addr = ipaddress.ip_address(host)
        target = (b"\x01" if addr.version == 4 else b"\x04") + addr.packed
    else:
        encoded = host.encode("idna")
        if len(encoded) > 255:
            raise ValueError("SOCKS5 target host is too long")
        target = b"\x03" + bytes([len(encoded)]) + encoded
    sock.sendall(b"\x05\x01\x00" + target + struct.pack(">H", port))
    version, status, _, atyp = _recv_exact(sock, 4)
    if version != 5 or status != 0:
        raise ConnectionError(f"SOCKS5 proxy refused the connection (code {status})")
    if atyp == 1:
        _recv_exact(sock, 4 + 2)
    elif atyp == 4:
        _recv_exact(sock, 16 + 2)
    elif atyp == 3:
        _recv_exact(sock, _recv_exact(sock, 1)[0] + 2)
    else:
        raise ConnectionError("SOCKS5 proxy sent an invalid reply")


def _socks4(sock: socket.socket, spec: ProxySpec, host: str, port: int) -> None:
    user = (spec.username or "").encode("utf-8") + b"\x00"
    if _is_ip(host):
        addr = ipaddress.ip_address(host)
        if addr.version != 4:
            raise ValueError("SOCKS4 proxies support only IPv4 targets")
        request = b"\x04\x01" + struct.pack(">H", port) + addr.packed + user
    elif spec.scheme == "socks4a":
        request = b"\x04\x01" + struct.pack(">H", port) + b"\x00\x00\x00\x01" + user + host.encode("idna") + b"\x00"
    else:
        raise ValueError("SOCKS4 proxies need an IPv4 target")
    sock.sendall(request)
    reply = _recv_exact(sock, 8)
    if reply[1] != 0x5A:
        raise ConnectionError(f"SOCKS4 proxy refused the connection (code {reply[1]})")


def _http_connect(sock: socket.socket, spec: ProxySpec, host: str, port: int) -> None:
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    lines = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
    if spec.username or spec.password:
        token = base64.b64encode(f"{spec.username or ''}:{spec.password or ''}".encode("utf-8")).decode("ascii")
        lines.append(f"Proxy-Authorization: Basic {token}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
    head = bytearray()
    while not head.endswith(b"\r\n\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("HTTP proxy closed the connection during CONNECT")
        head.extend(chunk)
        if len(head) > _HEADER_LIMIT:
            raise ConnectionError("HTTP proxy response headers are too large")
    status_line = bytes(head).split(b"\r\n", 1)[0].decode("latin-1")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or parts[1] != "200":
        raise ConnectionError(f"HTTP proxy CONNECT failed: {status_line[:120]}")


def tunnel(sock: socket.socket, spec: ProxySpec, host: str, port: int) -> None:
    if spec.scheme in {"socks5", "socks5h"}:
        _socks5(sock, spec, host, port)
    elif spec.scheme in {"socks4", "socks4a"}:
        _socks4(sock, spec, host, port)
    elif spec.scheme == "http":
        _http_connect(sock, spec, host, port)
    else:
        raise ValueError(f"unsupported proxy scheme: {spec.scheme}")


def dial(entries: Sequence[Address], steps: Sequence[Step], timeout: Optional[float]) -> socket.socket:
    if not entries:
        raise OSError("network target could not be resolved")
    error: OSError = OSError("network target is unreachable")
    for entry in entries:
        try:
            sock = socket.create_connection(entry, timeout=timeout)
        except OSError as exc:
            error = exc
            continue
        try:
            for spec, host, port in steps:
                tunnel(sock, spec, host, port)
        except BaseException:
            sock.close()
            raise
        return sock
    raise error


def _system_resolve(host: str, port: int) -> List[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)]


class NetPolicy:
    def __init__(self, default: Optional[ProxySpec] = None, *, resolve: Resolver = _system_resolve, blocked_ttl: float = 300.0) -> None:
        self.default = default
        self._resolve = resolve
        self._blocked_ttl = blocked_ttl
        self._blocked: frozenset[str] = frozenset()
        self._blocked_at = 0.0
        self._lock = threading.Lock()

    def blocked_addresses(self) -> frozenset[str]:
        with self._lock:
            if time.monotonic() - self._blocked_at < self._blocked_ttl and self._blocked_at:
                return self._blocked
        found: set[str] = set()
        for host in BLOCKED_HOSTS:
            try:
                found.update(_system_resolve(host, 443))
            except OSError:
                pass
        with self._lock:
            self._blocked = frozenset(found) | self._blocked
            self._blocked_at = time.monotonic()
            return self._blocked

    def public_addresses(self, host: str, port: int) -> List[str]:
        if is_blocked_host(host):
            raise PermissionError("network target is a denied host")
        try:
            addresses = self._resolve(host, port)
        except PermissionError:
            raise
        except Exception as exc:
            raise PermissionError("network target could not be resolved") from exc
        if not addresses:
            raise PermissionError("network target could not be resolved")
        blocked = self.blocked_addresses()
        for value in addresses:
            try:
                addr = ipaddress.ip_address(value.split("%", 1)[0])
            except ValueError:
                raise PermissionError("network target resolved to an invalid address") from None
            if not addr.is_global or addr.is_multicast:
                raise PermissionError("network target resolves to a non-public address")
            if str(addr) in blocked:
                raise PermissionError("network target is a denied host")
        return addresses

    def route(self, requested: object = None) -> Tuple[ProxySpec, ...]:
        hops: List[ProxySpec] = []
        if self.default is not None:
            hops.append(self.default)
        if requested is not None and requested != "":
            spec = parse_proxy(requested)
            if is_blocked_host(spec.host):
                raise PermissionError("proxy host is denied")
            hops.append(spec)
        return tuple(hops)

    def plan(self, hops: Sequence[ProxySpec], host: str, port: int) -> Tuple[List[Address], List[Step]]:
        targets = self.public_addresses(host, port)
        if not hops:
            return [(addr, port) for addr in targets], []
        first = hops[0]
        if first is self.default:
            try:
                entry_hosts = _system_resolve(first.host, first.port)
            except OSError as exc:
                raise OSError(f"proxy {first.redacted()} could not be resolved") from exc
        else:
            entry_hosts = self.public_addresses(first.host, first.port)
        steps: List[Step] = []
        for current, following in zip(hops, hops[1:]):
            steps.append((current, self.public_addresses(following.host, following.port)[0], following.port))
        steps.append((hops[-1], targets[0], port))
        return [(addr, first.port) for addr in entry_hosts], steps

    def open(self, hops: Sequence[ProxySpec], host: str, port: int, timeout: Optional[float] = None) -> socket.socket:
        entries, steps = self.plan(hops, host, port)
        try:
            return dial(entries, steps, timeout)
        except PermissionError:
            raise
        except OSError as exc:
            if not hops:
                raise
            raise ConnectionError(f"proxy route failed: {type(exc).__name__}: {exc}") from None
