from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from typing import Any

from . import toolkit as _toolkit

_TOOLKIT_SOURCE = Path(_toolkit.__file__).read_text(encoding="utf-8")

SANDBOX_BASE_ROOT = "/run/hotaru-sandbox"
SANDBOX_UID = 65534
SANDBOX_GID = 65534

WORKER_SOURCE = r'''
import asyncio
import json
import os
import resource
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

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


def _respond_call(payload):
    req = json.dumps({"respond": payload})
    sys.stdout.write(req + "\n")
    sys.stdout.flush()
    while True:
        line = sys.stdin.readline()
        if not line:
            raise OSError("host closed the sandbox channel")
        resp = json.loads(line)
        if resp.get("kind") != "respond_result":
            continue
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


class SandboxContext:
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
    def message(self):
        return SimpleNamespace(
            chat_id=self.chat_id,
            id=self.message_id,
            is_me=self._msg.get("out", False),
            out=self._msg.get("out", False),
        )

    def args_list(self):
        return list(self.args)

    @staticmethod
    def escape(value):
        import html as _html
        return _html.escape(str(value), quote=False)

    async def respond(self, content=None, **kwargs):
        return _respond_call({"content": content, "kwargs": kwargs})

    async def smart_answer(self, content=None, **kwargs):
        return await self.respond(content, **kwargs)

    async def answer(self, text=None, **kwargs):
        if text is not None:
            kwargs["text"] = text
        value = kwargs.pop("text", "")
        return _respond_call({"content": value, "kwargs": kwargs})

    async def reply_html(self, text, **kwargs):
        kwargs.setdefault("output", "reply")
        return await self.answer(text, **kwargs)

    async def edit_html(self, text, **kwargs):
        kwargs.setdefault("output", "edit")
        return await self.answer(text, **kwargs)

    async def answer_file(self, media, **kwargs):
        return await self.send_file(media, kwargs.pop("caption", None), **kwargs)

    async def answer_media(self, media, **kwargs):
        kwargs.setdefault("output", "reply")
        return await self.send_file(media, **kwargs)

    async def answer_rich(self, rich_message, **kwargs):
        return await self.send_rich(rich_message, **kwargs)

    async def send_rich(self, html, **kwargs):
        kwargs["rich"] = True
        return _respond_call({"content": html, "kwargs": kwargs})

    async def send_file(self, media, caption=None, **kwargs):
        kwargs["media"] = media
        return _respond_call({"content": caption or "", "kwargs": kwargs})

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
        rich = {"_": "inputRichMessageHTML", "html": html_text} if isinstance(html_text, str) else html_text
        return await self.call("messages.sendMessage", {"rich_message": rich, **kwargs})

    async def edit_message(self, message_id, text, **kwargs):
        return await self.call("messages.editMessage", {"id": message_id, "message": text, **kwargs})

    async def edit_rich(self, message_id, html_text, **kwargs):
        rich = {"_": "inputRichMessageHTML", "html": html_text} if isinstance(html_text, str) else html_text
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

    async def unload(self, module_id):
        return _cap_call("modules", {"op": "unload", "module_id": module_id})

    async def reload(self, module_id):
        return _cap_call("modules", {"op": "reload", "module_id": module_id})


def _build_tools(source):
    ns = {}
    exec(compile(source, "hotaru_toolkit", "exec"), ns, ns)
    func_map = ns.get("TOOLKIT_FUNCS") or {}
    return dict(func_map)


def main():
    cfg = json.loads(sys.stdin.readline())
    policy = cfg.get("seccomp") or {}
    SECCOMP_CFG["allow"] = set(policy.get("allow", []))
    SECCOMP_CFG["errno"] = set(policy.get("errno", []))
    SECCOMP_CFG["kill"] = set(policy.get("kill", []))
    install_seccomp()
    import errno as _errno
    try:
        os.splice(-1, -1, 0)
        sys.stderr.write("seccomp self-test failed: splice succeeded\n")
        sys.exit(1)
    except OSError as _exc:
        if _exc.errno != _errno.EPERM:
            sys.stderr.write(f"seccomp self-test failed: splice errno={_exc.errno}\n")
            sys.exit(1)
    install_firewall(cfg.get("protected", []))
    apply_limits(
        cfg.get("mem_mb", 256),
        cfg.get("file_mb", 16),
        cfg.get("nofile", 64),
        cfg.get("cpu_seconds", 1800),
        cfg.get("net_blocked", True),
    )
    tools = _build_tools(cfg.get("toolkit_source", ""))
    ns = {"__name__": cfg.get("module_id", "sandbox"), "cap": _cap_call, "mt": _mt_call, "net": _net_call, "tools": SimpleNamespace(**tools)}
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
                payload = req.get("payload") or {}
                args = req.get("args") or []
                ctx = SandboxContext(tools, {**payload, "args": args})
                invocation = SimpleNamespace(
                    name=req.get("command"),
                    args=tuple(args),
                    source=payload.get("source", "command"),
                    message_id=payload.get("message_id"),
                    chat_id=payload.get("chat_id"),
                )
                result = handler(ctx, invocation)
                if asyncio.iscoroutine(result):
                    result = asyncio.run(result)
                out = {"ok": True, "result": result}
        except BaseException as exc:
            out = {"ok": False, "error": type(exc).__name__}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


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
    "quotactl_fd", "process_mrelease", "fchmodat2",
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
    "fsconfig", "fsmount", "fspick", "mount_setattr", "pidfd_open",
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
        spawn_timeout: float = 10.0,
        call_timeout: float = 15.0,
    ) -> None:
        self.runtime = runtime
        self.mem_mb = mem_mb
        self.file_mb = file_mb
        self.nofile = nofile
        self.cpu_seconds = cpu_seconds
        self.nproc = nproc
        self.spawn_timeout = spawn_timeout
        self.call_timeout = call_timeout
        self._workers: dict[str, subprocess.Popen] = {}
        self._booted: dict[str, bool] = {}
        self._respond_sources: dict[str, Any] = {}
        self._python = os.path.realpath(sys.executable)
        self._stdlib = sysconfig.get_paths()["stdlib"]
        self._stdlib_dst = f"/opt/py/lib/python{sys.version_info.major}.{sys.version_info.minor}"
        self._rootless = os.geteuid() != 0
        if self._rootless:
            self._sandbox_base = os.path.join(tempfile.gettempdir(), f"hotaru-sandbox-{os.getuid()}")
        else:
            self._sandbox_base = SANDBOX_BASE_ROOT
        os.makedirs(self._sandbox_base, exist_ok=True)
        os.chmod(self._sandbox_base, 0o700)

    def _make_preexec(self):
        python_path = self._python
        stdlib_path = self._stdlib
        stdlib_dst = self._stdlib_dst
        sandbox_base = self._sandbox_base
        mem_mb = self.mem_mb
        file_mb = self.file_mb
        nofile = self.nofile
        cpu_seconds = self.cpu_seconds
        nproc = self.nproc

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

    def _spawn(self, module_id: str, source: str, commands: list[str]) -> subprocess.Popen:
        hello = {
            "module_id": module_id,
            "source": source,
            "commands": commands,
            "toolkit_source": _TOOLKIT_SOURCE,
            "mem_mb": self.mem_mb,
            "file_mb": self.file_mb,
            "nofile": self.nofile,
            "cpu_seconds": self.cpu_seconds,
            "net_blocked": True,
            "seccomp": SECCOMP_POLICY,
            "protected": [
                str(self.runtime.config.session_dir),
                str(self.runtime.config.session_dir / f"{self.runtime.config.session_name}.vault"),
                str(self.runtime.config.session_dir / f"{self.runtime.config.session_name}.session"),
            ],
        }
        process = subprocess.Popen(
            ["/usr/bin/python3", "-s", "-c", WORKER_SOURCE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "PYTHONPATH": "", "PYTHONHOME": "/opt/py"},
            preexec_fn=self._make_preexec(),
        )
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write((json.dumps(hello) + "\n").encode("utf-8"))
        process.stdin.flush()
        ready = self._readline(process)
        payload = json.loads(ready) if ready else {}
        if not payload.get("ok"):
            stderr_tail = ""
            try:
                process.kill()
                _, stderr = process.communicate(timeout=5)
                stderr_tail = stderr.decode("utf-8", errors="replace")[-800:]
            except Exception:
                pass
            raise SandboxError(f"sandbox worker failed to boot: {payload.get('error', 'no output')} {stderr_tail}".strip())
        self._workers[module_id] = process
        self._booted[module_id] = True
        return process

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

    async def call(self, module_id: str, command: str, args: list[str], payload: dict[str, Any], source: Any = None) -> Any:
        if source is not None:
            self._respond_sources[module_id] = source
        try:
            result = await self.roundtrip(module_id, {"command": command, "args": args, "payload": payload})
        finally:
            if source is not None:
                self._respond_sources.pop(module_id, None)
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
        self._respond_pending = getattr(self, "_respond_pending", [])

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
                if isinstance(message, dict) and "respond" in message:
                    self._respond_pending.append(message)
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
                if self._respond_pending:
                    await self._serve_respond(module_id)
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

    async def _serve_respond(self, module_id: str) -> None:
        pending = self._respond_pending
        self._respond_pending = []
        source = self._respond_sources.get(module_id)
        for message in pending:
            payload = message.get("respond") or {}
            reply = {"kind": "respond_result", "ok": False, "error": "respond failed"}
            try:
                result = await self._trusted_respond(module_id, source, payload)
                reply = {"kind": "respond_result", "ok": True, "result": result}
            except Exception as exc:
                reply = {"kind": "respond_result", "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
            process = self._workers.get(module_id)
            if process is not None and process.poll() is None and process.stdin is not None:
                process.stdin.write((json.dumps(reply) + "\n").encode("utf-8"))
                process.stdin.flush()

    async def _trusted_respond(self, module_id: str, source: Any, payload: dict[str, Any]) -> Any:
        import secrets as _secrets

        from .firewall import trusted_scope

        content = payload.get("content")
        kwargs = dict(payload.get("kwargs") or {})
        if source is None:
            raise PermissionError("no message source is bound to this call")
        app = getattr(self.runtime, "app", None)
        if app is None or getattr(app, "mt", None) is None:
            raise PermissionError("userbot transport is not ready")
        if kwargs.get("buttons"):
            raise PermissionError("sandbox respond does not support inline buttons yet")
        rich = bool(kwargs.pop("rich", False))
        output = kwargs.pop("output", "auto")
        media = kwargs.pop("media", None)
        text = kwargs.pop("text", None)
        if text is None and isinstance(content, str):
            text = content
        kwargs.pop("parse_mode", None)
        kwargs.pop("split_limit", None)
        kwargs.pop("file_limit", None)
        kwargs.pop("filename", None)
        kwargs.pop("preserve_html", None)
        chat_id = getattr(source, "chat_id", None)
        message_id = getattr(source, "id", None)
        is_out = bool(getattr(source, "is_me", False) or getattr(source, "out", False))
        if output == "auto":
            output = "edit" if is_out else "reply"
        topic_id = None
        for name in ("topic_id", "message_thread_id", "top_msg_id"):
            value = getattr(source, name, None)
            if isinstance(value, int) and value > 0:
                topic_id = value
                break

        async def send_plain(value: str, *, mode: str) -> Any:
            if mode == "edit" and message_id is not None:
                with trusted_scope():
                    return await app.mt_req("messages.editMessage", id=int(message_id), message=value)
            data: dict[str, Any] = {"peer": chat_id, "message": value, "random_id": _secrets.randbits(63)}
            if topic_id is not None:
                data["reply_to"] = {"_": "inputReplyToMessage", "reply_to_msg_id": int(message_id), "top_msg_id": topic_id} if message_id is not None else None
                if data["reply_to"] is None:
                    data.pop("reply_to")
            elif message_id is not None and mode == "reply":
                data["reply_to"] = {"_": "inputReplyToMessage", "reply_to_msg_id": int(message_id)}
            with trusted_scope():
                return await app.mt_req("messages.sendMessage", **data)

        async def send_rich_html(html: str, *, mode: str) -> Any:
            rich_message = {"_": "inputRichMessageHTML", "html": html}
            if mode == "edit" and message_id is not None:
                with trusted_scope():
                    return await app.mt_req("messages.editMessage", id=int(message_id), message="", rich_message=rich_message)
            data = {"peer": chat_id, "message": "", "random_id": _secrets.randbits(63), "rich_message": rich_message}
            if message_id is not None:
                reply_to = {"_": "inputReplyToMessage", "reply_to_msg_id": int(message_id)}
                if topic_id is not None:
                    reply_to["top_msg_id"] = topic_id
                data["reply_to"] = reply_to
            with trusted_scope():
                return await app.mt_req("messages.sendMessage", **data)

        if media is not None:
            raise PermissionError("sandbox respond media must go through files capability")
        if rich:
            if not isinstance(text, str):
                raise PermissionError("rich respond requires string content")
            try:
                return await send_rich_html(text, mode=output)
            except Exception as exc:
                marker = str(exc).lower()
                if "length" in marker or "too long" in marker or "MESSAGE_TOO_LONG" in str(exc):
                    parts = []
                    limit = 3800
                    rest = text
                    while len(rest) > limit:
                        cut = rest.rfind("\n", 0, limit + 1)
                        if cut < limit // 2:
                            cut = limit
                        parts.append(rest[:cut])
                        rest = rest[cut:].lstrip("\n")
                    parts.append(rest)
                    last = None
                    for index, part in enumerate(parts):
                        mode = "edit" if index == 0 and output == "edit" else "reply"
                        last = await send_rich_html(part, mode=mode)
                    return last
                raise
        if isinstance(text, str):
            limit = 4096
            if len(text) > limit:
                return await self.runtime.responses.smart_split(source, text)
            return await send_plain(text, mode=output)
        raise PermissionError("sandbox respond requires text content")

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
