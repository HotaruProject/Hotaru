from __future__ import annotations

import asyncio
import codecs
import ipaddress
import os
import re
import signal
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

import aiohttp

if TYPE_CHECKING:
    from aiohttp.abc import ResolveResult
from aiohttp.abc import AbstractResolver


class PublicResolver(AbstractResolver):
    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[ResolveResult]:
        rows = await asyncio.get_running_loop().getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        if not rows or any(not ipaddress.ip_address(row[4][0]).is_global for row in rows):
            raise PermissionError("network target is not public")
        return [{"hostname": host, "host": str(row[4][0]), "port": port, "family": row[0], "proto": row[2], "flags": 0} for row in rows]

    async def close(self) -> None:
        return None


class Job:
    inline_tail = 262144

    def __init__(self, kind: str, data: dict[str, Any], check_url: Callable[[str], Any], workspace: Path) -> None:
        self.kind = kind
        self.data = data
        self.check_url = check_url
        self.workspace = workspace
        self.token = uuid4().hex
        self.events: asyncio.Queue[dict[str, str]] = asyncio.Queue(64)
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.done = False
        self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        try:
            self.result = await (self.net() if self.kind == "net" else self.shell())
        except asyncio.CancelledError:
            self.error = "CancelledError"
        except Exception as exc:
            self.error = type(exc).__name__
        finally:
            self.done = True

    async def net(self) -> dict[str, Any]:
        data = self.data
        url = str(data["url"])
        self.check_url(url)
        method = data.get("method", "POST" if data.get("data") is not None else "GET")
        if not isinstance(method, str) or method.upper() not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            raise ValueError("unsupported HTTP method")
        method = method.upper()
        requested = data.get("timeout")
        timeout = aiohttp.ClientTimeout(total=max(1.0, float(requested))) if requested is not None else aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
        limit = data.get("max_bytes")
        limit = int(limit) if isinstance(limit, (int, float)) and int(limit) > 0 else 8 * 1024 * 1024
        result: dict[str, Any] = {"status": 0, "body": ""}
        connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=False) as session:
            async with session.request(method, url,
                                       headers=data.get("headers"), json=data.get("data"), allow_redirects=False) as response:
                parser = Stream(self.events) if data.get("stream") and 200 <= response.status < 300 else None
                body = bytearray()
                size = 0
                async for chunk in response.content.iter_chunked(8192):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError(f"response exceeds {limit} bytes")
                    if parser:
                        await parser.feed(chunk)
                    else:
                        body.extend(chunk)
                if parser:
                    await parser.feed(b"", final=True)
                result = {"status": response.status, "body": body.decode("utf-8", "replace"), "method": method,
                          "headers": {key.lower(): value for key, value in response.headers.items()}}
        return result

    async def shell(self) -> dict[str, Any]:
        data = self.data
        self.workspace.mkdir(parents=True, exist_ok=True)
        cwd = str(Path(data.get("cwd") or self.workspace).expanduser().resolve())
        result: dict[str, Any] = {"stdout": "", "stderr": "", "cwd": cwd, "truncated": False, "returncode": None}
        self.result = result
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.workspace), "LANG": "C.UTF-8"}
        if os.name == "nt":
            env.update({key: os.environ[key] for key in ("SystemRoot", "COMSPEC", "PATHEXT") if key in os.environ})
        spawn = asyncio.create_task(asyncio.create_subprocess_shell(str(data["command"]), cwd=cwd, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=os.name != "nt"))
        try:
            proc = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            proc = await spawn
            self.kill(proc)
            await proc.communicate()
            raise

        async def read(pipe: asyncio.StreamReader | None, name: str) -> None:
            if pipe is None:
                return
            path = self.workspace / f".job-{self.token}.{name}"
            handle = path.open("wb")
            raw = bytearray()
            try:
                while True:
                    chunk = await pipe.read(8192)
                    if not chunk:
                        break
                    handle.write(chunk)
                    raw.extend(chunk)
                    if len(raw) > self.inline_tail:
                        del raw[:-self.inline_tail]
                        result["truncated"] = True
                    result[name] = raw.decode("utf-8", "replace")
            finally:
                handle.close()
                if result["truncated"]:
                    result[f"{name}_file"] = str(path)
                else:
                    path.unlink(missing_ok=True)

        async def write() -> None:
            if proc.stdin is None:
                return
            try:
                proc.stdin.write(str(data.get("stdin", "")).encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        tasks = [asyncio.create_task(read(proc.stdout, "stdout")), asyncio.create_task(read(proc.stderr, "stderr")), asyncio.create_task(write())]
        completion = asyncio.gather(proc.wait(), *tasks)
        try:
            requested = data.get("timeout")
            await asyncio.wait_for(completion, max(1.0, float(requested)) if requested is not None else None)
            result["returncode"] = proc.returncode
            return result
        finally:
            self.kill(proc)
            await proc.wait()
            for task in tasks:
                task.cancel()
            await asyncio.gather(completion, *tasks, return_exceptions=True)

    @staticmethod
    def kill(proc: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "nt":
                if proc.returncode is None:
                    proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class Stream:
    def __init__(self, queue: asyncio.Queue[dict[str, str]]) -> None:
        self.queue = queue
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.buffer = ""
        self.lines: list[str] = []
        self.event = "message"
        self.size = 0

    async def feed(self, chunk: bytes, final: bool = False) -> None:
        self.buffer += self.decoder.decode(chunk, final=final)
        while True:
            match = re.search(r"\r\n|\r|\n", self.buffer)
            if match is None or not final and match[0] == "\r" and match.end() == len(self.buffer):
                break
            line, self.buffer = self.buffer[:match.start()], self.buffer[match.end():]
            await self.line(line)
        if len(self.buffer) + self.size > 1048576:
            raise ValueError("SSE event limit")
        if final:
            if self.buffer:
                await self.line(self.buffer)
                self.buffer = ""
            await self.line("")

    async def line(self, line: str) -> None:
        if not line:
            if self.lines:
                await self.queue.put({"event": self.event, "data": "\n".join(self.lines)})
            self.lines = []
            self.event = "message"
            self.size = 0
        elif line.startswith("data:"):
            self.lines.append(line[5:].lstrip(" ") if line[5:6] == " " else line[5:])
            self.size += len(line)
        elif line.startswith("event:"):
            self.event = line[6:][1:] if line[6:7] == " " else line[6:]
        if self.size > 1048576:
            raise ValueError("SSE event limit")
