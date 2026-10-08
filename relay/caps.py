from __future__ import annotations

import json
import http.client
import ipaddress
import socket
import asyncio
import os
import secrets
import shutil
import stat
from pathlib import Path
import subprocess
import urllib.parse
import urllib.request
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, cast

from .denylist import is_blocked_host, payload_hits_blocked
from .firewall import trusted_scope
from hotaru.plainfmt import rich_to_plain
from hotaru.capabilities import BehaviorEnvelope
from .emoji import to_entities
from .rpc import rpcname

if TYPE_CHECKING:
    from .jobs import Job


MT_READ_ONLY = frozenset({
    "get",
    "gethistory",
    "getentity",
    "getdialogs",
    "getpeerdialogs",
    "getmessages",
    "getdifference",
    "getstate",
    "search",
    "getforumtopics",
    "getuserphotos",
    "getfulluser",
    "getfullchannel",
    "getfullchat",
})

MT_BLOCKED = frozenset({
    "account.deleteaccount",
    "account.resetauthorization",
    "account.resetauthorizations",
    "account.updatepasswordsettings",
    "account.setpasswordemail",
    "account.confirmpasswordemail",
    "account.resendpasswordemail",
    "account.changephone",
    "account.invalidatesignincodes",
    "account.registerdevice",
    "account.unregisterdevice",
    "account.updatedevicelocked",
    "account.setaccountttl",
    "account.getpassword",
    "account.getpasswordsettings",
    "account.gettmppassword",
    "account.getauthorizations",
    "account.setglobalprivacysettings",
    "account.setprivacypremiumrequired",
    "account.setprivacy",
    "account.reportpeer",
    "payments.sendpaymentform",
    "payments.validaterequestedinfo",
    "payments.clearsavedinfo",
    "users.setsecurevalueerrors",
})


def normalize_method(name: str) -> str:
    name = name.strip()
    if name.startswith("mt_"):
        name = name[3:]
    if "." in name:
        namespace, method = name.split(".", 1)
        return namespace + "." + method.replace("_", "")
    parts = name.split("_")
    if len(parts) < 2:
        return name
    namespace = parts[0]
    first = parts[1]
    tail = "".join(part[:1].upper() + part[1:] for part in parts[2:])
    return f"{namespace}.{first}{tail}"


PROVIDERS: dict[str, dict[str, Any]] = {
    "mt": {
        "title": "Telegram API",
        "detail": "full userbot MTProto access (messages, media, dialogs, chats, channels, contacts) with account security and payment operations blocked",
        "side_effect": "write",
    },
    "files": {
        "title": "Files",
        "detail": "read and write files inside the module workspace",
        "side_effect": "write",
    },
    "net": {
        "title": "Network",
        "detail": "outbound HTTP(S) requests to the internet; private/internal targets and denied hosts are blocked",
        "side_effect": "write",
    },
    "state": {
        "title": "State",
        "detail": "persistent key-value state in the module namespace",
        "side_effect": "write",
    },
    "inline": {
        "title": "Inline bots",
        "detail": "query and interact with inline bots",
        "side_effect": "write",
    },
    "modules": {
        "title": "Module management",
        "detail": "load, unload, reload, list, and inspect installed modules; hashes and manifest info",
        "side_effect": "write",
    },
    "assets": {
        "title": "Assets",
        "detail": "access to hidden assets storage",
        "side_effect": "write",
    },
    "shell": {
        "title": "Shell",
        "detail": "full shell access with the Hotaru process OS permissions, including host files, credentials and network; NOT confined to the module workspace. Command timeout and returned output limits still apply",
        "side_effect": "write",
    },
    "logs": {
        "title": "Logs",
        "detail": "write structured entries to the observatory log; always granted",
        "side_effect": "write",
    },
    "fetch": {
        "title": "Fetch",
        "detail": "read-only access to messages, entities and reply resolution; always granted",
        "side_effect": "read",
    },
}

KNOWN = frozenset(PROVIDERS)


