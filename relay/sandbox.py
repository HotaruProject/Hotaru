from __future__ import annotations

import __future__
import asyncio
import base64
import itertools
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Sequence, cast

from . import toolkit as _toolkit
from . import context as _context
from . import translation as _translation
from hotaru.callbacks import CallbackBinding
from relay.firewall import trusted_scope

_CONTEXT_SOURCE = Path(_context.__file__).read_text(encoding="utf-8")
_TRANSLATION_SOURCE = Path(_translation.__file__).read_text(encoding="utf-8")

_TOOLKIT_SOURCE = Path(_toolkit.__file__).read_text(encoding="utf-8")

_PROTO_BYTES_KEY = "$bytes"


def _proto_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {_PROTO_BYTES_KEY: base64.b64encode(bytes(value)).decode("ascii")}
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


def _proto_object_hook(data: dict[str, Any]) -> Any:
    if set(data.keys()) == {_PROTO_BYTES_KEY}:
        encoded = data[_PROTO_BYTES_KEY]
        if isinstance(encoded, str):
            try:
                return base64.b64decode(encoded.encode("ascii"))
            except ValueError:
                return data
    return data


def _proto_dumps(payload: Any) -> str:
    return json.dumps(payload, default=_proto_default)


def _proto_loads(text: str) -> Any:
    return json.loads(text, object_hook=_proto_object_hook)

SANDBOX_BASE_ROOT = "/run/hotaru-sandbox"
SANDBOX_UID = 65534
SANDBOX_GID = 65534


def userns_supported() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    probe = "import ctypes,os;os._exit(0 if ctypes.CDLL(None,use_errno=True).unshare(0x10000000)==0 else 1)"
    try:
        done = subprocess.run([os.path.realpath(sys.executable), "-c", probe], capture_output=True, timeout=15)
    except Exception:
        return False
    return done.returncode == 0

