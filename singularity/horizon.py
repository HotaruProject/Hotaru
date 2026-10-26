from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shutil
import socket
import ssl
import stat
import urllib.parse
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

try:
    import hotaru as _hotaru_pkg
except Exception:
    _hotaru_pkg = None

try:
    from relay.firewall import trusted_scope
except Exception:
    trusted_scope = nullcontext

log = logging.getLogger("hotaru.singularity.horizon")

QUICK_TUNNEL_RE = re.compile(
    r"https://(?!api\.)([a-z0-9-]+)\.trycloudflare\.com", re.IGNORECASE
)


def _resolve_release_asset_name() -> str:
    sys_name = platform.system().lower()
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise RuntimeError(f"Unsupported architecture for cloudflared: {machine}")

    if sys_name == "linux":
        return f"cloudflared-linux-{arch}"
    raise RuntimeError(f"Auto-download of cloudflared is supported on Linux (got {sys_name})")


def _https_get_stream_to_file(url: str, tmp_path: Path, timeout: float = 60.0) -> None:
    current_url = url
    with trusted_scope():
        tls_ctx = ssl.create_default_context()
        tls_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        for _ in range(6):
            parsed = urllib.parse.urlsplit(current_url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("Unsupported download URL scheme")
            host = parsed.hostname
            port = parsed.port or 443
            path_part = parsed.path or "/"
            if parsed.query:
                path_part = f"{path_part}?{parsed.query}"
            req_bytes = (
                f"GET {path_part} HTTP/1.0\r\n"
                f"Host: {host}\r\n"
                "User-Agent: Hotaru-Singularity/1.0\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            with socket.create_connection((host, port), timeout=timeout) as raw_sock:
                with tls_ctx.wrap_socket(raw_sock, server_hostname=host) as tls_sock:
                    tls_sock.sendall(req_bytes)
                    header_buf = bytearray()
                    while b"\r\n\r\n" not in header_buf:
                        chunk = tls_sock.recv(4096)
                        if not chunk:
                            break
                        header_buf.extend(chunk)
                        if len(header_buf) > 65536:
                            raise RuntimeError("Response headers too large")
                    sep_idx = header_buf.find(b"\r\n\r\n")
                    if sep_idx < 0:
                        raise RuntimeError("Malformed HTTP response")
                    head_text = bytes(header_buf[:sep_idx]).decode("iso-8859-1", errors="replace")
                    body_prefix = bytes(header_buf[sep_idx + 4 :])
                    lines = head_text.split("\r\n")
                    status_parts = lines[0].split(" ", 2)
                    status = int(status_parts[1]) if len(status_parts[1:]) >= 1 and status_parts[1].isdigit() else 0
                    headers_map: dict[str, str] = {}
                    for hline in lines[1:]:
                        if ":" in hline:
                            hk, hv = hline.split(":", 1)
                            headers_map[hk.strip().lower()] = hv.strip()
                    if status in (301, 302, 303, 307, 308):
                        loc = headers_map.get("location", "")
                        if not loc:
                            raise RuntimeError("Redirect missing Location header")
                        current_url = urllib.parse.urljoin(current_url, loc)
                        continue
                    if status != 200:
                        raise RuntimeError(f"Download failed with HTTP {status}")
                    with open(tmp_path, "wb") as out:
                        if body_prefix:
                            out.write(body_prefix)
                        while True:
                            buf = tls_sock.recv(65536)
                            if not buf:
                                break
                            out.write(buf)
                    return
    raise RuntimeError("Too many redirects while downloading cloudflared")


def _download_binary_sync(url: str, destination: Path) -> None:
    with trusted_scope():
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = destination.with_suffix(".download")
        _https_get_stream_to_file(url, tmp_path, timeout=60.0)
        tmp_path.chmod(tmp_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        tmp_path.replace(destination)


async def ensure_cloudflared_binary(cache_dir: Path | None = None) -> Path:
    with trusted_scope():
        env_bin = os.environ.get("CLOUDFLARED_BIN", "").strip()
        if env_bin:
            candidate = Path(env_bin).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate

        which_bin = shutil.which("cloudflared")
        if which_bin:
            return Path(which_bin)

        base_dir = cache_dir or (Path.home() / ".hotaru" / "bin")
        cached_bin = base_dir / "cloudflared"
        if cached_bin.is_file() and os.access(cached_bin, os.X_OK):
            return cached_bin

        asset = _resolve_release_asset_name()
        release_url = (
            f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
        )
        log.info("Downloading cloudflared binary (%s) to %s", asset, cached_bin)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _download_binary_sync, release_url, cached_bin)
        return cached_bin


class HorizonTunnel:
    def __init__(
        self,
        local_port: int,
        *,
        local_host: str = "127.0.0.1",
        cache_dir: Path | None = None,
        on_url_change: Callable[[str], None] | None = None,
    ) -> None:
        self.local_port = int(local_port)
        self.local_host = local_host
        self.cache_dir = cache_dir
        self.on_url_change = on_url_change

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._url_ready = asyncio.Event()
        self.public_url: str | None = None
        self.last_error: str | None = None
        self._stopping = False

    @property
    def is_alive(self) -> bool:
        return (
            not self._stopping
            and self._proc is not None
            and self._proc.returncode is None
        )

    async def start(self, timeout: float = 30.0) -> str | None:
        self._stopping = False
        self._url_ready.clear()
        self.public_url = None
        self.last_error = None

        with trusted_scope():
            try:
                binary = await ensure_cloudflared_binary(self.cache_dir)
            except Exception as exc:
                self.last_error = f"cloudflared binary unavailable: {exc}"
                log.warning("%s", self.last_error)
                return None

            target = f"http://{self.local_host}:{self.local_port}"
            cmd = [
                str(binary),
                "tunnel",
                "--no-autoupdate",
                "--protocol",
                "http2",
                "--url",
                target,
            ]

            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                )
            except Exception as exc:
                self.last_error = f"failed to spawn cloudflared: {exc}"
                log.warning("%s", self.last_error)
                return None

        self._reader_task = asyncio.create_task(
            self._consume_output(), name="singularity:horizon-log"
        )

        try:
            await asyncio.wait_for(self._url_ready.wait(), timeout=timeout)
            return self.public_url
        except asyncio.TimeoutError:
            self.last_error = "timed out waiting for trycloudflare.com tunnel URL"
            log.warning("%s", self.last_error)
            await self.stop()
            return None

    async def _consume_output(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                match = QUICK_TUNNEL_RE.search(line)
                if match:
                    found_url = match.group(0)
                    if found_url != self.public_url:
                        self.public_url = found_url
                        self._url_ready.set()
                        if self.on_url_change is not None:
                            try:
                                self.on_url_change(found_url)
                            except Exception:
                                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("horizon reader stopped: %s", exc)

    async def stop(self) -> None:
        self._stopping = True
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        self._reader_task = None

        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=4.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    pass
            except Exception:
                pass


CloudflaredTunnel = HorizonTunnel
