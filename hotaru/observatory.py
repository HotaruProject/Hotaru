from __future__ import annotations

import asyncio
import contextlib
import contextvars
import errno
import io
import json
import logging

import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Generator, Iterator, TextIO, cast

from goygram.errors import ConnectionClosedError

LEVELS = ("debug", "info", "warn", "error", "crit")
_aliases = {"warning": "warn", "critical": "crit", "fatal": "crit", "exception": "error"}
_numeric = {"debug": 10, "info": 20, "warn": 30, "error": 40, "crit": 50}

Event = Dict[str, object]
Subscriber = Callable[[Event], object]

_sink: contextvars.ContextVar[Observatory | None] = contextvars.ContextVar("hotaru_observatory_sink", default=None)
_module: contextvars.ContextVar[str] = contextvars.ContextVar("hotaru_observatory_module", default="")

_secret_keys = (
    "token", "password", "passwd", "passphrase", "secret", "api_hash", "apihash",
    "api-key", "apikey", "api_key", "auth_key", "authkey", "auth-key", "auth_token",
    "vault", "session", "cookie", "authorization", "signing_seed", "phone", "code",
    "pin", "otp", "private_key", "client_secret", "access_token", "refresh_token",
    "master_key", "credential", "webhook", "proxy_auth",
)
_secret_patterns = re.compile(
    r"(\b(?:bot)?\d{6,14}:[A-Za-z0-9_-]{30,}\b)|"
    r"(\bsk-(?:proj-|ant-|admin-|or-v1-)?[A-Za-z0-9_-]{16,}\b)|"
    r"(\bAIza[0-9A-Za-z_-]{30,45}\b)|"
    r"(\b(?:gsk|pplx|hf)_[A-Za-z0-9_-]{20,}\b)|"
    r"(\b(?:ghp|gho|ghu|ghs|ghr|glpat)_[A-Za-z0-9_-]{20,}\b)|"
    r"(\bgithub_pat_[A-Za-z0-9_]{50,}\b)|"
    r"(\bxox[baprs]-[A-Za-z0-9-]{10,}\b)|"
    r"(\b[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b)|"
    r"(\b(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}\b)|"
    r"(\b(?:rk|sk)_(?:test|live)_[0-9a-zA-Z]{24,}\b)|"
    r"(\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b)|"
    r"(\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*\b)|"
    r"(\b1[A-Za-z0-9+/=]{150,}\b)|"
    r"(\bBQ[A-Za-z0-9+/=]{150,}\b)|"
    r"(\b[0-9a-f]{32,64}\b)|"
    r"(\b[A-Za-z0-9._%+-]+@[\w.-]+\.\w{2,4}:[^\s]{8,}\b)",
    re.IGNORECASE,
)
_pem_pattern = re.compile(
    r"-----BEGIN [A-Z0-9 -]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 -]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_url_credentials_pattern = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.-]*://(?:[^:@\s]*:)?)[^@\s]+@",
)
_kv_secret_pattern = re.compile(
    r"([\'\"]?\b(?:api[_-]?key|api[_-]?hash|auth[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|bot[_-]?token|app[_-]?secret|password|passwd|passphrase|private[_-]?key|"
    r"session[_-]?key|master[_-]?key|secret|token|authorization|cookie|pin)\b[\'\"]?)"
    r"(\s*[:=]\s*)(?:([\'\"])(.*?)\3|([^\s\'\",;}{]+))",
    re.IGNORECASE,
)
_otp_pattern = re.compile(
    r"(?<!status[ _-])(?<!error[ _-])(?<!exit[ _-])(?<!response[ _-])(?<!country[ _-])(?<!zip[ _-])(?<!http[ _-])"
    r"\b(login[_-]?code|auth[_-]?code|verify[_-]?code|verification[_-]?code|otp|pin|passcode|code)\b"
    r"(\s*[:=\s]\s*)([\'\"]?)(\d{4,8})\b\3",
    re.IGNORECASE,
)
_cli_flag_pattern = re.compile(
    r"(--(?:api[_-]?key|token|password|passwd|secret|auth[_-]?token|access[_-]?token))\s+([^\s]+)",
    re.IGNORECASE,
)


def _kv_repl(m: re.Match[str]) -> str:
    prefix = m.group(1) + m.group(2)
    if m.group(3) is not None:
        return f"{prefix}{m.group(3)}[redacted]{m.group(3)}"
    return f"{prefix}[redacted]"


def norm_level(level: str) -> str:
    text = str(level or "info").strip().casefold()
    if text in _aliases:
        return _aliases[text]
    return text if text in LEVELS else "info"