WORKER_SOURCE = r'''
import asyncio
import __future__
import base64
import io
import itertools
import json
import os
try:
    import resource
except ImportError:
    resource = None
import queue
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import re as _re

_HOST_OUT = sys.stdout
_HOST_LOCK = threading.Lock()
_HEARTBEAT_SECONDS = 2.0
_MAX_TASKS = 8
_LOCAL = threading.local()
_reply_wait = {}
_reply_ids = itertools.count(1)
_busy_lock = threading.Lock()
_busy_count = 0


def _host_raw(text):
    with _HOST_LOCK:
        _HOST_OUT.write(text)
        _HOST_OUT.flush()


def _host_emit(payload):
    _host_raw(_proto_dumps(payload) + "\n")


def _busy(delta):
    global _busy_count
    with _busy_lock:
        _busy_count += delta


def _ask(payload):
    cid = next(_reply_ids)
    payload["cid"] = cid
    payload["rid"] = getattr(_LOCAL, "rid", 0)
    waiter = _reply_wait[cid] = queue.Queue(maxsize=1)
    try:
        _host_emit(payload)
        return waiter.get()
    finally:
        _reply_wait.pop(cid, None)


def _heartbeat_loop():
    while True:
        time.sleep(_HEARTBEAT_SECONDS)
        if _busy_count <= 0:
            continue
        try:
            _host_emit({"_hb": 1})
        except Exception:
            return

_PROTO_BYTES_KEY = "$bytes"


def _proto_default(value):
    if isinstance(value, (bytes, bytearray)):
        return {_PROTO_BYTES_KEY: base64.b64encode(bytes(value)).decode("ascii")}
    raise TypeError("object of type " + type(value).__name__ + " is not JSON serializable")


def _proto_object_hook(data):
    if isinstance(data, dict) and set(data) == {_PROTO_BYTES_KEY}:
        encoded = data[_PROTO_BYTES_KEY]
        if isinstance(encoded, str):
            try:
                return base64.b64decode(encoded.encode("ascii"))
            except ValueError:
                return data
    return data


def _proto_dumps(payload):
    return json.dumps(payload, default=_proto_default)


def _proto_loads(text):
    return json.loads(text, object_hook=_proto_object_hook)


def rich_html(html):
    return {"html": _re.sub(r"\n(?![^<]*>)", "<br>", html)}

SECCOMP_CFG = {"allow": set(), "errno": set(), "kill": set()}
CLONE_NR = 56
CLONE_NS_MASK = 0x7E020000
CLONE_THREAD_MASK = 0x10800
CLONE3_NR = 435
ARCH_X86_64 = 0xC000003E
RET_ALLOW = 0x7FFF0000
RET_ERRNO = 0x00050001
RET_ERRNO_NOSYS = 0x00050026
RET_KILL = 0x80000000


def install_seccomp():
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError("seccomp requires no_new_privs")
    libc.prctl(4, 0, 0, 0, 0)
    allowed = sorted(SECCOMP_CFG["allow"])
    errno_set = sorted(SECCOMP_CFG["errno"])
    insns = []

    def emit(code, jt=0, jf=0, k=0):
        insns.append([code, jt, jf, k])
        return len(insns) - 1

    emit(0x20, k=4)
    emit(0x15, 1, 2, ARCH_X86_64)
    emit(0x06, k=RET_KILL)
    emit(0x20, k=0)
    allow_start = len(insns)
    for nr in allowed:
        emit(0x15, 0, 0, nr)
    allow_fallthrough = emit(0x05)
    allow_ret = emit(0x06, k=RET_ALLOW)
    for idx in range(allow_start, allow_fallthrough):
        insns[idx][1] = allow_ret - idx - 1
    errno_start = len(insns)
    insns[allow_fallthrough][3] = errno_start - allow_fallthrough - 1
    for nr in errno_set:
        emit(0x15, 0, 0, nr)
    errno_fallthrough = emit(0x05)
    errno_ret = emit(0x06, k=RET_ERRNO)
    for idx in range(errno_start, errno_fallthrough):
        insns[idx][1] = errno_ret - idx - 1
    clone3_check = emit(0x15, 0, 0, CLONE3_NR)
    clone3_enosys = emit(0x06, k=RET_ERRNO_NOSYS)
    clone_check = emit(0x15, 0, 0, CLONE_NR)
    clone_load = emit(0x20, k=0x10)
    emit(0x54, 0, 0, CLONE_THREAD_MASK)
    clone_thread_ok = emit(0x15, 0, 0, CLONE_THREAD_MASK)
    clone_allow = emit(0x06, k=RET_ALLOW)
    emit(0x06, k=RET_ERRNO)
    default_kill = emit(0x06, k=RET_KILL)
    insns[errno_fallthrough][3] = clone3_check - errno_fallthrough - 1
    insns[clone3_check][1] = clone3_enosys - clone3_check - 1
    insns[clone3_check][2] = clone_check - clone3_check - 1
    insns[clone_check][1] = clone_load - clone_check - 1
    insns[clone_check][2] = default_kill - clone_check - 1
    insns[clone_thread_ok][1] = clone_allow - clone_thread_ok - 1
    insns[clone_thread_ok][2] = clone_allow - clone_thread_ok

    class SockFilter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]

    filters = (SockFilter * len(insns))(*[SockFilter(*insn) for insn in insns])
    program = SockFprog(len=len(insns), filter=filters)
    if libc.prctl(22, 2, ctypes.cast(ctypes.byref(program), ctypes.c_void_p).value or 0, 0, 0) != 0:
        raise OSError("seccomp filter installation failed")


def install_firewall(protected, namespaces=False, deps_root=""):
    roots = [str(Path(item).resolve()) for item in protected]
    native_root = str((Path(os.__file__).resolve().parent / "lib-dynload").resolve()) + os.sep
    module_deps = str(Path(deps_root).resolve()) + os.sep if deps_root else ""

    def deny_process(*args, **kwargs):
        raise PermissionError("module process escape is denied")

    for module_name, attribute in (("subprocess", "_fork_exec"), ("_posixsubprocess", "fork_exec")):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, attribute):
            setattr(module, attribute, deny_process)

    def audit(event, args):
        if not namespaces and event == "os.chdir":
            raise PermissionError("portable sandbox working directory is fixed")
        if event.startswith("ctypes."):
            raise PermissionError("module native code access is denied")
        if event == "import" and args and str(args[0]).split(".", 1)[0].casefold() in {"goygram", "hotaru", "relay"}:
            raise PermissionError("module cannot import Hotaru/GoyGram internals; use capability proxies")
        if not namespaces and event == "import" and len(args) > 1 and args[1]:
            resolved = str(Path(os.fsdecode(args[1])).resolve())
            pure_python_dep = bool(module_deps) and resolved.startswith(module_deps) and not resolved.endswith((".so", ".pyd", ".dylib"))
            if not resolved.startswith(native_root) and not pure_python_dep:
                raise PermissionError("portable sandbox only permits standard-library native extensions")
        if event in {"open", "os.open"} and args and isinstance(args[0], (str, bytes)):
            if not namespaces and args[1] is None and not os.path.isabs(args[0]):
                raise PermissionError("portable sandbox requires absolute paths for descriptor opens")
            value = str(Path(os.fsdecode(args[0])).resolve())
            if value.endswith((".vault", ".session")) or any(value == root or value.startswith(root + os.sep) for root in roots):
                raise PermissionError("module access to Telegram session storage is denied")
        if event in {"os.remove", "os.rmdir", "os.truncate", "shutil.rmtree", "os.rename", "os.link", "os.symlink"}:
            count = 2 if event in {"os.rename", "os.link", "os.symlink"} else 1
            for path in args[:count]:
                if not isinstance(path, (str, bytes)):
                    continue
                if not namespaces and not os.path.isabs(path) and any(isinstance(fd, int) and fd != -1 for fd in args[count:]):
                    raise PermissionError("portable sandbox requires absolute paths for descriptor operations")
                value = str(Path(os.fsdecode(path)).resolve())
                if value.endswith((".vault", ".session")) or any(value == root or value.startswith(root + os.sep) or root.startswith(value.rstrip(os.sep) + os.sep) for root in roots):
                    raise PermissionError("module access to Telegram session storage is denied")
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork", "os.forkpty", "multiprocessing.Process"}:
            raise PermissionError("module process escape is denied")

    sys.addaudithook(audit)


def apply_limits(mem_mb, file_mb, nofile, cpu_seconds, net_blocked):
    if net_blocked:
        real_socket = socket.socket

        class NetBlocked(real_socket):
            def __init__(self, *a, **k):
                raise OSError("network is blocked in sandbox")

        def _real_socketpair(family=None, type=socket.SOCK_STREAM, proto=0):
            import _socket as raw_socket
            if family is None:
                family = getattr(socket, "AF_UNIX", socket.AF_INET)
            a, b = raw_socket.socketpair(family, type, proto)
            return real_socket(family, type, proto, a.detach()), real_socket(family, type, proto, b.detach())

        socket.socket = NetBlocked
        socket.socketpair = _real_socketpair
        socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("network is blocked"))
        socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(OSError("network is blocked"))
    try:
        if mem_mb > 0:
            resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024, mem_mb * 1024 * 1024))
        if file_mb > 0:
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_mb * 1024 * 1024, file_mb * 1024 * 1024))
        if nofile > 0:
            resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
        if cpu_seconds > 0:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass


def _cap_call(name, payload):
    resp = _ask({"cap": name, "payload": payload})
    if not resp.get("ok"):
        error = {"PermissionError": PermissionError, "OSError": OSError, "TimeoutError": TimeoutError, "ConnectionError": ConnectionError, "ValueError": ValueError, "TypeError": TypeError}.get(resp.get("error_type", "PermissionError"), RuntimeError)
        raise error(resp.get("error", "capability failed"))
    return resp.get("result")


class _WorkerLogs:
    def debug(self, *msg):
        return _cap_call("logs", {"op": "write", "level": "debug", "msg": " ".join(str(m) for m in msg)})
    def info(self, *msg):
        return _cap_call("logs", {"op": "write", "level": "info", "msg": " ".join(str(m) for m in msg)})
    def warn(self, *msg):
        return _cap_call("logs", {"op": "write", "level": "warn", "msg": " ".join(str(m) for m in msg)})
    def error(self, *msg):
        return _cap_call("logs", {"op": "write", "level": "error", "msg": " ".join(str(m) for m in msg)})
    def crit(self, *msg):
        return _cap_call("logs", {"op": "write", "level": "crit", "msg": " ".join(str(m) for m in msg)})
    def exc(self, err, *msg):
        text = " ".join(str(m) for m in msg) if msg else type(err).__name__
        return _cap_call("logs", {"op": "write", "level": "error", "msg": text, "name": type(err).__name__, "detail": str(err)[:500]})


def _mt_call(method, **kwargs):
    return _cap_call("mt", {"method": method, "kwargs": kwargs})


def _net_call(url, data=None, timeout=10.0):
    return _cap_call("net", {"url": url, "data": data, "timeout": timeout})


def _respond_call(payload):
    resp = _ask({"respond": payload})
    if not resp.get("ok"):
        raise PermissionError(resp.get("error", "respond failed"))
    return resp.get("result")


class _StateProxy:
    def get(self, key, default=None):
        try:
            value = _cap_call("state", {"op": "get", "key": key})
        except PermissionError:
            return default
        return default if value is None else value

    def set(self, key, value):
        return _cap_call("state", {"op": "set", "key": key, "value": value})

    def delete(self, key):
        return _cap_call("state", {"op": "delete", "key": key})

    def keys(self):
        return _cap_call("state", {"op": "keys"})

    def pointer(self, key, default=None):
        return _StatePointer(self, key, default)

class _StatePointer:
    def __init__(self, namespace, key, default):
        self.namespace = namespace
        self.key = key
        self.default = default

    @property
    def value(self):
        return self.namespace.get(self.key, self.default)

    def set(self, val):
        self.namespace.set(self.key, val)

    @value.setter
    def value(self, val):
        self.set(val)


_sandbox_callbacks = {}


def _cb_respond_call(data):
    msg = _ask({"cb_respond": data})
    result = msg.get("cb_respond_result") or {}
    if not result.get("ok"):
        raise PermissionError(result.get("error", "cb_respond failed"))
    return result.get("result")


class SandboxCallbackProxy:
    def form_allowed(self, key):
        return bool(_respond_call({"form": key, "op": "allowed"}))
    def __init__(self, cb_data):
        self._data = cb_data

    async def respond(self, text="", alert=False):
        return _cb_respond_call({"action": "respond", "text": str(text), "alert": bool(alert)})

    async def edit(self, text, **kwargs):
        return _cb_respond_call({"action": "edit", "text": str(text), **{k: v for k, v in kwargs.items() if isinstance(v, (str, int, float, bool, list, dict, type(None)))}})

    async def delete(self):
        return _cb_respond_call({"action": "delete"})

    @property
    def chat_id(self):
        return self._data.get("chat_id")

    @property
    def message_id(self):
        return self._data.get("message_id")

    @property
    def from_id(self):
        return self._data.get("from_id")

    @property
    def inline_message_id(self):
        return self._data.get("inline_message_id")


class SandboxAttachment:
    __slots__ = ("kind", "file_name", "mime_type", "size")

    def __init__(self, data):
        data = data or {}
        self.kind = data.get("kind")
        self.file_name = data.get("file_name")
        self.mime_type = data.get("mime_type")
        self.size = data.get("size")

    @property
    def name(self):
        return self.file_name

    def __repr__(self):
        return f"SandboxAttachment(kind={self.kind!r}, file_name={self.file_name!r})"


_template_providers = {}
_template_resolving = set()


class ContextOperations:
    pass


class SandboxContext(ContextOperations):
    def __init__(self, tools, payload):
        payload = payload or {}
        self.chat_id = payload.get("chat_id")
        self.message_id = payload.get("message_id")
        self.args = list(payload.get("args") or [])
        self.topic_id = payload.get("topic_id")
        self.state = _StateProxy()
        self.tools = SimpleNamespace(**{k: v for k, v in (tools or {}).items()})
        self._msg = payload

    @property
    def uptime(self):
        return max(0, int(time.monotonic() - self._msg["started_at"]))

    def render_template(self, key, **values):
        entry = self._msg.get("templates", {}).get(key)
        if entry is None:
            raise ValueError(f"{key}: no template_fields declared")
        if any("/" in name for name in self.tools.template_names(entry["value"], legacy_braces=entry["legacy_braces"])):
            raise ValueError("Shared fields require await ctx.render_template_async(key, **values)")
        return self.tools.template_render(entry["value"], entry["fields"], values, legacy_braces=entry["legacy_braces"])

    async def render_template_async(self, key, **values):
        entry = self._msg.get("templates", {}).get(key)
        if entry is None:
            raise ValueError(f"{key}: no template_fields declared")
        local = {}
        names = self.tools.template_names(entry["value"], legacy_braces=entry["legacy_braces"])
        fields = dict(entry["fields"], **{name: {} for name in names if "/" in name})
        for name in self.tools.template_names(entry["value"], fields, legacy_braces=entry["legacy_braces"]):
            handler = _template_providers.get(name)
            if handler is not None:
                if name in _template_resolving:
                    raise ValueError("Template provider cycle")
                _template_resolving.add(name)
                try:
                    local[name] = await asyncio.wait_for(handler(self), timeout=2.0)
                finally:
                    _template_resolving.discard(name)
        return _cap_call("$template", {"key": key, "values": values, "local": local})

    @property
    def i18n(self):
        return SimpleNamespace(language=self._msg.get("language", "en"), t=self.t)

    def t(self, key, default=None, **params):
        value = self._msg.get("translations") or {}
        for part in str(key).split("."):
            if not isinstance(value, dict) or part not in value:
                value = default if isinstance(default, str) else str(key)
                break
            value = value[part]
        if not isinstance(value, str):
            value = default if isinstance(default, str) else str(key)
        try:
            return value.format(**params)
        except (KeyError, IndexError, ValueError):
            return default if isinstance(default, str) else str(key)

    @property
    def message(self):
        return SimpleNamespace(
            chat_id=self.chat_id,
            id=self.message_id,
            is_me=self._msg.get("out", False),
            out=self._msg.get("out", False),
            from_id=self._msg.get("from_id"),
        )

    @property
    def has_media(self) -> bool:
        return self.attachment is not None

    @property
    def attachment(self):
        data = self._msg.get("attachment")
        return SandboxAttachment(data) if data else None

    @property
    def reply_attachment(self):
        data = self._msg.get("reply_attachment")
        return SandboxAttachment(data) if data else None

    def args_list(self):
        return list(self.args)

    async def reply(self, message=None):
        target = self._msg if message is None else message
        if target is None:
            return None
        reply_to = target.get("reply_to")
        if not isinstance(reply_to, dict):
            return None
        fetched = _cap_call("fetch", {"op": "reply", "peer": self.chat_id, "reply_to": reply_to})
        return fetched if isinstance(fetched, dict) else None

    async def reply_text(self, message=None):
        reply = await self.reply(message)
        if reply is None:
            return None
        text = reply.get("message") or reply.get("text") or reply.get("caption")
        return str(text) if text is not None else None

    async def msg(self, chat_id, message_id):
        fetched = _cap_call("fetch", {"op": "message", "peer": chat_id, "id": int(message_id)})
        return fetched if isinstance(fetched, dict) else None

    async def resolve(self, value):
        fetched = _cap_call("fetch", {"op": "entity", "value": value})
        return fetched if isinstance(fetched, dict) else None

    @staticmethod
    def escape(value):
        import html as _html
        return _html.escape(str(value), quote=False)

    async def _ctx_prepare(self, text, parse_mode, buttons):
        return _respond_call({"prepare": {"text": text, "parse_mode": parse_mode, "buttons": buttons}})

    async def respond(self, content=None, **kwargs):
        if any(key in kwargs for key in ("peer", "chat_id", "message_id", "target")):
            return await self._ctx_operation("respond", content, **kwargs)
        return _respond_call({"content": content, "kwargs": kwargs})

    async def send(self, text, **kwargs):
        kwargs.setdefault("output", "reply")
        return await self.respond(text, **kwargs)

    async def edit(self, text, **kwargs):
        if any(key in kwargs for key in ("peer", "chat_id", "message_id", "target")):
            return await self._ctx_operation("edit", text, **kwargs)
        return await self.respond(text, output="edit", **kwargs)

    async def smart_respond(self, content=None, **kwargs):
        return await self.respond(content, **kwargs)

    async def reply_html(self, text, **kwargs):
        kwargs.setdefault("output", "reply")
        return await self.respond(text, **kwargs)

    async def edit_html(self, text, **kwargs):
        kwargs.setdefault("output", "edit")
        return await self.respond(text, **kwargs)

    async def respond_file(self, media, caption=None, **kwargs):
        kwargs.setdefault("output", "auto")
        kwargs.setdefault("caption", caption)
        return await self.send_file(media, kwargs.pop("caption", None), **kwargs)

    async def respond_media(self, media, **kwargs):
        kwargs.setdefault("output", "auto")
        return await self.send_file(media, **kwargs)

    async def respond_rich(self, rich_message, **kwargs):
        return await self.respond(rich_message, rich=True, **kwargs)

    async def send_rich(self, html, **kwargs):
        return await self._ctx_operation("send_rich", html, **kwargs)

    async def inline_form(self, text, buttons=None, form_handle=False, **kwargs):
        return _respond_call({"content": text, "form_handle": bool(form_handle), "kwargs": {"buttons": buttons, "output": "inline", **kwargs}})

    async def edit_form(self, key, text, buttons=None):
        return _respond_call({"form": key, "op": "edit", "content": text, "buttons": buttons})

    def form_allowed(self, key):
        return bool(_respond_call({"form": key, "op": "allowed"}))

    @property
    def rich(self):
        return _RichProxy()

    @property
    def ui(self):
        return _UiProxy(self.t)

    async def _ctx_media_rpc(self, method, **kwargs):
        return _respond_call({"media_rpc": {"method": method, "kwargs": kwargs}})

    async def send_file(self, media, caption=None, **kwargs):
        output = kwargs.pop("output", "send")
        explicit = any(key in kwargs for key in ("peer", "chat_id", "message_id", "target"))
        if output in {"auto", "edit"} and not explicit and self._msg.get("out"):
            return await self._ctx_operation("edit_media", media, caption=caption, **kwargs)
        operation = "reply_file" if output in {"auto", "reply"} and not explicit else "send_file"
        return await self._ctx_operation(operation, media, caption, **kwargs)

    async def send_media(self, media, caption=None, **kwargs):
        return await self.send_file(media, caption, **kwargs)

    async def upload_file(self, source, **kwargs):
        return _cap_call("assets", {"op": "upload", "file": source, "filename": kwargs.get("file_name")})

    async def download_file(self, source, destination=None, **kwargs):
        return _cap_call("assets", {"op": "download", "message": source, "destination": destination})

    def file_media(self, up, *, mime="application/octet-stream", file_name=None, force_file=True):
        name = file_name or (up.get("name") if isinstance(up, dict) else None) or "file"
        if isinstance(up, dict) and up.get("big"):
            file = {"_": "inputFileBig", "id": up["id"], "parts": up["parts"], "name": up["name"]}
        else:
            file = {"_": "inputFile", "id": up["id"], "parts": up["parts"], "name": up["name"], "md5_checksum": up.get("md5", "")}
        return {"_": "inputMediaUploadedDocument", "file": file, "mime_type": mime, "attributes": [{"_": "documentAttributeFilename", "file_name": name}], "force_file": force_file}

    async def cap(self, capability, payload=None):
        return _cap_call(capability, payload or {})

    async def mt(self, method, **kwargs):
        return _mt_call(method, **kwargs)

    async def net(self, url, *, data=None, timeout=10.0):
        return _net_call(url, data=data, timeout=timeout)

    @property
    def tg(self):
        return _TgProxy()

    @property
    def html(self):
        return _HtmlHelper()

    @property
    def inline(self):
        return _InlineProxy()

    @property
    def modules(self):
        return _ModulesProxy()

    @property
    def assets(self):
        return _AssetsProxy()

class _AssetsProxy:
    async def upload(self, file, filename=None):
        return _cap_call("assets", {"op": "upload", "file": file, "filename": filename})
        
    async def download(self, message, destination=None):
        return _cap_call("assets", {"op": "download", "message": message, "destination": destination})


class _RichProxy:
    async def send(self, html, **kwargs):
        return _respond_call({"content": html, "kwargs": {"rich": True, **kwargs}})

    async def edit(self, message_id, html, **kwargs):
        return _respond_call({"content": html, "kwargs": {"rich": True, "output": "edit", "message_id": message_id, **kwargs}})

    async def respond(self, html, **kwargs):
        return await self.send(html, **kwargs)


class _UiProxy:
    def __init__(self, t):
        self.t = t

    @staticmethod
    def button(text, action_id, payload=None, *, style=None):
        if callable(action_id):
            import hashlib
            qname = getattr(action_id, "__qualname__", getattr(action_id, "__name__", "handler"))
            action_key = hashlib.sha256(qname.encode()).hexdigest()[:16]
            _sandbox_callbacks[action_key] = action_id
            action_id = action_key
        res = {"text": text, "action_id": action_id, "payload": payload}
        if style is not None:
            res["style"] = style
        return res

    @staticmethod
    def primary(text, callback, payload=None):
        return _UiProxy.button(text, callback, payload, style="primary")

    @staticmethod
    def success(text, callback, payload=None):
        return _UiProxy.button(text, callback, payload, style="success")

    @staticmethod
    def danger(text, callback, payload=None):
        return _UiProxy.button(text, callback, payload, style="danger")

    @staticmethod
    def url(text, url):
        return {"text": text, "url": url}

    def on(self, callback):
        import hashlib
        qname = getattr(callback, "__qualname__", getattr(callback, "__name__", "handler"))
        action_id = hashlib.sha256(qname.encode()).hexdigest()[:16]
        _sandbox_callbacks[action_id] = callback
        return action_id

    def close(self, text=None):
        if text is None:
            text = self.t("common.close")
        return {"text": text, "action_id": "close", "style": "danger"}

    @staticmethod
    def grid(*buttons, columns=2):
        rows = []
        row = []
        for b in buttons:
            row.append(b)
            if len(row) == columns:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows

    @staticmethod
    def row(*buttons):
        return list(buttons)

    @staticmethod
    def rows(*rows):
        return [list(r) for r in rows]

    @staticmethod
    def back(text, handler, payload=None):
        return _UiProxy.button(text, handler, payload)

    @staticmethod
    def confirm(text, handler, payload=None):
        return _UiProxy.button(text, handler, payload, style="success")

    @staticmethod
    def cancel(text, handler, payload=None):
        return _UiProxy.button(text, handler, payload, style="danger")

    @staticmethod
    def callback(text, handler, payload=None, *, style=None):
        return _UiProxy.button(text, handler, payload, style=style)


class _TgProxy:
    async def call(self, method, kwargs=None):
        return _mt_call(method, **(kwargs or {}))

    async def send(self, method, **kwargs):
        return await self.call(method, kwargs)

    async def get(self, method, **kwargs):
        return await self.call(method, kwargs)

    async def send_message(self, text, **kwargs):
        return await self.call("messages.sendMessage", {"message": text, **kwargs})

    async def send_media(self, media, caption="", **kwargs):
        return await self.call("messages.sendMedia", {"media": media, "message": caption, **kwargs})

    async def send_rich(self, html_text, **kwargs):
        rich = {"_": "inputRichMessageHTML", **rich_html(html_text)} if isinstance(html_text, str) else html_text
        return await self.call("messages.sendMessage", {"rich_message": rich, **kwargs})

    async def edit_message(self, message_id, text, **kwargs):
        return await self.call("messages.editMessage", {"id": message_id, "message": text, **kwargs})

    async def edit_rich(self, message_id, html_text, **kwargs):
        rich = {"_": "inputRichMessageHTML", **rich_html(html_text)} if isinstance(html_text, str) else html_text
        return await self.call("messages.editMessage", {"id": message_id, "rich_message": rich, **kwargs})

    async def delete_message(self, message_id, **kwargs):
        return await self.call("messages.deleteMessages", {"id": [message_id], **kwargs})

    def __getattr__(self, name):
        async def _call(**kwargs):
            return await self.call(name, kwargs)
        return _call


class _HtmlHelper:
    @staticmethod
    def escape(value):
        import html as _html
        return _html.escape(str(value), quote=False)

    @staticmethod
    def bold(value):
        import html as _html
        return f"<b>{_html.escape(str(value), quote=False)}</b>"

    @staticmethod
    def italic(value):
        import html as _html
        return f"<i>{_html.escape(str(value), quote=False)}</i>"

    @staticmethod
    def code(value):
        import html as _html
        return f"<code>{_html.escape(str(value), quote=False)}</code>"

    @staticmethod
    def underline(value):
        import html as _html
        return f"<u>{_html.escape(str(value), quote=False)}</u>"

    @staticmethod
    def quote(value):
        import html as _html
        return f"<blockquote>{_html.escape(str(value), quote=False)}</blockquote>"

    @staticmethod
    def link(label, url):
        import html as _html
        return f'<a href="{_html.escape(str(url), quote=True)}">{_html.escape(str(label), quote=False)}</a>'

    @staticmethod
    def pre(value, language=None):
        import html as _html
        safe = _html.escape(str(value), quote=False)
        if language:
            return f'<pre class="language-{_html.escape(str(language), quote=True)}">{safe}</pre>'
        return f"<pre>{safe}</pre>"


class _InlineProxy:
    async def query(self, text, **kwargs):
        return _cap_call("inline", {"op": "query", "text": text, "kwargs": kwargs})

    async def send(self, peer, text, **kwargs):
        return _cap_call("inline", {"op": "send", "peer": peer, "text": text, "kwargs": kwargs})

    async def form(self, text, buttons=None, **kwargs):
        return _cap_call("inline", {"op": "form", "text": text, "buttons": buttons, "kwargs": kwargs})


class SandboxInlineQuery:
    def __init__(self, payload):
        payload = payload or {}
        self.query = str(payload.get("query", ""))
        self.args = list(payload.get("args") or [])
        self._answered = False

    @property
    def from_id(self):
        return None

    async def respond(self, results=None, **kwargs):
        self._answered = True
        return {"ok": True, "results": results or []}


def _invoke_lifecycle(handler, ctx):
    import inspect
    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        params = {}
    if len(params) == 0:
        return handler()
    return handler(ctx)


class _ModulesProxy:
    async def list(self):
        return _cap_call("modules", {"op": "list"})

    async def info(self, module_id):
        return _cap_call("modules", {"op": "info", "module_id": module_id})

    async def hashes(self):
        return _cap_call("modules", {"op": "hashes"})

    async def load(self, url=None, text=None, source=None):
        payload = {"op": "load"}
        if url is not None:
            payload["url"] = url
        if text is not None:
            payload["text"] = text
        if source is not None:
            payload["source"] = source
        return _cap_call("modules", payload)

    async def unload(self, module_id, purge=False):
        return _cap_call("modules", {"op": "unload", "module_id": module_id, "purge": purge})

    async def reload(self, module_id):
        return _cap_call("modules", {"op": "reload", "module_id": module_id})

    async def reset(self, module_id):
        return _cap_call("modules", {"op": "reset", "module_id": module_id})


def _build_tools(source):
    ns = {}
    exec(compile(source, "hotaru_toolkit", "exec"), ns, ns)
    func_map = ns.get("TOOLKIT_FUNCS") or {}
    return dict(func_map)


class _LogSink(io.TextIOBase):
    def __init__(self, stream_name):
        self.stream_name = stream_name

    def write(self, data):
        if not isinstance(data, str):
            data = str(data)
        buffers = getattr(_LOCAL, "logs", None)
        if buffers is None:
            buffers = _LOCAL.logs = {}
        buf = buffers.get(self.stream_name, "") + data
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if line.strip():
                try:
                    _host_raw(json.dumps({"log": line[:2000], "stream": self.stream_name}) + "\n")
                except Exception:
                    pass
        buffers[self.stream_name] = buf
        return len(data)

    def flush(self):
        pass

    def isatty(self):
        return False


def install_output_capture():
    _LOCAL.logs = {}
    sys.stdout = _LogSink("stdout")
    sys.stderr = _LogSink("stderr")


def main():
    cfg = _proto_loads(sys.stdin.readline())
    policy = cfg.get("seccomp") or {}
    SECCOMP_CFG["allow"] = set(policy.get("allow", []))
    SECCOMP_CFG["errno"] = set(policy.get("errno", []))
    SECCOMP_CFG["kill"] = set(policy.get("kill", []))
    import errno as _errno
    try:
        install_seccomp()
    except Exception as _seccomp_exc:
        if cfg.get("seccomp_required", True):
            sys.stderr.write(f"seccomp unavailable: {_seccomp_exc}\n")
            sys.exit(1)
        sys.stderr.write(f"seccomp skipped: {_seccomp_exc}\n")
    else:
        try:
            os.splice(-1, -1, 0)
            sys.stderr.write("seccomp self-test failed: splice succeeded\n")
            sys.exit(1)
        except OSError as _exc:
            if _exc.errno != _errno.EPERM:
                sys.stderr.write(f"seccomp self-test failed: splice errno={_exc.errno}\n")
                sys.exit(1)
    install_firewall(cfg.get("protected", []), cfg.get("namespaces", False), cfg.get("deps_root", ""))
    apply_limits(
        cfg.get("mem_mb", 256),
        cfg.get("file_mb", 16),
        cfg.get("nofile", 64),
        cfg.get("cpu_seconds", 1800),
        cfg.get("net_blocked", True),
    )
    install_output_capture()
    context_ns = {}
    exec(compile(cfg["context_source"], "hotaru_context", "exec"), context_ns, context_ns)
    SandboxContext.__bases__ = (context_ns["ContextOperations"],)
    translation_ns = {}
    exec(compile(cfg["translation_source"], "hotaru_translation", "exec"), translation_ns, translation_ns)
    SandboxContext._ctx_translate_text = staticmethod(translation_ns["translate"])
    tools = _build_tools(cfg.get("toolkit_source", ""))
    ns = {"__name__": cfg.get("module_id", "sandbox"), "cap": _cap_call, "mt": _mt_call, "net": _net_call, "tools": SimpleNamespace(**tools), "logs": _WorkerLogs()}
    try:
        code = compile(cfg["source"], cfg.get("module_id", "sandbox"), "exec", flags=__future__.annotations.compiler_flag, dont_inherit=True)
        exec(code, ns, ns)
        for name, definition in ns.get("HOTARU", {}).get("template_fields", {}).items():
            if "provider" in definition:
                _template_providers[cfg["module_id"] + "/" + name] = ns["template_" + definition["provider"]]
    except BaseException as exc:
        trace = exc.__traceback__
        while trace is not None and trace.tb_next is not None:
            trace = trace.tb_next
        _host_emit({"ok": False, "error": type(exc).__name__, "detail": str(exc)[:400], "line": trace.tb_lineno if trace is not None else 0})
        sys.exit(1)
    _host_emit({"ok": True, "commands": list(cfg.get("commands", []))})
    threading.Thread(target=_heartbeat_loop, daemon=True).start()

    def _execute(req):
        if "cb" in req:
            cb_data = req["cb"]
            action_id = cb_data.get("action_id")
            cb_handler = _sandbox_callbacks.get(action_id)
            if cb_handler is None:
                if action_id == "close" or str(action_id).startswith("close_"):
                    cb_proxy = SandboxCallbackProxy(cb_data)
                    try:
                        asyncio.run(cb_proxy.delete())
                        out = {"ok": True, "result": None}
                    except BaseException as exc:
                        out = {"ok": False, "error": type(exc).__name__}
                    return out
                import hashlib as _hashlib
                import types as _types
                mod_name = str(cb_data.get("module_id", ""))
                for item_name, item_fn in ns.items():
                    if callable(item_fn):
                        qn = getattr(item_fn, "__qualname__", getattr(item_fn, "__name__", str(item_name)))
                        cands = (
                            _hashlib.sha256((mod_name + ":" + qn).encode()).hexdigest()[:24],
                            _hashlib.sha256((mod_name + ":" + str(item_name)).encode()).hexdigest()[:24],
                            _hashlib.sha256(qn.encode()).hexdigest()[:16],
                            _hashlib.sha256(str(item_name).encode()).hexdigest()[:16],
                            str(item_name),
                        )
                        if action_id in cands:
                            cb_handler = item_fn
                            _sandbox_callbacks[action_id] = item_fn
                            break
                if cb_handler is None:
                    def _find_c(code, parent=None):
                        for c in getattr(code, "co_consts", ()):
                            if isinstance(c, _types.CodeType):
                                qn = getattr(c, "co_qualname", getattr(c, "co_name", ""))
                                cands = (
                                    _hashlib.sha256((mod_name + ":" + qn).encode()).hexdigest()[:24],
                                    _hashlib.sha256((mod_name + ":" + c.co_name).encode()).hexdigest()[:24],
                                    _hashlib.sha256(qn.encode()).hexdigest()[:16],
                                    _hashlib.sha256(c.co_name.encode()).hexdigest()[:16],
                                    c.co_name,
                                )
                                if action_id in cands:
                                    return c, parent or code
                                sub, p = _find_c(c, code)
                                if sub is not None:
                                    return sub, p
                        return None, None
                    found_code, parent_code = None, None
                    for fn in ns.values():
                        if callable(fn) and hasattr(fn, "__code__"):
                            found_code, parent_code = _find_c(fn.__code__)
                            if found_code is not None:
                                break
                    if found_code is not None:
                        ctx = SandboxContext(tools, {**cb_data, "args": ()})
                        payload_val = cb_data.get("payload")
                        def _make_cell(val):
                            return (lambda: val).__closure__[0]
                        memo = {}
                        def _resolve(c, p):
                            if c in memo:
                                return memo[c]
                            cells = []
                            for var in getattr(c, "co_freevars", ()):
                                if var == "ctx":
                                    cells.append(_make_cell(ctx))
                                elif var == "tools":
                                    cells.append(_make_cell(tools))
                                elif isinstance(payload_val, dict) and var in payload_val:
                                    cells.append(_make_cell(payload_val[var]))
                                else:
                                    sibling = None
                                    search_in = [p] if p else []
                                    for sc in search_in:
                                        for inner in getattr(sc, "co_consts", ()):
                                            if isinstance(inner, _types.CodeType) and inner.co_name == var:
                                                sibling = inner
                                                break
                                        if sibling is not None:
                                            break
                                    if sibling is not None:
                                        cells.append(_make_cell(_resolve(sibling, p)))
                                    elif payload_val is not None and not isinstance(payload_val, dict) and var not in ("ctx", "tools"):
                                        cells.append(_make_cell(payload_val))
                                    elif var in ns:
                                        cells.append(_make_cell(ns[var]))
                                    else:
                                        cells.append(_make_cell(None))
                            fn = _types.FunctionType(c, ns, c.co_name, None, tuple(cells))
                            memo[c] = fn
                            return fn
                        try:
                            cb_handler = _resolve(found_code, parent_code)
                            _sandbox_callbacks[action_id] = cb_handler
                        except Exception:
                            pass
            if cb_handler is None:
                out = {"ok": False, "error": "no_callback_handler"}
            else:
                cb_proxy = SandboxCallbackProxy(cb_data)
                try:
                    import inspect as _inspect
                    try:
                        sig = _inspect.signature(cb_handler)
                    except (TypeError, ValueError):
                        sig = None
                    if sig is not None and len(sig.parameters) == 1:
                        result = cb_handler(cb_proxy)
                    else:
                        result = cb_handler(cb_proxy, cb_data.get("payload"))
                    if asyncio.iscoroutine(result):
                        result = asyncio.run(result)
                    out = {"ok": True, "result": result}
                except BaseException as exc:
                    out = {"ok": False, "error": type(exc).__name__}
            return out
        try:
            target = req.get("target") or ("command_" + req["command"])
            handler = ns.get(target)
            if handler is None:
                out = {"ok": False, "error": f"unknown_target:{target}"}
            else:
                payload = req.get("payload") or {}
                args = req.get("args") or []
                ctx = SandboxContext(tools, {**payload, "args": args})
                if str(target).startswith("inline_"):
                    query = SandboxInlineQuery(payload)
                    result = handler(query, tuple(args))
                    if asyncio.iscoroutine(result):
                        result = asyncio.run(result)
                    out = {"ok": True, "result": result}
                else:
                    invocation = SimpleNamespace(
                        name=req.get("command"),
                        args=tuple(args),
                        raw_args=payload.get("raw_args", ""),
                        source=payload.get("source", "command"),
                        message_id=payload.get("message_id"),
                        chat_id=payload.get("chat_id"),
                    )
                    if payload.get("source") in ("lifecycle", "template", "task"):
                        result = _invoke_lifecycle(handler, ctx)
                    else:
                        result = handler(ctx, invocation)
                    if asyncio.iscoroutine(result):
                        result = asyncio.run(result)
                    out = {"ok": True, "result": result}
        except BaseException as exc:
            out = {"ok": False, "error": type(exc).__name__}
        return out

    def _task(req):
        _LOCAL.rid = req.get("rid") or 0
        _busy(1)
        try:
            out = _execute(req)
        finally:
            _busy(-1)
        out["rid"] = _LOCAL.rid
        try:
            _host_emit(out)
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=_MAX_TASKS) as pool:
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            message = _proto_loads(line)
            cid = message.get("cid")
            if cid is not None:
                waiter = _reply_wait.get(cid)
                if waiter is not None:
                    waiter.put(message)
                continue
            pool.submit(_task, message)


main()
'''