def describe(capabilities: tuple[str, ...], t: Callable[..., str] | None = None) -> str:
    lines: list[str] = []
    for cap in capabilities:
        meta = PROVIDERS.get(cap)
        if meta is None:
            text = t("runtime.unknown_capability") if t is not None else "unknown capability (denied)"
            lines.append(f"{cap}: {text}")
            continue
        title = t("kernel.capabilities." + cap, meta['title']) if t is not None else meta['title']
        detail = t("kernel.capability_details." + cap, meta['detail']) if t is not None else meta['detail']
        lines.append(f"{cap}: {title} — {detail}")
    return "\n".join(lines)


def _result_body(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    payload = cast('dict[str, Any]', value)
    result = payload.get("result")
    return cast('dict[str, Any]', result) if isinstance(result, dict) else payload


def _public_connection(address: tuple[str, int], timeout: Any = None, source_address: Any = None) -> socket.socket:
    hostname, port = address
    if is_blocked_host(hostname):
        raise PermissionError("net url targets a denied host")
    addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    for _, _, _, _, target in addresses:
        addr = ipaddress.ip_address(target[0])
        if not addr.is_global or addr.is_multicast:
            raise PermissionError("net url resolves to a non-public address")
    error: OSError = OSError("net url could not be resolved")
    for family, kind, proto, _, target in addresses:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(timeout)
            if source_address is not None:
                sock.bind(source_address)
            sock.connect(target)
            return sock
        except OSError as exc:
            error = exc
            sock.close()
    raise error


class _PublicHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        cast(Any, self)._create_connection = _public_connection
        super().connect()


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        cast(Any, self)._create_connection = _public_connection
        super().connect()


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: Any) -> Any:
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: Any) -> Any:
        return self.do_open(_PublicHTTPSConnection, req)


