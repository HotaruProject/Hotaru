from __future__ import annotations

import contextlib
import contextvars
import io
import json
import logging
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

LEVELS = ("debug", "info", "warn", "error", "crit")
_aliases = {"warning": "warn", "critical": "crit", "fatal": "crit", "exception": "error"}
_numeric = {"debug": 10, "info": 20, "warn": 30, "error": 40, "crit": 50}

_sink: contextvars.ContextVar = contextvars.ContextVar("hotaru_observatory_sink", default=None)
_module: contextvars.ContextVar = contextvars.ContextVar("hotaru_observatory_module", default="")

_secret_keys = (
    "token", "password", "passwd", "secret", "api_hash", "api-key", "apikey",
    "auth_key", "authkey", "vault", "session", "cookie", "authorization",
    "signing_seed", "phone", "code",
)
_secret_patterns = re.compile(
    r"(\b\d{6,12}:[A-Za-z0-9_-]{30,}\b)|(\b[0-9a-f]{32,}\b)|(\bsk-[A-Za-z0-9]{16,}\b)|"
    r"(\bbot\d{6,12}:[\w-]{30,}\b)|(\bBearer\s+[A-Za-z0-9._-]{16,}\b)|(\b[A-Za-z0-9._%+-]+@[\w.-]+\.\w{2,4}:[^\s]{8,}\b)",
    re.IGNORECASE,
)


def norm_level(level: str) -> str:
    text = str(level or "info").strip().casefold()
    if text in _aliases:
        return _aliases[text]
    return text if text in LEVELS else "info"


def scrub_text(value: str) -> str:
    return _secret_patterns.sub("[redacted]", value)


def is_secret_key(key: str) -> bool:
    lowered = key.casefold()
    return any(marker in lowered for marker in _secret_keys)


def _skip_frame(filename: str) -> bool:
    if not filename or filename[0] == "<":
        return True
    lowered = filename.replace("\\", "/")
    return any(
        part in lowered
        for part in ("/python3.", "/lib/python", "/asyncio/", "/importlib/", "/linecache/", "/contextvars/")
    )


def pretty_error(error: BaseException | None, *, frames: int = 4, note: str = "") -> str:
    if error is None:
        return ""
    lines = [f"{type(error).__name__}: {scrub_text(str(error)) or '<no message>'}"]
    collected: list[str] = []
    skipped = 0
    tb = error.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        if _skip_frame(code.co_filename):
            skipped += 1
        else:
            collected.append(f"  {code.co_filename}:{tb.tb_lineno} in {code.co_name}")
        tb = tb.tb_next
    if collected:
        lines.append("  trace (most recent last):")
        lines.extend(collected[-frames:])
    if skipped:
        lines.append(f"  [+{skipped} stdlib frames hidden]")
    cause = error.__cause__ or error.__context__
    depth = 0
    while cause is not None and depth < 2:
        lines.append(f"  caused by {type(cause).__name__}: {scrub_text(str(cause))[:200]}")
        inner = []
        itb = cause.__traceback__
        while itb is not None:
            icode = itb.tb_frame.f_code
            if not _skip_frame(icode.co_filename):
                inner.append(f"    {icode.co_filename}:{itb.tb_lineno} in {icode.co_name}")
                break
            itb = itb.tb_next
        if inner:
            lines.append(inner[0])
        cause = cause.__cause__ or cause.__context__
        depth += 1
    if note:
        lines.append(f"  note: {scrub_text(note)}")
    return "\n".join(lines)