SYSCALL_NUMBERS = {
    "read": 0, "write": 1, "open": 2, "close": 3, "stat": 4, "fstat": 5,
    "lstat": 6, "poll": 7, "lseek": 8, "mmap": 9, "mprotect": 10, "munmap": 11,
    "brk": 12, "rt_sigaction": 13, "rt_sigprocmask": 14, "rt_sigreturn": 15,
    "ioctl": 16, "pread64": 17, "pwrite64": 18, "readv": 19, "writev": 20,
    "access": 21, "pipe": 22, "select": 23, "sched_yield": 24, "mremap": 25,
    "msync": 26, "mincore": 27, "madvise": 28, "dup": 32, "dup2": 33,
    "nanosleep": 35, "getpid": 39, "socket": 41, "connect": 42, "accept": 43,
    "sendto": 44, "recvfrom": 45, "sendmsg": 46, "recvmsg": 47, "shutdown": 48,
    "bind": 49, "listen": 50, "getsockname": 51, "getpeername": 52,
    "socketpair": 53, "setsockopt": 54, "getsockopt": 55, "fork": 57,
    "vfork": 58, "execve": 59, "exit": 60, "wait4": 61, "kill": 62, "uname": 63,
    "fcntl": 72, "flock": 73, "fsync": 74, "fdatasync": 75, "truncate": 76,
    "ftruncate": 77, "getcwd": 79, "chdir": 80, "fchdir": 81, "rename": 82,
    "mkdir": 83, "rmdir": 84, "creat": 85, "link": 86, "unlink": 87,
    "symlink": 88, "readlink": 89, "chmod": 90, "fchmod": 91, "chown": 92,
    "fchown": 93, "lchown": 94, "umask": 95, "gettimeofday": 96,
    "getrlimit": 97, "getrusage": 98, "sysinfo": 99, "times": 100,
    "ptrace": 101, "getuid": 102, "syslog": 103, "getgid": 104, "setuid": 105,
    "setgid": 106, "geteuid": 107, "getegid": 108, "setpriority": 141,
    "getppid": 110, "getpgrp": 111, "setsid": 112, "setreuid": 113,
    "setregid": 114, "setgroups": 116, "setresuid": 117, "setresgid": 119,
    "getsid": 124, "sigaltstack": 131, "utime": 132, "mknod": 133,
    "uselib": 134, "personality": 135, "statfs": 137, "fstatfs": 138,
    "getpriority": 140, "mlock": 149, "munlock": 150, "mlockall": 151,
    "munlockall": 152, "modify_ldt": 154, "pivot_root": 155, "prctl": 157,
    "arch_prctl": 158, "adjtimex": 159, "setrlimit": 160, "chroot": 161,
    "sync": 162, "acct": 163, "settimeofday": 164, "mount": 165,
    "umount2": 166, "swapon": 167, "swapoff": 168, "reboot": 169,
    "sethostname": 170, "setdomainname": 171, "iopl": 172, "ioperm": 173,
    "init_module": 175, "delete_module": 176, "quotactl": 179, "gettid": 186,
    "tkill": 200, "futex": 202, "sched_setaffinity": 203,
    "sched_getaffinity": 204, "set_tid_address": 218, "epoll_create": 213,
    "epoll_ctl": 233, "epoll_wait": 232, "tgkill": 234, "exit_group": 231,
    "fadvise64": 221, "clock_gettime": 228, "clock_getres": 229,
    "clock_nanosleep": 230, "clock_settime": 227, "mbind": 237,
    "set_mempolicy": 238, "mq_open": 240, "mq_unlink": 241,
    "mq_timedsend": 242, "mq_timedreceive": 243, "mq_notify": 244,
    "mq_getsetattr": 245, "kexec_load": 246, "waitid": 247, "add_key": 248,
    "request_key": 249, "keyctl": 250, "ioprio_set": 251, "ioprio_get": 252,
    "inotify_init": 253, "inotify_add_watch": 254, "inotify_rm_watch": 255,
    "migrate_pages": 256, "openat": 257, "mkdirat": 258, "mknodat": 259,
    "fchownat": 260, "futimesat": 261, "newfstatat": 262, "unlinkat": 263,
    "renameat": 264, "linkat": 265, "symlinkat": 266, "readlinkat": 267,
    "fchmodat": 268, "faccessat": 269, "pselect6": 270, "ppoll": 271,
    "unshare": 272, "set_robust_list": 273, "get_robust_list": 274,
    "splice": 275, "tee": 276, "sync_file_range": 277, "vmsplice": 278,
    "move_pages": 279, "utimensat": 280, "epoll_pwait": 281, "signalfd": 282,
    "timerfd_create": 283, "eventfd": 284, "fallocate": 285,
    "timerfd_settime": 286, "timerfd_gettime": 287, "accept4": 288,
    "signalfd4": 289, "eventfd2": 290, "epoll_create1": 291, "dup3": 292,
    "pipe2": 293, "inotify_init1": 294, "preadv": 295, "pwritev": 296,
    "perf_event_open": 298, "recvmmsg": 299, "fanotify_init": 300,
    "fanotify_mark": 301, "prlimit64": 302, "name_to_handle_at": 303,
    "open_by_handle_at": 304, "clock_adjtime": 305, "syncfs": 306,
    "sendmmsg": 307, "setns": 308, "getcpu": 309, "process_vm_readv": 310,
    "process_vm_writev": 311, "kcmp": 312, "finit_module": 313,
    "sched_setattr": 314, "sched_getattr": 315, "renameat2": 316,
    "seccomp": 317, "getrandom": 318, "memfd_create": 319,
    "kexec_file_load": 320, "bpf": 321, "execveat": 322, "userfaultfd": 323,
    "membarrier": 324, "mlock2": 325, "copy_file_range": 326, "preadv2": 327,
    "pwritev2": 328, "pkey_mprotect": 329, "pkey_alloc": 330, "pkey_free": 331,
    "statx": 332, "io_pgetevents": 333, "rseq": 334, "io_uring_setup": 425,
    "io_uring_enter": 426, "io_uring_register": 427, "open_tree": 428,
    "move_mount": 429, "fsopen": 430, "fsconfig": 431, "fsmount": 432,
    "fspick": 433, "pidfd_open": 434, "clone3": 435, "close_range": 436,
    "openat2": 437, "pidfd_getfd": 438, "faccessat2": 439,
    "process_madvise": 440, "epoll_pwait2": 441, "mount_setattr": 442,
    "quotactl_fd": 443, "landlock_create_ruleset": 444, "landlock_add_rule": 445,
    "landlock_restrict_self": 446, "memfd_secret": 447, "process_mrelease": 448,
    "futex_waitv": 449, "cachestat": 451, "fchmodat2": 452,
    "map_shadow_stack": 453, "statmount": 457, "listmount": 458,
    "getdents": 78, "getdents64": 217,
}

