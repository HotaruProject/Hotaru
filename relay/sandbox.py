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
from pathlib import Path


def install_firewall(protected):
    roots = [str(Path(item).resolve()) for item in protected]
    def audit(event, args):
        if event == "import" and args and str(args[0]).split(".", 1)[0].casefold() in {"goygram", "hotaru", "relay"}:
            raise PermissionError("module cannot import Hotaru/GoyGram internals; use capability proxies")
        if event in {"open", "os.open"} and args and isinstance(args[0], (str, bytes)):
            value = str(Path(os.fsdecode(args[0])).resolve())
            if value.endswith((".vault", ".session")) or any(value == root or value.startswith(root + os.sep) for root in roots):
                raise PermissionError("module access to Telegram session storage is denied")
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "ctypes.dlopen", "multiprocessing.Process"}:
            raise PermissionError("module process escape is denied")
    sys.addaudithook(audit)


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

def _cap_call(name, payload):
    req = json.dumps({"cap": name, "payload": payload})
    sys.stdout.write(req + "\n")
    sys.stdout.flush()
    while True:
        line = sys.stdin.readline()
        if not line:
            raise OSError("host closed the sandbox channel")
        resp = json.loads(line)
        if resp.get("kind") != "cap_result":
            continue
        if not resp.get("ok"):
            raise PermissionError(resp.get("error", "capability denied"))
        return resp.get("result")


def _mt_call(method, **kwargs):
    return _cap_call("mt", {"method": method, "kwargs": kwargs})


def _net_call(url, data=None, timeout=10.0):
    return _cap_call("net", {"url": url, "data": data, "timeout": timeout})


def main():
    cfg = json.loads(sys.stdin.readline())
    install_firewall(cfg.get("protected", []))
    apply_limits(cfg.get("mem_mb", 128), cfg.get("file_mb", 16), cfg.get("nofile", 64), cfg.get("net_blocked", True))
    ns = {"__name__": cfg.get("module_id", "sandbox"), "cap": _cap_call, "mt": _mt_call, "net": _net_call}
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
            "protected": [str(self.runtime.config.session_dir), str(self.runtime.config.session_dir / f"{self.runtime.config.session_name}.vault"), str(self.runtime.config.session_dir / f"{self.runtime.config.session_name}.session")],
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
        result = await self.roundtrip(module_id, {"command": command, "args": args, "payload": payload})
        if not isinstance(result, dict) or not result.get("ok"):
            raise SandboxError(f"sandbox call failed: {result.get('error') if isinstance(result, dict) else 'malformed'}")
        return result.get("result")

    async def cap_call(self, module_id: str, capability: str, payload: dict[str, Any]) -> Any:
        result = await self.roundtrip(module_id, {"command": "$cap." + capability, "args": [], "payload": payload})
        if not isinstance(result, dict) or not result.get("ok"):
            raise SandboxError(f"sandbox capability failed: {result.get('error') if isinstance(result, dict) else 'malformed'}")
        return result.get("result")

    async def roundtrip(self, module_id: str, request: dict[str, Any]) -> dict[str, Any] | None:
        process = self._workers.get(module_id)
        if process is None or process.poll() is not None:
            raise SandboxError(f"sandbox worker is not running: {module_id}")
        loop = asyncio.get_running_loop()

        def _roundtrip() -> Any:
            assert process.stdin is not None
            process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            process.stdin.flush()
            while True:
                line = self._readline(process)
                if not line:
                    raise SandboxError(f"sandbox worker died during call: {module_id}")
                message = json.loads(line)
                if isinstance(message, dict) and "cap" in message:
                    self._pending_caps.append(message)
                    continue
                return message

        self._pending_caps = getattr(self, "_pending_caps", [])
        task = asyncio.ensure_future(loop.run_in_executor(None, _roundtrip))
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
            except asyncio.TimeoutError:
                if self._pending_caps:
                    await self._serve_caps(module_id)
                continue
            except Exception:
                task.cancel()
                raise

    async def _serve_caps(self, module_id: str) -> None:
        caps = self._pending_caps
        self._pending_caps = []
        cap_host = getattr(self.runtime, "cap_host", None)
        for message in caps:
            name = message.get("cap")
            payload = message.get("payload") or {}
            reply = {"kind": "cap_result", "ok": False, "error": "capability host is unavailable"}
            if cap_host is not None:
                try:
                    result = await cap_host.call(module_id, name, payload)
                    reply = {"kind": "cap_result", "ok": True, "result": result}
                except Exception as exc:
                    reply = {"kind": "cap_result", "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
            process = self._workers.get(module_id)
            if process is not None and process.poll() is None and process.stdin is not None:
                process.stdin.write((json.dumps(reply) + "\n").encode("utf-8"))
                process.stdin.flush()

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