def _workspace_open(root: Path, name: str, flags: int) -> int:
    root = root.absolute()
    target = Path(name)
    if target.is_absolute():
        try:
            target = target.relative_to(root)
        except ValueError as exc:
            raise PermissionError("file path escapes the module workspace") from exc
    if ".." in root.parts or ".." in target.parts:
        raise PermissionError("file path escapes the module workspace")
    directory = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*root.parts[1:], *target.parts[:-1]):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                if not flags & os.O_CREAT:
                    raise
                try:
                    os.mkdir(part, 0o755, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(target.name or ".", (flags & ~os.O_TRUNC) | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if flags & os.O_DIRECTORY:
                if not stat.S_ISDIR(info.st_mode):
                    raise PermissionError("workspace directory required")
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PermissionError("workspace file must be regular and unlinked elsewhere")
            if flags & os.O_TRUNC:
                os.ftruncate(fd, 0)
        except BaseException:
            os.close(fd)
            raise
        return fd
    finally:
        os.close(directory)


class CapabilityHost:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.jobs: dict[tuple[str, str, str], Job] = {}

    def _envelope(self, module_id: str, side_effect: str) -> Any:
        return BehaviorEnvelope(
            actor=None,
            module_id=module_id,
            target=None,
            fanout=1,
            reversible=False,
            persistence=False,
            network="internet",
            side_effect=side_effect,
        )

    def _allowed(self, module_id: str, capability: str) -> bool:
        modules = self.runtime.modules
        if modules is None:
            return False
        active = modules.get(module_id)
        if active is None:
            return False
        manifest = active.loaded.manifest
        return capability in manifest.capabilities and (
            self.runtime._is_kernel_module(module_id)
            or self.runtime._caps_consented(module_id, manifest)
        )

    async def call(self, module_id: str, capability: str, payload: dict[str, Any]) -> Any:
        if capability not in KNOWN:
            raise PermissionError(f"unknown capability: {capability}")
        meta = PROVIDERS[capability]
        if capability == "logs":
            return self._logs_op(module_id, payload)
        elif capability == "fetch":
            return await self._fetch_op(module_id, payload)
        elif not self._allowed(module_id, capability):
            if capability in {"net", "shell"}:
                await self.close_jobs(module_id, capability)
            raise PermissionError(f"capability not granted to module: {capability}")
        with trusted_scope():
            if capability in {"net", "shell"} and payload.get("op") in {"start", "poll", "cancel", "forget"}:
                return await self._job_op(module_id, capability, payload)
            if capability == "mt":
                return await self._mt_call(module_id, payload, meta)
            if capability == "files":
                return self._file_op(module_id, payload, meta)
            if capability == "net":
                return await asyncio.get_running_loop().run_in_executor(None, self._net_fetch, module_id, payload, meta)
            if capability == "state":
                return self._state_op(module_id, payload, meta)
            if capability == "inline":
                return await self._inline_op(module_id, payload, meta)
            if capability == "modules":
                return await self._modules_op(module_id, payload, meta)
            if capability == "assets":
                return await self._assets_op(module_id, payload, meta)
            if capability == "shell":
                return await self._shell_op(module_id, payload, meta)
        raise PermissionError(f"capability not implemented: {capability}")

    async def close_jobs(self, module_id: str, kind: str | None = None) -> None:
        jobs = [self.jobs.pop(key) for key in list(self.jobs) if key[0] == module_id and (kind is None or key[1] == kind)]
        for job in jobs:
            job.task.cancel()
        if jobs:
            await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)

    async def _job_op(self, module_id: str, kind: str, payload: dict[str, Any]) -> Any:
        from .jobs import Job
        op = payload["op"]
        if op == "start":
            if sum(key[0] == module_id for key in self.jobs) >= 16:
                raise PermissionError("active request limit")
            if kind == "net":
                self._check_net_target(str(payload.get("url", "")))
            elif not isinstance(payload.get("command"), str) or not payload["command"].strip() or len(payload["command"]) > 16000:
                raise PermissionError("shell requires a command up to 16000 characters")
            key = secrets.token_hex(16)
            workspace = self.runtime.config.state_path.parent / "workspaces" / module_id
            self.jobs[module_id, kind, key] = Job(kind, dict(payload), self._check_net_target, workspace)
            return {"id": key}
        key = (module_id, kind, str(payload.get("id", "")))
        job = self.jobs.get(key)
        if job is None:
            raise PermissionError("request unavailable")
        if op in {"cancel", "forget"}:
            self.jobs.pop(key)
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            return {"ok": True}
        events: list[dict[str, str]] = []
        while not job.events.empty():
            events.append(job.events.get_nowait())
        result = {"done": job.done, "result": job.result, "error": job.error, "events": events}
        if kind == "shell":
            result.update({name: (job.result or {}).get(name, "") for name in ("stdout", "stderr")})
        return result

    async def _inline_op(self, module_id: str, payload: dict[str, Any], meta: Any) -> Any:
        if payload_hits_blocked(payload):
            raise PermissionError("inline payload targets a denied peer")
        inline = getattr(self.runtime, "inline", None)
        if inline is None:
            raise PermissionError("inline is unavailable")
        op = str(payload.get("op") or "")
        extra: dict[str, Any] = dict(payload["kwargs"]) if isinstance(payload.get("kwargs"), dict) else {}
        if op == "query":
            return await inline.query(payload.get("text"), **extra)
        if op == "send":
            return await inline.send(payload.get("peer"), payload.get("text"), **extra)
        if op == "form":
            return await inline.form(payload.get("text"), payload.get("buttons"), **extra)
        raise PermissionError(f"inline op is invalid: {op}")

    async def _fetch_op(self, module_id: str, payload: dict[str, Any]) -> Any:
        if payload_hits_blocked(payload):
            raise PermissionError("fetch payload targets a denied peer")
        app = self.runtime.app
        if app is None or app.mt is None:
            raise PermissionError("userbot transport is not ready")
        op = payload.get("op")

        async def _history(peer_value: Any, offset_id: int, limit: int) -> list[dict[str, Any]]:
            peer = await app.mt.resolve_peer(peer_value)
            if payload_hits_blocked({"peer": peer}):
                raise PermissionError("resolved fetch target is a denied peer")
            result = await app.mt_messages_get_history(
                peer=peer,
                offset_id=offset_id,
                offset_date=0,
                add_offset=0,
                limit=min(max(limit, 1), 100),
                max_id=0,
                min_id=0,
                hash=0,
            )
            body = _result_body(result)
            messages = body.get("messages")
            return [cast('dict[str, Any]', message) for message in cast('list[Any]', messages) if isinstance(message, dict)] if isinstance(messages, list) else []

        if op == "message":
            peer_value = payload.get("peer")
            msg_id = payload.get("id")
            if not isinstance(msg_id, int) or peer_value is None:
                raise PermissionError("fetch message requires peer and id")
            for message in await _history(peer_value, msg_id + 1, 3):
                if message.get("id") == msg_id:
                    return message
            return None
        if op == "messages":
            return await _history(payload.get("peer"), int(payload.get("offset_id") or 0), int(payload.get("limit") or 20))
        if op == "reply":
            header = payload.get("reply_to")
            header_dict = cast('dict[str, Any]', header) if isinstance(header, dict) else None
            reply_id = header_dict.get("reply_to_msg_id") or header_dict.get("reply_to_id") if header_dict is not None else None
            if not isinstance(reply_id, int):
                return None
            for message in await _history(payload.get("peer"), reply_id + 1, 3):
                if message.get("id") == reply_id:
                    return message
            return None
        if op == "entity":
            value = payload.get("value")
            if payload_hits_blocked({"peer": value}):
                raise PermissionError("fetch entity targets a denied peer")
            if value is None:
                raise PermissionError("fetch entity requires a value")
            if isinstance(value, str) and value.lstrip("-").isdigit():
                value = int(value)
            if isinstance(value, str):
                username = value.lstrip("@").casefold()
                entity = app.mt.entity_usernames.get(username)
                if entity is None:
                    result = await app.mt_contacts_resolve_username( username=username)
                    body = _result_body(result)
                    app.mt._ingest_entities(body)
                    entity = app.mt.entity_usernames.get(username)
                return entity
            if isinstance(value, int):
                if value > 0:
                    entity = app.mt.entities.get(("user", value))
                    if entity is None:
                        return None
                    return entity
                raw = -value
                if raw > 1000000000000:
                    raw -= 1000000000000
                return app.mt.entities.get(("chat", raw))
            raise PermissionError("fetch entity requires a username or an id")
        raise PermissionError(f"unknown fetch op: {op}")

    def _logs_op(self, module_id: str, payload: dict[str, Any]) -> Any:
        observatory = getattr(self.runtime, "observatory", None)
        if observatory is None:
            return {"ok": False}
        op = payload.get("op") if isinstance(payload.get("op"), str) else "write"
        if op == "write":
            text = payload.get("msg")
            if not isinstance(text, str) or not text.strip():
                return {"ok": False}
            level = str(payload.get("level") or "info")
            fields: dict[str, Any] = {"msg": text[:2048]}
            if isinstance(payload.get("name"), str):
                fields["name"] = payload["name"][:120]
            if isinstance(payload.get("detail"), str):
                fields["detail"] = payload["detail"][:2048]
            observatory.emit("module", "log", level=level, module=module_id, **fields)
            return {"ok": True}
        if op == "read":
            entries = observatory.tail(lines=40, module=module_id, level=str(payload.get("level") or "info"))
            return {"ok": True, "entries": entries}
        return {"ok": False}

    async def _shell_op(self, module_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> Any:
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip() or len(command) > 16000:
            raise PermissionError("shell requires a non-empty command up to 16000 characters")
        timeout = payload.get("timeout")
        try:
            timeout = float(timeout) if timeout is not None else None
        except (TypeError, ValueError):
            timeout = None
        if timeout is not None and timeout <= 0:
            timeout = None
        workspace = self.runtime.config.state_path.parent / "workspaces" / module_id
        workspace.mkdir(parents=True, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(workspace), "LANG": "C.UTF-8"}
        if os.name == "nt":
            env.update({key: os.environ[key] for key in ("SystemRoot", "COMSPEC", "PATHEXT") if key in os.environ})
        try:
            loop = asyncio.get_running_loop()
            proc = await loop.run_in_executor(
                None,
                partial(
                    subprocess.run,
                command if os.name == "nt" else [shutil.which("sh") or "/bin/sh", "-lc", command],
                shell=os.name == "nt",
                cwd=str(workspace),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                ),
            )
        except subprocess.TimeoutExpired:
            raise PermissionError(f"shell command timed out after {timeout}s")
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return {"returncode": proc.returncode, "output": output, "cwd": str(workspace), "truncated": False}

    async def _mt_call(self, module_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> Any:
        app = self.runtime.app
        if app is None or app.mt is None:
            raise PermissionError("userbot transport is not ready")
        method = payload.get("method")
        if not isinstance(method, str) or not method.strip():
            raise PermissionError("mt payload requires a method")
        method = method.strip()
        lowered = method.lower()
        canonical = normalize_method(lowered).lower()
        if "." not in canonical:
            raise PermissionError("mt capability requires a namespaced RPC method")
        if canonical.startswith(("auth.", "phone.")) or canonical in MT_BLOCKED:
            raise PermissionError(f"mt method is blocked by policy: {method}")
        if payload_hits_blocked(payload):
            raise PermissionError("mt payload targets a denied peer")
        if not lowered.startswith(("get", "search")) and lowered not in MT_READ_ONLY:
            self._audit_mt(module_id, lowered, payload)
        raw_kwargs_value = payload.get("kwargs")
        if raw_kwargs_value is not None and not isinstance(raw_kwargs_value, dict):
            raise PermissionError("mt kwargs must be a mapping")
        raw_kwargs: dict[Any, Any] = cast('dict[Any, Any]', raw_kwargs_value) if isinstance(raw_kwargs_value, dict) else {}
        kwargs: dict[str, Any] = {k: v for k, v in raw_kwargs.items() if isinstance(k, str) and k not in ("api_id", "api_hash")}
        rich_message = kwargs.get("rich_message")
        if rich_message is not None:
            allowed = False
            try:
                allowed = await self.runtime.is_premium()
            except Exception:
                allowed = False
            if not allowed:
                html_text = cast('dict[str, Any]', rich_message).get("html") if isinstance(rich_message, dict) else None
                kwargs.pop("rich_message", None)
                if isinstance(html_text, str) and lowered.startswith(("messages.send", "messages.edit")):
                    plain_html = rich_to_plain(html_text)
                    plain, entities = to_entities(plain_html)
                    kwargs["message"] = (kwargs.get("message") or "") + plain
                    if entities:
                        kwargs["entities"] = entities
                else:
                    raise PermissionError("rich transport requires premium")

        if lowered.startswith("messages.gethistory"):
            limit = kwargs.get("limit", 100)
            if isinstance(limit, (int, float)) and int(limit) > 500:
                kwargs = dict(kwargs, limit=500)
        timeout = payload.get("timeout")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60):
            raise ValueError("MT timeout must be between 0 and 60 seconds")
        request = getattr(app, rpcname(method))(**kwargs)
        result = await request if timeout is None else await asyncio.wait_for(request, float(timeout))
        return _result_body(result)

    def _audit_mt(self, module_id: str, method: str, payload: dict[str, Any]) -> None:
        observatory = getattr(self.runtime, "observatory", None)
        if observatory is None:
            return
        observatory.emit("caps", "mt_call", module=module_id, method=method)

    def _file_op(self, module_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> Any:
        op = payload.get("op", "read")
        root = self.runtime.config.state_path.parent / "workspaces" / module_id
        root.mkdir(parents=True, exist_ok=True)
        if op == "list":
            fd = _workspace_open(root, "", os.O_RDONLY | os.O_DIRECTORY)
            try:
                with os.scandir(fd) as entries:
                    return sorted(entry.name for entry in entries if entry.is_file(follow_symlinks=False))
            finally:
                os.close(fd)
        name = payload.get("name")
        if not isinstance(name, str) or not name or "/" in name or "\\" in name or ".." in name or name.startswith(".") or "\x00" in name or len(name) > 190:
            raise PermissionError("file name must be a simple relative name")
        target = root / name
        if root.is_symlink() or target.resolve().parent != root.resolve():
            raise PermissionError("file path escapes the module workspace")
        if op == "read":
            try:
                fd = _workspace_open(root, name, os.O_RDONLY)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        if op == "write":
            content = payload.get("content")
            if not isinstance(content, str):
                raise PermissionError("file write requires string content")
            if len(content.encode()) > 1024 * 1024:
                raise PermissionError("file write exceeds 1MB")
            fd = _workspace_open(root, name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            return {"bytes": len(content.encode())}
        raise PermissionError(f"unknown file op: {op}")

    def _check_net_target(self, url: str) -> urllib.parse.ParseResult:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise PermissionError("net url scheme is not allowed")
        hostname = parsed.hostname
        if parsed.username is not None or parsed.password is not None:
            raise PermissionError("net url credentials are not allowed")
        if not hostname:
            raise PermissionError("net url must contain a hostname")
        hostname = urllib.parse.unquote(hostname)
        if is_blocked_host(hostname):
            raise PermissionError("net url targets a denied host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            import ipaddress
            import socket
            for info in socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP):
                addr = ipaddress.ip_address(info[4][0])
                if not addr.is_global or addr.is_multicast:
                    raise PermissionError("net url resolves to a non-public address")
        except PermissionError:
            raise
        except ValueError as exc:
            raise PermissionError("net url contains an invalid address") from exc
        except Exception as exc:
            raise PermissionError("net url could not be resolved") from exc
        return parsed

    def _net_fetch(self, module_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> Any:
        url = payload.get("url")
        if not isinstance(url, str) or not url:
            raise PermissionError("net capability requires a url")
        self._check_net_target(url)
        data = payload.get("data")
        timeout = payload.get("timeout", 30)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = 30.0
        if timeout <= 0:
            timeout = None
        limit = payload.get("max_bytes")
        limit = int(limit) if isinstance(limit, (int, float)) and int(limit) > 0 else 8 * 1024 * 1024
        try:
            if data is not None:
                body = json.dumps(data).encode()
                request = urllib.request.Request(
                    url,
                    data=body,
                    headers={"Content-Type": "application/json", "User-Agent": f"hotaru/{module_id}"},
                    method="POST",
                )
            else:
                request = urllib.request.Request(url, headers={"User-Agent": f"hotaru/{module_id}"}, method="GET")

            host = self

            class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> Any:
                    host._check_net_target(newurl)
                    return super().redirect_request(req, fp, code, msg, headers, newurl)

            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _PublicHTTPHandler(), _PublicHTTPSHandler(), _CheckedRedirect())
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(limit + 1)
                truncated = len(raw) > limit
                if truncated:
                    raw = raw[:limit]
                return {"status": response.status, "body": raw.decode("utf-8", errors="replace"), "truncated": truncated}
        except PermissionError:
            raise
        except Exception as exc:
            raise OSError(f"net fetch failed: {type(exc).__name__}") from exc

    def _state_op(self, module_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> Any:
        state = self.runtime.state
        if state is None:
            raise PermissionError("state store is not ready")
        op = payload.get("op", "get")
        if op == "keys":
            return list(state.namespace(module_id).keys())
        key = payload.get("key")
        if not isinstance(key, str) or not key or "\x00" in key or len(key) > 1024:
            raise PermissionError("state op requires a valid key")
        if op in {"set", "delete"} and key in {"sourcepath", "caps-consent", "moduleversion", "lasterror"}:
            raise PermissionError("module lifecycle state is protected")
        namespace = state.namespace(module_id)
        if op == "get":
            return namespace.get(key)
        if op == "set":
            value = payload.get("value")
            encoded = json.dumps(value, ensure_ascii=False, default=str)
            if len(encoded.encode("utf-8")) > 10 * 1024 * 1024:
                raise PermissionError("state value exceeds 10MiB")
            namespace.set(key, value)
            return {"ok": True}
        if op == "delete":
            return {"deleted": namespace.delete(key)}
        raise PermissionError(f"unknown state op: {op}")

    def _module_brief(self, module_id: str) -> dict[str, Any]:
        runtime = self.runtime
        modules = runtime.modules
        state = runtime.state
        active = modules.get(module_id) if modules is not None else None
        loaded = active.loaded if active is not None else None
        manifest = loaded.manifest if loaded is not None else None
        namespace = state.namespace(module_id) if state is not None else None
        return {
            "module_id": module_id,
            "active": active is not None,
            "version": manifest.version if manifest else (namespace.get("moduleversion") if namespace else None),
            "digest": loaded.digest if loaded is not None else None,
            "commands": list(manifest.commands) if manifest else [],
            "capabilities": list(manifest.capabilities) if manifest else [],
            "description": manifest.description if manifest else "",
            "path": str(loaded.path) if loaded is not None else (namespace.get("sourcepath") if namespace else None),
            "lasterror": namespace.get("lasterror") if namespace else None,
        }

    async def _modules_op(self, module_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> Any:
        runtime = self.runtime
        op = payload.get("op")
        if not isinstance(op, str):
            raise PermissionError("modules op is required")
        modules = runtime.modules
        if modules is None:
            raise PermissionError("module manager is not ready")
        if op == "list":
            if runtime.state is not None:
                module_ids: set[str] = set(runtime.state.module_ids())
            else:
                module_ids = set()
            module_ids |= {item.loaded.manifest.module_id for item in modules.items()}
            return [self._module_brief(module_id) for module_id in sorted(module_ids)]
        if op == "info":
            target = payload.get("module_id")
            if not isinstance(target, str) or not target:
                raise PermissionError("modules info requires a module_id")
            return self._module_brief(target.casefold())
        if op == "hashes":
            result: dict[str, str] = {}
            if runtime.state is not None:
                ids: set[str] = set(runtime.state.module_ids())
            else:
                ids = set()
            ids |= {item.loaded.manifest.module_id for item in modules.items()}
            for module_id in sorted(ids):
                brief = self._module_brief(module_id)
                if brief["digest"]:
                    result[module_id] = brief["digest"]
            return result
        if op == "load":
            return await self._modules_load(module_id, payload)
        if op == "unload":
            return await self._modules_unload(module_id, payload)
        if op == "reload":
            return await self._modules_reload(module_id, payload)
        if op == "reset":
            return await self._modules_reset(module_id, payload)
        raise PermissionError(f"unknown modules op: {op}")

    async def _modules_load(self, caller_id: str, payload: dict[str, Any]) -> Any:
        runtime = self.runtime
        url = payload.get("url")
        text = payload.get("text")
        source = payload.get("source")
        if runtime.stager is None or runtime.state is None:
            raise PermissionError("module stager is not ready")
        sources = [item for item in (source, text, url) if item is not None]
        if len(sources) != 1 or not isinstance(sources[0], str):
            raise PermissionError("modules load requires exactly one source")
        value = sources[0]
        if not (value.startswith("https://") or "\n" in value or value.lstrip().startswith("HOTARU")):
            raise PermissionError("modules load requires an https url or module source text")
        loaded, action = await runtime.load_module(value)
        module_id = loaded.manifest.module_id
        return {
            "module_id": module_id,
            "version": loaded.manifest.version,
            "digest": loaded.digest,
            "action": action,
            **({"source": loaded.source, "capabilities": list(loaded.manifest.capabilities)} if action == "confirm" else {}),
        }

    async def _modules_unload(self, caller_id: str, payload: dict[str, Any]) -> Any:
        runtime = self.runtime
        target = payload.get("module_id")
        if not isinstance(target, str) or not target:
            raise PermissionError("modules unload requires a module_id")
        purge = payload.get("purge", False)
        if not isinstance(purge, bool):
            raise PermissionError("purge must be a boolean")
        target = target.casefold()
        if runtime._is_kernel_module(target):
            raise PermissionError("kernel modules are protected")
        sandbox = runtime.sandbox
        waiters = tuple(sandbox._waiters.get(caller_id, {}).values()) if sandbox is not None and target == caller_id else ()
        if waiters and sandbox is not None:
            async def finish() -> None:
                await asyncio.wait(waiters, timeout=runtime.modules.timeout)
                result = await runtime.unload_module(target, purge=purge)
                if result is not None:
                    raise RuntimeError(result)
            sandbox._spawn_pump_task(finish())
            return {"module_id": target, "action": "unloading", "purged": False}
        result = await runtime.unload_module(target, purge=purge)
        if result is not None:
            raise PermissionError(result)
        return {"module_id": target.casefold(), "action": "unloaded", "purged": purge}

    async def _modules_reset(self, caller_id: str, payload: dict[str, Any]) -> Any:
        runtime = self.runtime
        target = payload.get("module_id")
        if not isinstance(target, str) or not target:
            raise PermissionError("modules reset requires a module_id")
        if runtime._is_kernel_module(target.casefold()):
            raise PermissionError("kernel module state is protected")
        result = await runtime.reset_module_database(target)
        if "not found" in result:
            raise PermissionError(result)
        return {"module_id": target.casefold(), "action": "reset", "detail": result}

    async def _modules_reload(self, caller_id: str, payload: dict[str, Any]) -> Any:
        runtime = self.runtime
        target = payload.get("module_id")
        if not isinstance(target, str) or not target:
            raise PermissionError("modules reload requires a module_id")
        module_id = target.casefold()
        if runtime._is_kernel_module(module_id):
            raise PermissionError("kernel modules are protected")
        if runtime.modules.get(module_id) is None:
            raise PermissionError(f"module not found: {module_id}")
        result = await runtime._command_rl(SimpleNamespace(args=(module_id, "force")))
        if result != runtime.t("runtime.reloaded", module_id=module_id):
            raise RuntimeError(str(result))
        return {"module_id": module_id, "action": "reloaded", "detail": result}

    async def _assets_op(self, module_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> Any:
        if payload_hits_blocked(payload):
            raise PermissionError("assets payload targets a denied peer")
        import os
        from .sandbox import SANDBOX_BASE_ROOT

        op = payload.get("op")
        sandbox_root = os.path.join(SANDBOX_BASE_ROOT, module_id)
        if os.path.realpath(sandbox_root) != sandbox_root:
            raise PermissionError("invalid module workspace")
        
        if op == "upload":
            file_path = str(payload.get("file", ""))
            filename = str(payload.get("filename") or "")
            path = os.path.realpath(os.path.join(sandbox_root, file_path))
            if os.path.commonpath((sandbox_root, path)) != sandbox_root or not os.path.isfile(path):
                raise PermissionError("invalid file path")
            app = self.runtime.app
            if app is None:
                raise PermissionError("userbot transport is not ready")
            from relay.files import put
            fd = _workspace_open(Path(sandbox_root), path, os.O_RDONLY)
            with os.fdopen(fd, "rb") as handle:
                return await put(app, handle, file_name=filename or Path(path).name)

        if op == "download":
            raw_message_value = payload.get("message")
            if raw_message_value is not None and not isinstance(raw_message_value, dict):
                raise PermissionError("invalid message object")
            msg_dict = cast('dict[str, Any]', raw_message_value) if isinstance(raw_message_value, dict) else {}
            destination = payload.get("destination")
            if destination:
                dest_path = os.path.realpath(os.path.join(sandbox_root, str(destination)))
                if os.path.commonpath((sandbox_root, dest_path)) != sandbox_root:
                    raise PermissionError("invalid destination path")
            else:
                dest_path = sandbox_root + "/"
            app = self.runtime.app
            if app is None:
                raise PermissionError("userbot transport is not ready")
            chat_id = msg_dict.get("chat_id")
            msg_id = msg_dict.get("id") or msg_dict.get("message_id")
            if chat_id is None or msg_id is None:
                raise PermissionError("invalid message object")
            from relay.files import take
            if dest_path.endswith(os.sep) or os.path.isdir(dest_path):
                dest_path = os.path.join(dest_path, secrets.token_hex(12) + ".bin")
            fd = _workspace_open(Path(sandbox_root), dest_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            with os.fdopen(fd, "w+b") as handle:
                await take(app, (chat_id, msg_id), handle)
            return os.path.relpath(dest_path, sandbox_root)