_ALLOW_NAMES = {
    "read", "write", "open", "close", "stat", "fstat", "lstat", "poll",
    "lseek", "mmap", "mprotect", "munmap", "brk", "rt_sigaction",
    "rt_sigprocmask", "rt_sigreturn", "ioctl", "pread64", "pwrite64", "readv",
    "writev", "access", "pipe", "select", "sched_yield", "mremap", "msync",
    "mincore", "madvise", "dup", "dup2", "dup3", "pipe2", "nanosleep",
    "getpid", "exit", "exit_group", "wait4", "uname", "fcntl", "flock",
    "fsync", "fdatasync", "truncate", "ftruncate", "getcwd", "chdir",
    "fchdir", "rename", "mkdir", "rmdir", "creat", "unlink", "symlink",
    "link", "mknod", "readlink", "chmod", "fchmod", "chown", "fchown",
    "lchown", "umask", "gettimeofday", "getrlimit", "getrusage", "sysinfo",
    "times", "getuid", "getgid", "geteuid", "getegid", "getppid", "getpgrp",
    "setsid", "getsid", "sigaltstack", "utime", "statfs", "fstatfs",
    "getpriority", "prctl", "arch_prctl", "set_tid_address",
    "set_robust_list", "get_robust_list", "futex", "sched_getaffinity",
    "sched_setaffinity", "gettid", "epoll_create", "epoll_ctl", "epoll_wait",
    "epoll_create1", "epoll_pwait", "epoll_pwait2", "getdents", "getdents64",
    "openat", "newfstatat", "mkdirat", "mknodat", "unlinkat", "renameat",
    "renameat2", "linkat", "symlinkat", "readlinkat", "fchmodat", "faccessat",
    "faccessat2", "fchownat", "pselect6", "ppoll", "preadv", "pwritev",
    "preadv2", "pwritev2", "prlimit64", "clock_gettime", "clock_getres",
    "clock_nanosleep", "utimensat", "futimesat", "fallocate", "fadvise64",
    "statx", "close_range", "getrandom", "memfd_create", "rseq",
    "membarrier", "mlock", "munlock", "mlockall", "munlockall", "mlock2",
    "tkill", "tgkill", "copy_file_range", "eventfd2", "sync", "syncfs",
    "signalfd4", "inotify_init1", "socket", "socketpair",
}