class Observatory:
    def __init__(
        self,
        path: str | Path = "observatory/runtime/events.jsonl",
        *,
        max_value: int = 2048,
        keep_files: int = 4,
        max_bytes: int = 4 * 1024 * 1024,
        level: str = "debug",
    ) -> None:
        if max_value < 1:
            raise ValueError("max_value must be positive")
        self.path = Path(path)
        self.max_value = max_value
        self.keep_files = max(keep_files, 1)
        self.max_bytes = max_bytes
        self.level = norm_level(level)
        self._failed = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.path.touch(exist_ok=True)
        self.path.chmod(0o600)

    def _allowed(self, level: str) -> bool:
        return _numeric[norm_level(level)] >= _numeric[self.level]

    def _rotate(self) -> None:
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
            for index in range(self.keep_files - 1, 0, -1):
                source = self.path.with_suffix(f".{index}.jsonl")
                target = self.path.with_suffix(f".{index + 1}.jsonl")
                if source.exists():
                    source.replace(target)
            self.path.replace(self.path.with_suffix(".1.jsonl"))
            self.path.touch(exist_ok=True)
            self.path.chmod(0o600)
            for stale in sorted(self.path.parent.glob("*.jsonl")):
                if stale.stat().st_size == 0 and stale != self.path and not stale.name.endswith(".1.jsonl"):
                    stale.unlink(missing_ok=True)
        except OSError:
            pass

    def _module_tag(self) -> str:
        tag = _module.get()
        if tag:
            return tag
        try:
            from relay.firewall import current_module

            return current_module()
        except Exception:
            return ""

    def _redact(self, key: str, value: Any) -> Any:
        if is_secret_key(key):
            return "[REDACTED]"
        if value is None or isinstance(value, (int, float, bool)):
            return value
        if isinstance(value, BaseException):
            return pretty_error(value)
        if isinstance(value, str):
            return scrub_text(value)[: self.max_value]
        if isinstance(value, dict):
            return {str(k): self._redact(str(k), v) for k, v in list(value.items())[:64]}
        if isinstance(value, (list, tuple)):
            return [self._redact(key, item) for item in value[:64]]
        return scrub_text(str(value))[: self.max_value]

    def emit(self, component: str, event: str, level: str = "info", **fields: Any) -> None:
        level = norm_level(level)
        if not self._allowed(level):
            return
        now_ts = time.time()
        payload = {
            "ts": now_ts,
            "time": datetime.fromtimestamp(now_ts, tz=UTC).isoformat(timespec="milliseconds"),
            "level": level,
            "component": component,
            "event": event,
            "module": self._module_tag() or None,
        }
        for key, value in fields.items():
            payload[key] = self._redact(key, value)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        now = time.monotonic()
        if now - self._failed < 30.0:
            return
        try:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
            self._failed = 0.0
            self._rotate()
        except OSError:
            self._failed = now

    def tail(self, *, lines: int = 40, component: str | None = None, level: str = "debug", module: str | None = None) -> list[dict[str, Any]]:
        floor = _numeric[norm_level(level)]
        wanted: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8", errors="replace") as stream:
            try:
                stream.seek(0, io.SEEK_END)
                size = stream.tell()
                step = min(65536, size)
                stream.seek(max(0, size - step))
                if size > step:
                    stream.readline()
                tail_data = stream.read().splitlines()
            except OSError:
                tail_data = []
            for raw in reversed(tail_data):
                if len(wanted) >= lines:
                    break
                try:
                    entry = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if component and entry.get("component") != component:
                    continue
                if module and entry.get("module") != module:
                    continue
                if _numeric.get(entry.get("level") or "info", 20) < floor:
                    continue
                wanted.append(entry)
        return wanted

    def error(self, component: str, event: str, **fields: Any) -> None:
        self.emit(component, event, level="error", **fields)

    def warn(self, component: str, event: str, **fields: Any) -> None:
        self.emit(component, event, level="warn", **fields)

    def info(self, component: str, event: str, **fields: Any) -> None:
        self.emit(component, event, level="info", **fields)

    def debug(self, component: str, event: str, **fields: Any) -> None:
        self.emit(component, event, level="debug", **fields)

    def crash(self, component: str, event: str, **fields: Any) -> None:
        self.emit(component, event, level="crit", **fields)


class _Bridge(logging.Handler):
    _translate = {"WARNING": "warn", "ERROR": "error", "CRITICAL": "crit", "DEBUG": "debug", "INFO": "info"}

    def __init__(self, observatory: Observatory) -> None:
        super().__init__()
        self.obs = observatory

    def emit(self, record: logging.LogRecord) -> None:
        level = self._translate.get(record.levelname, "info")
        fields: dict[str, Any] = {"msg": record.getMessage()}
        if record.exc_info and record.exc_info[1] is not None:
            fields["exc"] = record.exc_info[1]
        self.obs.emit(
            str(record.name or "root").split(".")[-1] or "root",
            "log",
            level=level,
            **fields,
        )


def install(observatory: Observatory, *, goygram_level: str = "error") -> None:
    _sink.set(observatory)
    root = logging.getLogger()
    root.addHandler(_Bridge(observatory))
    root.setLevel(logging.INFO)
    goygram = logging.getLogger("goygram")
    goygram.addHandler(_Bridge(observatory))
    goygram.setLevel(_numeric.get(norm_level(goygram_level), 40))
    goygram.propagate = False


def log(*message: Any, level: str = "info", component: str = "module") -> None:
    observatory = _sink.get()
    if observatory is None:
        return
    observatory.emit(component, "log", level=level, msg=" ".join(str(item) for item in message))


class Logs:
    def debug(self, *message: Any) -> None:
        log(*message, level="debug")

    def info(self, *message: Any) -> None:
        log(*message, level="info")

    def warn(self, *message: Any) -> None:
        log(*message, level="warn")

    def error(self, *message: Any) -> None:
        log(*message, level="error")

    def crit(self, *message: Any) -> None:
        log(*message, level="crit")

    def exc(self, error: BaseException, *message: Any) -> None:
        observatory = _sink.get()
        if observatory is None:
            return
        observatory.emit("module", "exception", level="error", msg=" ".join(str(item) for item in message) if message else None, error=error)


logs = Logs()


class _Writer(io.TextIOBase):
    def __init__(self, original: Any, stream_name: str) -> None:
        self.original = original
        self.stream_name = stream_name
        self._buffer = ""

    def _emit_line(self, line: str) -> None:
        observatory = _sink.get()
        if observatory is None:
            try:
                self.original.write(line + "\n")
                self.original.flush()
            except Exception:
                pass
            return
        observatory.emit("module", "output", level="info", stream=self.stream_name, msg=line[: observatory.max_value])

    def write(self, data: str) -> int:
        if not isinstance(data, str):
            data = str(data)
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._emit_line(line)
        return len(data)

    def flush(self) -> None:
        try:
            self.original.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        try:
            return bool(self.original.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        return self.original.fileno()

    @property
    def encoding(self) -> Any:
        return getattr(self.original, "encoding", "utf-8")

    def writable(self) -> bool:
        return True

    def close(self) -> None:
        pass


_stdio_hooked = False


def hook_stdio() -> None:
    global _stdio_hooked
    if _stdio_hooked:
        return
    sys.stdout = _Writer(sys.stdout, "stdout")
    sys.stderr = _Writer(sys.stderr, "stderr")
    _stdio_hooked = True


@contextlib.contextmanager
def scope(module_id: str) -> Iterator[None]:
    token = _module.set(module_id)
    try:
        yield
    finally:
        _module.reset(token)


def observatory() -> Observatory | None:
    return _sink.get()