def scrub_text(value: str) -> str:
    if not value:
        return value
    text = _pem_pattern.sub("[redacted-private-key]", value)
    text = _url_credentials_pattern.sub(r"\1[redacted]@", text)
    text = _kv_secret_pattern.sub(_kv_repl, text)
    text = _otp_pattern.sub(r"\1\2\3[redacted]\3", text)
    text = _cli_flag_pattern.sub(r"\1 [redacted]", text)
    return _secret_patterns.sub("[redacted]", text)


def is_secret_key(key: str) -> bool:
    lowered = key.casefold()
    if any(ignore in lowered for ignore in ("status_code", "error_code", "exit_code", "response_code", "country_code", "lang_code", "language_code", "currency_code")):
        return False
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
    worker = getattr(error, "worker_trace", "")
    if worker:
        lines.append("  module trace (most recent last):")
        lines.extend(f"    {line}" for line in scrub_text(str(worker)).rstrip().splitlines()[-12:])
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
        inner: list[str] = []
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


def _reverse_lines(stream: BinaryIO, block: int = 65536) -> Iterator[str]:
    position = stream.seek(0, io.SEEK_END)
    rest = b""
    while position > 0:
        step = min(block, position)
        position -= step
        stream.seek(position)
        parts = (stream.read(step) + rest).split(b"\n")
        rest = parts[0]
        for part in reversed(parts[1:]):
            yield part.decode("utf-8", "replace")
    if rest:
        yield rest.decode("utf-8", "replace")


def rotate_file(path: Path, max_bytes: int = 4 * 1024 * 1024, keep_files: int = 4) -> None:
    try:
        if path.stat().st_size < max_bytes:
            return
        for index in range(keep_files - 1, 0, -1):
            source = path.with_suffix(f".{index}.jsonl")
            target = path.with_suffix(f".{index + 1}.jsonl")
            if source.exists():
                source.replace(target)
        path.replace(path.with_suffix(".1.jsonl"))
        path.touch(exist_ok=True)
        path.chmod(0o600)
        for stale in sorted(path.parent.glob(f"{path.stem}.*.jsonl")):
            if stale.stat().st_size == 0 and stale != path and not stale.name.endswith(".1.jsonl"):
                stale.unlink(missing_ok=True)
    except OSError:
        pass