_ERRNO_NAMES = {
    "connect", "accept", "accept4", "sendto", "recvfrom", "sendmsg",
    "recvmsg", "shutdown", "bind", "listen", "getsockname", "getpeername",
    "socketpair", "setsockopt", "getsockopt", "sendmmsg", "recvmmsg",
    "inotify_init", "inotify_add_watch", "inotify_rm_watch", "eventfd",
    "timerfd_create", "timerfd_settime", "timerfd_gettime", "signalfd",
    "splice", "tee", "vmsplice", "sync_file_range", "mq_open", "mq_unlink",
    "mq_timedsend", "mq_timedreceive", "mq_notify", "mq_getsetattr",
    "setpriority", "waitid", "sched_setattr", "sched_getattr", "io_pgetevents",
    "pkey_mprotect", "pkey_alloc", "pkey_free", "futex_waitv",
    "landlock_create_ruleset", "landlock_add_rule", "landlock_restrict_self",
    "ioprio_set", "ioprio_get", "getcpu", "name_to_handle_at",
    "open_by_handle_at", "openat2", "pidfd_getfd", "process_madvise",
    "quotactl_fd", "process_mrelease", "fchmodat2", "pidfd_open",
}

_KILL_NAMES = {
    "fork", "vfork", "execve", "execveat", "clone3", "kill", "ptrace",
    "mount", "umount2", "pivot_root", "chroot", "sethostname",
    "setdomainname", "setuid", "setgid", "setreuid", "setregid", "setresuid",
    "setresgid", "setgroups", "setrlimit", "reboot", "acct", "swapon",
    "swapoff", "init_module", "delete_module", "finit_module", "quotactl",
    "ioperm", "iopl", "modify_ldt", "syslog", "settimeofday", "clock_settime",
    "clock_adjtime", "adjtimex", "personality", "uselib", "kexec_load",
    "kexec_file_load", "bpf", "setns", "unshare", "userfaultfd",
    "io_uring_setup", "io_uring_enter", "io_uring_register",
    "process_vm_readv", "process_vm_writev", "kcmp", "perf_event_open",
    "add_key", "request_key", "keyctl", "mbind", "set_mempolicy",
    "migrate_pages", "move_pages", "open_tree", "move_mount", "fsopen",
    "fsconfig", "fsmount", "fspick", "mount_setattr",
    "memfd_secret", "map_shadow_stack", "statmount", "listmount", "cachestat",
    "seccomp", "fanotify_init", "fanotify_mark",
}

SECCOMP_POLICY = {
    "allow": sorted(SYSCALL_NUMBERS[name] for name in _ALLOW_NAMES),
    "errno": sorted(SYSCALL_NUMBERS[name] for name in _ERRNO_NAMES),
    "kill": sorted(SYSCALL_NUMBERS[name] for name in _KILL_NAMES),
}





