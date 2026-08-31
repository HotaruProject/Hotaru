from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import resource
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

WORKER_SOURCE = r'''
import json
import os
import resource
import socket
import sys

def apply_limits(mem_mb, file_mb, nofile, net_blocked):
    if net_blocked:
        class NetBlocked(socket.socket):
            def __init__(self, *a, **k):
                raise OSError("network is blocked in sandbox")
        socket.socket = NetBlocked
        socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("network is blocked"))
        socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(OSError("network is blocked"))
    try:
        if mem_mb > 0:
            resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024, mem_mb * 1024 * 1024))
        if file_mb > 0:
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_mb * 1024 * 1024, file_mb * 1024 * 1024))
        if nofile > 0:
            resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
        if net_blocked:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass

def main():
    cfg = json.loads(sys.stdin.readline())
    apply_limits(cfg.get("mem_mb", 128), cfg.get("file_mb", 16), cfg.get("nofile", 64), cfg.get("net_blocked", True))
    ns = {"__name__": cfg.get("module_id", "sandbox")}
    try:
        exec(compile(cfg["source"], cfg.get("module_id", "sandbox"), "exec"), ns, ns)
    except BaseException as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": type(exc).__name__}) + "\n")
        sys.stdout.flush()
        sys.exit(1)
    sys.stdout.write(json.dumps({"ok": True, "commands": list(cfg.get("commands", []))}) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        req = json.loads(line)
        try:
            handler = ns.get("command_" + req["command"])
            if handler is None:
                out = {"ok": False, "error": "unknown_command"}
            else:
                result = handler(req.get("args") or [], req.get("payload") or {})
                out = {"ok": True, "result": result}
        except BaseException as exc:
            out = {"ok": False, "error": type(exc).__name__}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()

main()
'''


class SandboxError(RuntimeError):
    pass


class ModuleSandbox:
    def __init__(
        self,
        runtime: Any,
        *,
        mem_mb: int = 128,
        file_mb: int = 16,
        nofile: int = 64,
        spawn_timeout: float = 10.0,
        call_timeout: float = 15.0,
    ) -> None:
        self.runtime = runtime
        self.mem_mb = mem_mb
        self.file_mb = file_mb
        self.nofile = nofile
        self.spawn_timeout = spawn_timeout
        self.call_timeout = call_timeout
        self._workers: dict[str, subprocess.Popen] = {}
        self._booted: dict[str, bool] = {}

    def _worker_path(self) -> Path:
        directory = self.runtime.config.state_path.parent / "sandbox"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "worker.py"
        path.write_text(WORKER_SOURCE, encoding="utf-8")
        path.chmod(0o600)
        return path

    def _spawn(self, module_id: str, source: str, commands: list[str]) -> subprocess.Popen:
        worker = self._worker_path()
        process = subprocess.Popen(
            [sys.executable, str(worker)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=tempfile.gettempdir(),
            env={"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir(), "PYTHONPATH": ""},
            preexec_fn=self._preexec,
        )
        hello = {
            "module_id": module_id,
            "source": source,
            "commands": commands,
            "mem_mb": self.mem_mb,
            "file_mb": self.file_mb,
            "nofile": self.nofile,
            "net_blocked": True,
        }
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write((json.dumps(hello) + "\n").encode("utf-8"))
        process.stdin.flush()
        ready = self._readline(process)
        payload = json.loads(ready) if ready else {}
        if not payload.get("ok"):
            process.kill()
            raise SandboxError(f"sandbox worker failed to boot: {payload.get('error', 'no output')}")
        self._workers[module_id] = process
        self._booted[module_id] = True
        return process

    def _preexec(self) -> None:
        try:
            os.setsid()
        except Exception:
            pass
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
        except Exception:
            pass

    def _readline(self, process: subprocess.Popen) -> str | None:
        assert process.stdout is not None
        line = process.stdout.readline()
        return line.decode("utf-8", errors="replace").strip() if line else None

    async def start_module(self, module_id: str, source: str, commands: list[str]) -> bool:
        current = self._workers.get(module_id)
        if current is not None and current.poll() is None:
            return True
        loop = asyncio.get_running_loop()
        process = await loop.run_in_executor(None, lambda: self._spawn(module_id, source, commands))
        return process is not None

    async def call(self, module_id: str, command: str, args: list[str], payload: dict[str, Any]) -> Any:
        process = self._workers.get(module_id)
        if process is None or process.poll() is not None:
            raise SandboxError(f"sandbox worker is not running: {module_id}")
        request = {"command": command, "args": args, "payload": payload}
        loop = asyncio.get_running_loop()

        def _roundtrip() -> Any:
            assert process.stdin is not None
            process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            process.stdin.flush()
            line = self._readline(process)
            if not line:
                raise SandboxError(f"sandbox worker died during call: {module_id}")
            return json.loads(line)

        result = await asyncio.wait_for(loop.run_in_executor(None, _roundtrip), timeout=self.call_timeout)
        if not isinstance(result, dict) or not result.get("ok"):
            raise SandboxError(f"sandbox call failed: {result.get('error') if isinstance(result, dict) else 'malformed'}")
        return result.get("result")

    def stop_module(self, module_id: str) -> bool:
        process = self._workers.pop(module_id, None)
        self._booted.pop(module_id, None)
        if process is None:
            return False
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        return True

    def stop_all(self) -> None:
        for module_id in list(self._workers):
            self.stop_module(module_id)