def prune_logs(root: str | Path, max_file_bytes: int = 8 * 1024 * 1024, keep_tail_bytes: int = 1 * 1024 * 1024) -> int:
    root_path = Path(root).resolve()
    pruned_count = 0
    patterns = ("nohup.out", "*.log", "*.out", "observatory/**/*.jsonl", "sanctuary/**/*.log", "sanctuary/**/*.out")
    scanned: set[Path] = set()
    for pattern in patterns:
        for file_path in root_path.glob(pattern):
            if file_path.is_file() and file_path not in scanned:
                scanned.add(file_path)
    for file_path in scanned:
        try:
            size = file_path.stat().st_size
            if size > max_file_bytes:
                with file_path.open("r+b") as f:
                    f.seek(max(0, size - keep_tail_bytes))
                    tail = f.read()
                    first_nl = tail.find(b"\n")
                    if first_nl != -1 and first_nl < len(tail) - 1:
                        tail = tail[first_nl + 1:]
                    f.seek(0)
                    f.write(tail)
                    f.truncate(len(tail))
                pruned_count += 1
        except Exception:
            continue
    return pruned_count


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
        self._subscribers: list[Subscriber] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            prune_logs(self.path.parent.parent.parent if "observatory" in self.path.parts else self.path.parent, max_file_bytes=max_bytes)
        except Exception:
            pass
        self.path.parent.chmod(0o700)
        self.path.touch(exist_ok=True)
        self.path.chmod(0o600)

    def _allowed(self, level: str) -> bool:
        return _numeric[norm_level(level)] >= _numeric[self.level]

    def _module_tag(self) -> str:
        tag = _module.get()
        if tag:
            return tag
        return ""

    def _redact(self, key: str, value: object) -> object:
        if is_secret_key(key):
            return "[REDACTED]"
        if value is None or isinstance(value, (int, float, bool)):
            return value
        if isinstance(value, BaseException):
            return pretty_error(value)
        if isinstance(value, str):
            return scrub_text(value)[: self.max_value]
        if isinstance(value, dict):
            return {str(k): self._redact(str(k), v) for k, v in list(cast('dict[object, object]', value).items())[:64]}
        if isinstance(value, (list, tuple)):
            return [self._redact(key, item) for item in cast('list[object]' | tuple[object, ...], value)[:64]]
        return scrub_text(str(value))[: self.max_value]

    def emit(self, component: str, event: str, level: str = "info", **fields: Any) -> None:
        level = norm_level(level)
        if not self._allowed(level):
            return
        now_ts = time.time()
        payload: Event = {
            "ts": now_ts,
            "time": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": level,
            "component": component,
            "event": event,
            "module": self._module_tag() or None,
        }
        for key, value in fields.items():
            payload[key] = self._redact(key, value)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        for sub in list(self._subscribers):
            try:
                sub(payload)
            except Exception:
                pass
        now = time.monotonic()
        if now - self._failed < 30.0:
            return
        try:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
            self._failed = 0.0
            rotate_file(self.path, self.max_bytes, self.keep_files)
        except OSError:
            self._failed = now

    def subscribe(self, callback: Subscriber) -> Subscriber:
        if callback not in self._subscribers:
            self._subscribers.append(callback)
        return callback

    def unsubscribe(self, callback: Subscriber) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(callback)

    def tail(self, *, lines: int = 40, component: str | None = None, level: str = "debug", module: str | None = None) -> list[dict[str, Any]]:
        floor = _numeric[norm_level(level)]
        wanted: list[dict[str, Any]] = []
        with self.path.open("rb") as stream:
            try:
                for raw in _reverse_lines(stream):
                    if len(wanted) >= lines:
                        break
                    try:
                        decoded = cast(object, json.loads(raw))
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(decoded, dict):
                        continue
                    entry = cast('dict[str, Any]', decoded)
                    if component and entry.get("component") != component:
                        continue
                    if module and entry.get("module") != module:
                        continue
                    if _numeric.get(entry.get("level") or "info", 20) < floor:
                        continue
                    wanted.append(entry)
            except OSError:
                pass
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


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: (scrub_text(v) if isinstance(v, str) else v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple((scrub_text(v) if isinstance(v, str) else v) for v in record.args)
            elif isinstance(record.args, list):
                record.args = [(scrub_text(v) if isinstance(v, str) else v) for v in record.args]
        return True


class RedactingFormatter(logging.Formatter):
    def __init__(self, original: logging.Formatter | None = None) -> None:
        super().__init__()
        self._original = original

    def format(self, record: logging.LogRecord) -> str:
        if self._original is not None:
            formatted = self._original.format(record)
        else:
            formatted = super().format(record)
        return scrub_text(formatted)


class _Bridge(logging.Handler):
    _translate = {"WARNING": "warn", "ERROR": "error", "CRITICAL": "crit", "DEBUG": "debug", "INFO": "info"}

    def __init__(self, observatory: Observatory) -> None:
        super().__init__()
        self.obs = observatory

    def emit(self, record: logging.LogRecord) -> None:
        level = self._translate.get(record.levelname, "info")
        fields: dict[str, Any] = {"msg": scrub_text(record.getMessage())}
        if record.name == "goygram.mtproto" and (
            record.msg == "MTProto connection lost, reconnecting in %.1fs"
            or (record.msg == "MTProto reconnect failed: %r" and isinstance(record.args, tuple)
                and len(record.args) == 1 and (
                    isinstance(record.args[0], (ConnectionError, ConnectionClosedError, TimeoutError, asyncio.TimeoutError))
                    or (isinstance(record.args[0], OSError)
                        and record.args[0].errno in {errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH})
                ))
        ):
            fields["routine_transport"] = True
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
    filt = SecretRedactionFilter()
    root = logging.getLogger()
    root.addFilter(filt)
    for handler in root.handlers:
        handler.addFilter(filt)
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(handler.formatter))
    bridge = _Bridge(observatory)
    bridge.addFilter(filt)
    root.addHandler(bridge)
    root.setLevel(logging.INFO)
    goygram = logging.getLogger("goygram")
    goygram.addFilter(filt)
    for handler in goygram.handlers:
        handler.addFilter(filt)
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(handler.formatter))
    goygram.addHandler(bridge)
    goygram.setLevel(_numeric.get(norm_level(goygram_level), 40))
    goygram.propagate = False
    logging.getLogger("hotaru").addFilter(filt)


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
    def __init__(self, original: TextIO, stream_name: str) -> None:
        self.original = original
        self.stream_name = stream_name
        self._buffer = ""

    def _emit_line(self, line: str) -> None:
        observatory = _sink.get()
        if observatory is None:
            return
        observatory.emit("module", "output", level="info", stream=self.stream_name, msg=scrub_text(line[: observatory.max_value]))

    def write(self, data: str) -> int:
        scrubbed = scrub_text(data)
        try:
            self.original.write(scrubbed)
            self.original.flush()
        except Exception:
            pass
        self._buffer += scrubbed
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
def scope(module_id: str) -> Generator[None, None, None]:
    token = _module.set(module_id)
    try:
        yield
    finally:
        _module.reset(token)


def observatory() -> Observatory | None:
    return _sink.get()