class SandboxError(RuntimeError):
    pass


def _json_dict(text: str) -> dict[str, Any]:
    value = cast(object, _proto_loads(text))
    return cast('dict[str, Any]', value) if isinstance(value, dict) else {}


class ModuleSandbox:
    def __init__(
        self,
        runtime: Any,
        *,
        mem_mb: int = 256,
        file_mb: int = 16,
        nofile: int = 64,
        cpu_seconds: int = 1800,
        nproc: int = 64,
        spawn_timeout: float = 60.0,
        idle_timeout: float = 300.0,
    ) -> None:
        self.runtime = runtime
        self.mem_mb = mem_mb
        self.file_mb = file_mb
        self.nofile = nofile
        self.cpu_seconds = cpu_seconds
        self.nproc = nproc
        self.spawn_timeout = spawn_timeout
        self.idle_timeout = idle_timeout
        self._workers: dict[str, subprocess.Popen[bytes]] = {}
        self._booted: dict[str, bool] = {}
        self._last_use: dict[str, float] = {}
        self._activity: dict[str, float] = {}
        self._module_defs: dict[str, tuple[str, list[str]]] = {}
        self._module_deps: dict[str, str] = {}
        self._respond_sources: dict[Any, Any] = {}
        self._respond_contexts: dict[Any, Any] = {}
        self._cb_waiters: dict[tuple[str, str], asyncio.Future[Any]] = {}
        self._active_callback: dict[Any, Any] = {}
        self._cb_respond_pending: dict[str, list[dict[str, Any]]] = {}
        self._pending_caps: dict[str, list[dict[str, Any]]] = {}
        self._respond_pending: dict[str, list[dict[str, Any]]] = {}
        self._roundtrip_locks: dict[str, asyncio.Lock] = {}
        self._waiters: dict[str, dict[int, asyncio.Future[Any]]] = {}
        self._readers: dict[str, threading.Thread] = {}
        self._pumps: dict[str, asyncio.Task[None]] = {}
        self._queues: dict[str, asyncio.Queue[Any]] = {}
        self._pump_tasks: set[asyncio.Task[Any]] = set()
        self._rids = itertools.count(1)
        self._stdin_lock: asyncio.Lock | None = None
        self._python = os.path.realpath(sys.executable)
        self._stdlib = sysconfig.get_paths()["stdlib"]
        self._stdlib_dst = f"/opt/py/lib/python{sys.version_info.major}.{sys.version_info.minor}"
        self._rootless = os.geteuid() != 0
        requested = str(getattr(runtime.config, "sandbox", "auto") or "auto").strip().lower()
        if requested in {"portable", "none", "off", "inprocess"}:
            self._namespaces = False
        elif requested in {"namespaces", "ns", "strict"}:
            self._namespaces = True
        else:
            self._namespaces = (not self._rootless) or userns_supported()
        if self._rootless:
            self._sandbox_base = os.path.join(tempfile.gettempdir(), f"hotaru-sandbox-{os.getuid()}")
        else:
            self._sandbox_base = SANDBOX_BASE_ROOT
        os.makedirs(self._sandbox_base, exist_ok=True)
        os.chmod(self._sandbox_base, 0o700)

    def _make_preexec(self, namespaces: bool = True, deps: str | None = None) -> Any:
        if not namespaces:
            def portable() -> None:
                import os as _os

                try:
                    _os.setsid()
                except Exception:
                    pass

            return portable
        python_path = self._python
        stdlib_path = self._stdlib
        stdlib_dst = self._stdlib_dst
        sandbox_base = self._sandbox_base
        mem_mb = self.mem_mb
        file_mb = self.file_mb
        nofile = self.nofile
        cpu_seconds = self.cpu_seconds
        nproc = self.nproc
        deps_path = deps

        def preexec() -> None:
            import ctypes
            import os
            import resource
            import tempfile

            libc = ctypes.CDLL(None, use_errno=True)
            CLONE_NEWUSER = 0x10000000
            CLONE_NEWNS = 0x00020000
            CLONE_NEWIPC = 0x08000000
            CLONE_NEWUTS = 0x04000000
            CLONE_NEWNET = 0x40000000
            MS_RDONLY = 1
            MS_NOSUID = 2
            MS_NODEV = 4
            MS_BIND = 4096
            MS_REC = 16384
            MS_PRIVATE = 262144
            MS_REMOUNT = 32
            MNT_DETACH = 2

            def mount(src: bytes, dst: bytes, fstype: bytes | None, flags: int, data: bytes | None = None) -> None:
                if libc.mount(src, dst, fstype, flags, data) != 0:
                    raise OSError(ctypes.get_errno(), f"mount failed: {src!r} -> {dst!r}")

            try:
                os.setsid()
            except Exception:
                pass
            outer_uid = os.getuid()
            outer_gid = os.getgid()
            rootless = outer_uid != 0
            if rootless:
                if libc.unshare(CLONE_NEWUSER) != 0:
                    raise OSError(ctypes.get_errno(), "unshare user namespace failed")
                with open("/proc/self/setgroups", "w") as _f:
                    _f.write("deny")
                with open("/proc/self/uid_map", "w") as _f:
                    _f.write(f"0 {outer_uid} 1")
                with open("/proc/self/gid_map", "w") as _f:
                    _f.write(f"0 {outer_gid} 1")
            if libc.unshare(CLONE_NEWNS | CLONE_NEWIPC | CLONE_NEWUTS | CLONE_NEWNET) != 0:
                raise OSError(ctypes.get_errno(), "unshare failed")
            mount(b"none", b"/", None, MS_REC | MS_PRIVATE)
            os.makedirs(sandbox_base, exist_ok=True)
            for entry in os.listdir(sandbox_base):
                candidate = os.path.join(sandbox_base, entry)
                try:
                    if os.path.isdir(candidate) and not os.listdir(candidate):
                        os.rmdir(candidate)
                except OSError:
                    pass
            newroot = tempfile.mkdtemp(prefix="root.", dir=sandbox_base)
            mount(b"tmpfs", newroot.encode(), b"tmpfs", MS_NOSUID | MS_NODEV, b"size=8m,mode=0755")

            def bind_ro(src: str, dst: str, is_dir: bool, dev: bool = False) -> None:
                dst_abs = os.path.join(newroot, dst.lstrip("/"))
                if not os.path.exists(dst_abs):
                    if is_dir:
                        os.makedirs(dst_abs, exist_ok=True)
                    else:
                        os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
                        with open(dst_abs, "wb"):
                            pass
                remount_flags = MS_BIND | MS_REC | MS_REMOUNT | MS_RDONLY | MS_NOSUID
                if not dev:
                    remount_flags |= MS_NODEV
                mount(src.encode(), dst_abs.encode(), None, MS_BIND | MS_REC)
                mount(src.encode(), dst_abs.encode(), None, remount_flags)

            bind_ro(python_path, "/usr/bin/python3", False)
            bind_ro(stdlib_path, stdlib_dst, True)
            if deps_path:
                bind_ro(deps_path, "/opt/pkgs", True)
            bind_ro("/usr/lib", "/usr/lib", True)
            if os.path.isdir("/usr/lib64"):
                bind_ro("/usr/lib64", "/usr/lib64", True)
            for link_name in ("/lib", "/lib64"):
                if os.path.islink(link_name):
                    target = os.readlink(link_name)
                    dst = os.path.join(newroot, link_name.lstrip("/"))
                    if not os.path.lexists(dst):
                        os.symlink(target, dst)
                elif os.path.isdir(link_name):
                    bind_ro(link_name, link_name, True)
            for dev in ("/dev/null", "/dev/zero", "/dev/urandom"):
                bind_ro(dev, dev, False, dev=True)
            tmp_dir = os.path.join(newroot, "tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            mount(b"tmpfs", tmp_dir.encode(), b"tmpfs", MS_NOSUID | MS_NODEV, b"size=32m,mode=1777")
            os.makedirs(os.path.join(newroot, "old_root"), exist_ok=True)
            os.chdir(newroot)
            if libc.pivot_root(b".", b"./old_root") != 0:
                raise OSError(ctypes.get_errno(), "pivot_root failed")
            os.chdir("/")
            if libc.umount2(b"/old_root", MNT_DETACH) != 0:
                raise OSError(ctypes.get_errno(), "old root detach failed")
            os.rmdir("/old_root")
            os.umask(0o077)
            resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024, mem_mb * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_mb * 1024 * 1024, file_mb * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            if not rootless:
                os.setgroups([])
                os.setgid(SANDBOX_GID)
                os.setuid(SANDBOX_UID)

        return preexec

    def _spawn(self, module_id: str, source: str, commands: list[str], *, namespaces: bool | None = None) -> subprocess.Popen[bytes]:
        if namespaces is None:
            namespaces = self._namespaces
        deps = self._module_deps.get(module_id)
        hello = {
            "module_id": module_id,
            "source": source,
            "commands": commands,
            "toolkit_source": _TOOLKIT_SOURCE,
            "context_source": _CONTEXT_SOURCE,
            "translation_source": _TRANSLATION_SOURCE,
            "mem_mb": self.mem_mb,
            "file_mb": self.file_mb,
            "nofile": self.nofile,
            "cpu_seconds": self.cpu_seconds,
            "net_blocked": True,
            "seccomp": SECCOMP_POLICY,
            "seccomp_required": namespaces,
            "namespaces": namespaces,
            "deps_root": deps or "",
            "protected": [
                str(self.runtime.config.session_dir),
                str(self.runtime.config.session_dir / f"{self.runtime.config.session_name}.vault"),
                str(self.runtime.config.session_dir / f"{self.runtime.config.session_name}.session"),
            ],
        }
        home = os.environ.get("HOME") or os.path.expanduser("~")
        cli_config = {
            "GH_CONFIG_DIR": os.path.join(home, ".config", "gh"),
            "GIT_CONFIG_GLOBAL": os.path.join(home, ".gitconfig"),
        }
        if namespaces:
            argv = ["/usr/bin/python3", "-s", "-c", WORKER_SOURCE]
            env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "PYTHONPATH": "/opt/pkgs" if deps else "", "PYTHONHOME": "/opt/py", **cli_config}
            cwd = "/"
        else:
            argv = [self._python, "-s", "-S", "-c", WORKER_SOURCE]
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": self._sandbox_base,
                "PYTHONPATH": deps or "",
                "PYTHONDONTWRITEBYTECODE": "1",
                "TMPDIR": self._sandbox_base,
                "LANG": "C.UTF-8",
                **cli_config,
            }
            cwd = self._sandbox_base
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            preexec_fn=self._make_preexec(namespaces, deps),
        )
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write((_proto_dumps(hello) + "\n").encode("utf-8"))
        process.stdin.flush()
        ready = self._readline(process)
        payload = _json_dict(ready) if ready else {}
        if not payload.get("ok"):
            stderr_tail = ""
            try:
                process.kill()
                _, stderr = process.communicate(timeout=5)
                stderr_tail = stderr.decode("utf-8", errors="replace")[-800:]
            except Exception:
                pass
            detail = str(payload.get("detail") or "")
            line = int(payload.get("line") or 0)
            location = f" line {line}" if line else ""
            raise SandboxError(f"sandbox worker failed to boot: {payload.get('error', 'no output')}{location}: {detail} {stderr_tail}".strip())
        self._workers[module_id] = process
        self._booted[module_id] = True
        self._last_use[module_id] = time.monotonic()
        return process

    def _spawn_worker(self, module_id: str, source: str, commands: list[str]) -> subprocess.Popen[bytes]:
        return self._spawn(module_id, source, commands)

    def _sink_worker_log(self, module_id: str, message: dict[str, Any]) -> None:
        observatory = getattr(self.runtime, "observatory", None)
        if observatory is None:
            return
        stream = str(message.get("stream") or "stdout")
        level = "error" if stream == "stderr" else "info"
        text = message.get("log")
        if not isinstance(text, str):
            return
        observatory.emit("module", "output", level=level, module=module_id, msg=text[:2048])

    def _readline(self, process: subprocess.Popen[bytes]) -> str | None:
        assert process.stdout is not None
        line = process.stdout.readline()
        return line.decode("utf-8", errors="replace").strip() if line else None

    def deps_root(self) -> Path:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        return Path(base) / "hotaru" / "module-deps"

    async def ensure_module_deps(self, module_id: str, source: str, extra: Sequence[str] = ()) -> str | None:
        from hotaru.deps import install_module_deps

        root = self.deps_root()
        loop = asyncio.get_running_loop()
        path = await loop.run_in_executor(None, lambda: install_module_deps(root, module_id, source, extra))
        if path:
            self._module_deps[module_id] = path
        else:
            self._module_deps.pop(module_id, None)
        return path

    async def start_module(self, module_id: str, source: str, commands: list[str]) -> bool:
        self._module_defs[module_id] = (source, commands)
        current = self._workers.get(module_id)
        if current is not None and current.poll() is None:
            return True
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._spawn_worker(module_id, source, commands))
        return True

    async def call(self, module_id: str, command: str, args: list[str], payload: dict[str, Any], source: Any = None, target: str | None = None, context: Any = None) -> Any:
        context = context or self.runtime.context_factory.create(module_id, source)
        config = context.config
        payload = dict(payload, started_at=self.runtime.started_at, templates={
            key: {"value": config.get(key), "fields": field.template_fields, "legacy_braces": field.legacy_braces}
            for key, field in config.schema.items() if field.template_fields is not None
        })
        payload.setdefault("language", context.i18n.language)
        payload["from_id"] = getattr(source, "from_id", None)
        payload["prefix"] = self.runtime.config.prefix
        rid = self._next_rid()
        self._respond_contexts[(module_id, rid)] = context
        if source is not None:
            self._respond_sources[(module_id, rid)] = source
        try:
            result = await self._roundtrip(module_id, {"command": command, "args": args, "payload": payload, "target": target}, rid)
        finally:
            self._respond_contexts.pop((module_id, rid), None)
            self._respond_sources.pop((module_id, rid), None)
        if not isinstance(result, dict) or not result.get("ok"):
            raise SandboxError(f"sandbox call failed: {result.get('error') if isinstance(result, dict) else 'malformed'}")
        return result.get("result")

    async def cap_call(self, module_id: str, capability: str, payload: dict[str, Any]) -> Any:
        result = await self._roundtrip(module_id, {"command": "$cap." + capability, "args": [], "payload": payload})
        if not isinstance(result, dict) or not result.get("ok"):
            raise SandboxError(f"sandbox capability failed: {result.get('error') if isinstance(result, dict) else 'malformed'}")
        return result.get("result")

    async def roundtrip(self, module_id: str, request: dict[str, Any], rid: int | None = None) -> dict[str, Any] | None:
        return await self._roundtrip(module_id, request, rid)

    def _next_rid(self) -> int:
        return next(self._rids)

    def _roundtrip_lock(self, module_id: str) -> asyncio.Lock:
        lock = self._roundtrip_locks.get(module_id)
        if lock is None:
            lock = asyncio.Lock()
            self._roundtrip_locks[module_id] = lock
        return lock

    def _write_lock(self) -> asyncio.Lock:
        if self._stdin_lock is None:
            self._stdin_lock = asyncio.Lock()
        return self._stdin_lock

    async def _ensure_worker(self, module_id: str) -> subprocess.Popen[bytes]:
        process = self._workers.get(module_id)
        if process is None or process.poll() is not None:
            if module_id not in self._module_defs:
                raise SandboxError(f"sandbox worker is not running: {module_id}")
            source, commands = self._module_defs[module_id]
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._spawn_worker(module_id, source, commands))
            process = self._workers.get(module_id)
        if process is None or process.poll() is not None:
            raise SandboxError(f"sandbox worker is not running: {module_id}")
        self._start_reader(module_id, process)
        return process

    def _start_reader(self, module_id: str, process: subprocess.Popen[bytes]) -> None:
        reader = self._readers.get(module_id)
        if reader is not None and reader.is_alive():
            return
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._queues[module_id] = queue

        def read() -> None:
            while True:
                line = self._readline(process)
                if not line:
                    break
                loop.call_soon_threadsafe(queue.put_nowait, _json_dict(line))
            loop.call_soon_threadsafe(queue.put_nowait, None)

        thread = threading.Thread(target=read, daemon=True)
        self._readers[module_id] = thread
        thread.start()
        self._pumps[module_id] = loop.create_task(self._pump(module_id, queue))

    async def _pump(self, module_id: str, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        try:
            while True:
                message = await queue.get()
                if message is None:
                    break
                self._activity[module_id] = time.monotonic()
                if "cap" in message:
                    self._pending_caps.setdefault(module_id, []).append(message)
                    self._spawn_pump_task(self._serve_caps(module_id))
                    continue
                if "respond" in message:
                    self._respond_pending.setdefault(module_id, []).append(message)
                    self._spawn_pump_task(self._serve_respond(module_id))
                    continue
                if "cb_respond" in message:
                    self._cb_respond_pending.setdefault(module_id, []).append(message)
                    self._spawn_pump_task(self._serve_cb_respond(module_id))
                    continue
                if "log" in message:
                    self._sink_worker_log(module_id, message)
                    continue
                rid = message.get("rid")
                waiter = self._waiters.get(module_id, {}).pop(rid, None) if isinstance(rid, int) else None
                if waiter is not None and not waiter.done():
                    waiter.set_result(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._kill_worker(module_id, SandboxError(f"sandbox worker stopped: {module_id}"))

    def _spawn_pump_task(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._pump_tasks.add(task)
        task.add_done_callback(self._pump_tasks.discard)

    def _fail_waiters(self, module_id: str, error: Exception) -> None:
        for waiter in self._waiters.pop(module_id, {}).values():
            if not waiter.done():
                waiter.set_exception(error)

    def _kill_worker(self, module_id: str, error: Exception) -> None:
        process = self._workers.get(module_id)
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        self._fail_waiters(module_id, error)

    async def _roundtrip(self, module_id: str, request: dict[str, Any], rid: int | None = None) -> dict[str, Any] | None:
        self._last_use[module_id] = time.monotonic()
        process = await self._ensure_worker(module_id)
        rid = rid if rid is not None else self._next_rid()
        loop = asyncio.get_running_loop()
        started = self._activity[module_id] = time.monotonic()
        waiter: asyncio.Future[dict[str, Any]] = loop.create_future()
        waiter.add_done_callback(lambda done: done.cancelled() or done.exception())
        self._waiters.setdefault(module_id, {})[rid] = waiter
        try:
            async with self._roundtrip_lock(module_id):
                await self._write_reply(process, dict(request, rid=rid))
            while True:
                done, _ = await asyncio.wait({waiter}, timeout=0.2)
                if done:
                    return waiter.result()
                last = self._activity.get(module_id, started)
                window = self.idle_timeout if last > started else self.spawn_timeout
                if time.monotonic() - last >= window:
                    detail = f"sandbox worker stalled: no activity for {time.monotonic() - last:.0f}s ({module_id})"
                    self._kill_worker(module_id, SandboxError(detail))
                    raise SandboxError(detail)
                if process.poll() is not None:
                    raise SandboxError(f"sandbox worker died during call: {module_id}")
        finally:
            self._waiters.get(module_id, {}).pop(rid, None)

    async def _write_reply(self, process: subprocess.Popen[bytes], reply: dict[str, Any]) -> None:
        async with self._write_lock():
            def write() -> None:
                if process.poll() is None and process.stdin is not None:
                    process.stdin.write((_proto_dumps(reply) + "\n").encode("utf-8"))
                    process.stdin.flush()

            await asyncio.get_running_loop().run_in_executor(None, write)

    async def _serve_caps(self, module_id: str) -> None:
        caps = self._pending_caps.pop(module_id, [])
        process = self._workers.get(module_id)
        cap_host = getattr(self.runtime, "cap_host", None)
        for message in caps:
            name = message.get("cap")
            payload_value = message.get("payload")
            payload = cast('dict[str, Any]', payload_value) if isinstance(payload_value, dict) else {}
            rid = message.get("rid")
            reply = {"kind": "cap_result", "cid": message.get("cid"), "ok": False, "error": "capability host is unavailable"}
            if cap_host is not None or name == "$template":
                try:
                    if name == "$template":
                        context = self._respond_contexts.get((module_id, rid))
                        if context is None:
                            context = self.runtime.context_factory.create(module_id, None)
                        if set(payload) - {"key", "values", "local"} or not isinstance(payload.get("key"), str) or not isinstance(payload.get("local", {}), dict):
                            raise ValueError("Invalid template request")
                        result = await self.runtime.modules.render_template(context, payload["key"], payload.get("values", {}), payload.get("local", {}))
                    else:
                        if cap_host is None:
                            raise RuntimeError("capability host is unavailable")
                        result = await cap_host.call(module_id, name, payload)
                    reply = {"kind": "cap_result", "cid": message.get("cid"), "ok": True, "result": result}
                except Exception as exc:
                    reply = {"kind": "cap_result", "cid": message.get("cid"), "ok": False, "error_type": type(exc).__name__, "error": f"{type(exc).__name__}: {exc}"[:200]}
            if process is self._workers.get(module_id) and process is not None and process.poll() is None and process.stdin is not None:
                await self._write_reply(process, reply)

    async def _serve_respond(self, module_id: str) -> None:
        pending = self._respond_pending.pop(module_id, [])
        for message in pending:
            payload_value = message.get("respond")
            payload = cast('dict[str, Any]', payload_value) if isinstance(payload_value, dict) else {}
            source = self._respond_sources.get((module_id, message.get("rid")))
            reply = {"kind": "respond_result", "cid": message.get("cid"), "ok": False, "error": "respond failed"}
            try:
                result = await self._trusted_respond(module_id, source, payload)
                reply = {"kind": "respond_result", "cid": message.get("cid"), "ok": True, "result": result}
            except Exception as exc:
                reply = {"kind": "respond_result", "cid": message.get("cid"), "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
            process = self._workers.get(module_id)
            if process is not None and process.poll() is None and process.stdin is not None:
                await self._write_reply(process, reply)

    async def _serve_cb_respond(self, module_id: str) -> None:
        pending = self._cb_respond_pending.pop(module_id, [])
        process = self._workers.get(module_id)
        for message in pending:
            data_value = message.get("cb_respond")
            data = cast('dict[str, Any]', data_value) if isinstance(data_value, dict) else {}
            callback = self._active_callback.get((module_id, message.get("rid")))
            result = {"ok": False, "error": "callback context is unavailable"}
            try:
                if callback is None:
                    raise PermissionError("callback context is unavailable")
                action = data.get("action")
                if action == "respond":
                    with trusted_scope():
                        value = await callback.respond(str(data.get("text", "")), alert=bool(data.get("alert", False)))
                elif action == "edit":
                    kwargs: dict[str, Any] = {}
                    for k in ("parse_mode", "rich", "style"):
                        if k in data:
                            kwargs[k] = data[k]
                    markup = data.get("reply_markup")
                    if markup is not None:
                        if isinstance(markup, list):
                            with trusted_scope():
                                kwargs["buttons"] = self._sandbox_buttons(module_id, markup, getattr(callback, "chat_id", None), getattr(callback, "from_id", None))
                        elif isinstance(markup, dict) and "inline_keyboard" in markup:
                            with trusted_scope():
                                kwargs["buttons"] = self._sandbox_buttons(module_id, markup["inline_keyboard"], getattr(callback, "chat_id", None), getattr(callback, "from_id", None))
                        else:
                            kwargs["reply_markup"] = markup
                    with trusted_scope():
                        value = await callback.edit(str(data.get("text", "")), **kwargs)
                elif action == "delete":
                    with trusted_scope():
                        value = await callback.delete()
                else:
                    raise PermissionError("unknown callback action")
                result = {"ok": True, "result": value}
            except Exception as exc:
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
            if process is not None and process.poll() is None and process.stdin is not None:
                await self._write_reply(process, {"cb_respond_result": result, "cid": message.get("cid")})

    async def _trusted_respond(self, module_id: str, source: Any, payload: dict[str, Any]) -> Any:
        from hotaru.response import FormHandle, Response
        from types import SimpleNamespace
        if "prepare" in payload:
            from hotaru.response import ModuleContext
            prepare = payload["prepare"]
            if not isinstance(prepare, dict):
                raise ValueError("context preparation requires a mapping")
            prepare = cast('dict[str, Any]', prepare)
            return await getattr(ModuleContext, "_ctx_prepare")(prepare.get("text"), prepare.get("parse_mode"), prepare.get("buttons"))
        if "form" in payload:
            from hotaru.runtime import Runtime
            runtime = cast(Runtime, self.runtime)
            entry = runtime.get_form(str(payload["form"]))
            if not entry or entry[4].get("module_id") != module_id:
                if payload.get("op") == "allowed":
                    return False
                raise PermissionError("module form is unavailable")
            handle, form_source, _, _, options = entry
            actor = options.get("callback_actor")
            command = str(options.get("command") or "")
            spec = self.runtime.kernel.registry.resolve_name(command)
            allowed = bool(spec and spec.module_id == module_id and actor and self.runtime.kernel.is_authorized(SimpleNamespace(from_id=actor, chat_id=form_source.chat_id), spec))
            if payload.get("op") == "allowed":
                return allowed
            if not allowed:
                raise PermissionError("form command is no longer authorized")
            if payload.get("op") != "edit":
                raise PermissionError("unknown form operation")
            buttons = payload.get("buttons")
            if buttons is not None:
                buttons = self._sandbox_buttons(module_id, buttons, form_source.chat_id, actor)
            with trusted_scope():
                await handle.edit(str(payload.get("content", "")), buttons=buttons)
            return True
        if source is None:
            raise PermissionError("no message source is bound to this call")
        context = self._respond_contexts.get(module_id)
        if context is None:
            context = self.runtime.context_factory.create(module_id, source)
            self._respond_contexts[module_id] = context
        if "media_rpc" in payload:
            media_rpc = payload["media_rpc"]
            if not isinstance(media_rpc, dict):
                raise ValueError("file response requires a mapping")
            media_rpc = cast('dict[str, Any]', media_rpc)
            media_kwargs = media_rpc.get("kwargs")
            if not isinstance(media_kwargs, dict):
                raise ValueError("file response arguments require a mapping")
            return await context._ctx_media_rpc(str(media_rpc.get("method", "")), **cast('dict[str, Any]', media_kwargs))
        kwargs = dict(payload.get("kwargs") or {})
        if kwargs.get("media") is not None or kwargs.get("file") is not None or payload.get("content") is not None and not isinstance(payload["content"], str):
            raise PermissionError("sandbox respond media must go through files capability")
        if kwargs.get("buttons"):
            kwargs["buttons"] = self._sandbox_buttons(module_id, kwargs["buttons"], getattr(source, "chat_id", None), getattr(source, "from_id", None))
        for key in ("module_options", "module_id", "callback_actor", "chat_id", "peer", "bot"):
            kwargs.pop(key, None)
        with trusted_scope():
            result = await context.respond(payload.get("content"), **kwargs)
        if isinstance(result, Response):
            return result.message
        if isinstance(result, FormHandle):
            return result.key if payload.get("form_handle") else result.value
        if isinstance(result, list):
            return [item.message if isinstance(item, Response) else item for item in cast('list[object]', result)]
        return result

    def _sandbox_buttons(self, module_id: str, buttons: Any, chat_id: Any, actor: Any = None) -> Any:
        with trusted_scope():
            router = getattr(self.runtime, "callbacks", None)
            owner = actor or getattr(getattr(self.runtime, "kernel", None), "owner_id", None)
            normalized: list[list[dict[str, Any]]] = []
            button_values = cast('list[Any]', buttons) if isinstance(buttons, list) else []
            rows: list[Any] = button_values if button_values and isinstance(button_values[0], list) else [buttons]
            for row in rows:
                if not isinstance(row, list):
                    row = [row]
                out_row: list[dict[str, Any]] = []
                for btn in cast('list[Any]', row):
                    if not isinstance(btn, dict):
                        continue
                    button = cast('dict[str, Any]', btn)
                    item: dict[str, Any] = {"text": button.get("text", "")}
                    if button.get("style"):
                        item["style"] = button["style"]
                    if button.get("icon_custom_emoji_id"):
                        item["icon_custom_emoji_id"] = button["icon_custom_emoji_id"]
                    if button.get("url"):
                        item["url"] = button["url"]
                    elif button.get("action_id"):
                        action_id = str(button["action_id"])
                        if action_id == "close":
                            if router is not None:
                                if not router.module_action_exists(module_id, action_id):
                                    router.register_module_action_id(module_id, action_id, getattr(router, "_default_close_handler", None))
                                binding = CallbackBinding(int(owner or 0), None, 0)
                                item["callback_data"] = router.issue_module(module_id, action_id, binding, button.get("payload"))
                        elif router is not None:
                            if not router.module_action_exists(module_id, action_id):
                                router.register_module_action_id(module_id, action_id, self._make_sandbox_cb(module_id, action_id))
                            binding = CallbackBinding(int(owner or 0), None, 0)
                            item["callback_data"] = router.issue_module(module_id, action_id, binding, button.get("payload"))
                        else:
                            raise PermissionError("callback store is unavailable")
                        item["_action_id"] = action_id
                        item["_payload"] = button.get("payload")
                    elif button.get("callback_data"):
                        item["callback_data"] = button["callback_data"]
                    else:
                        for k, v in button.items():
                            if k not in item:
                                item[k] = v
                    out_row.append(item)
                normalized.append(out_row)
            return normalized

    def has_module(self, module_id: str) -> bool:
        return module_id in self._workers

    def make_sandbox_cb(self, module_id: str, action_id: str) -> Any:
        return self._make_sandbox_cb(module_id, action_id)

    def _make_sandbox_cb(self, module_id: str, action_id: str) -> Any:
        async def handler(callback: Any, payload: Any) -> object:
            rid = self._next_rid()
            self._active_callback[(module_id, rid)] = callback
            request = {
                "cb": {
                    "module_id": module_id,
                    "action_id": action_id,
                    "payload": payload,
                    "chat_id": getattr(callback, "chat_id", None),
                    "message_id": getattr(callback, "msg_id", None),
                    "from_id": getattr(callback, "from_id", None),
                    "inline_message_id": getattr(callback, "inline_message_id", None),
                }
            }
            try:
                with trusted_scope():
                    result = await self.roundtrip(module_id, request, rid)
                if result is None:
                    raise PermissionError("sandbox callback failed: malformed")
                if result.get("ok"):
                    return result.get("result")
                raise PermissionError(f"sandbox callback failed: {result.get('error', 'malformed')}")
            finally:
                self._active_callback.pop((module_id, rid), None)

        return handler

    def stop_module(self, module_id: str) -> bool:
        process = self._workers.get(module_id)
        pump = self._pumps.pop(module_id, None)
        if pump is not None:
            pump.cancel()
        self._readers.pop(module_id, None)
        self._queues.pop(module_id, None)
        self._fail_waiters(module_id, SandboxError(f"sandbox worker stopped: {module_id}"))
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).error("sandbox stop failed: %s", type(exc).__name__)
                    return False
        self._module_defs.pop(module_id, None)
        self._module_deps.pop(module_id, None)
        self._workers.pop(module_id, None)
        self._booted.pop(module_id, None)
        self._last_use.pop(module_id, None)
        self._activity.pop(module_id, None)
        return process is not None

    def reap_idle(self, idle_seconds: float) -> list[str]:
        now = time.monotonic()
        reaped: list[str] = []
        for module_id, process in list(self._workers.items()):
            if process.poll() is not None:
                continue
            if now - self._last_use.get(module_id, now) < idle_seconds:
                continue
            if self._waiters.get(module_id):
                continue
            try:
                process.kill()
            except Exception:
                continue
            reaped.append(module_id)
        return reaped

    def stop_all(self) -> None:
        for module_id in list(self._workers):
            self.stop_module(module_id)
