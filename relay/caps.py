from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .denylist import is_blocked_host, payload_hits_blocked
from .firewall import trusted_scope

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
    "account.resetnotifysettings",
    "account.setaccountttl",
    "account.getpassword",
    "account.getpasswordsettings",
    "account.gettmppassword",
    "account.getauthorizations",
    "account.setglobalprivacysettings",
    "account.setprivacypremiumrequired",
    "account.updateusername",
    "account.updateprofile",
    "account.updatestatus",
    "account.updatenotifysettings",
    "account.setprivacy",
    "account.setcontactsignupnotification",
    "account.updateemojistatus",
    "account.saveautodownloadsettings",
    "account.reportpeer",
    "account.updatetheme",
    "account.installwallpaper",
    "account.savewallpaper",
    "payments.sendpaymentform",
    "payments.validaterequestedinfo",
    "payments.clearsavedinfo",
    "users.setsecurevalueerrors",
    "channels.deletechannel",
    "channels.deletehistory",
    "channels.editadmin",
    "channels.editbanned",
    "channels.editcreator",
    "channels.updateusername",
    "channels.toggleprehistoryhidden",
    "channels.edittitle",
    "channels.editphoto",
    "channels.joinchannel",
    "channels.leavechannel",
    "channels.togglesignatures",
    "messages.deletechat",
    "messages.deletechatuser",
    "messages.deletehistory",
    "messages.deletephonecall",
    "messages.deletescheduledmessages",
    "messages.editchatadmin",
    "messages.editchattitle",
    "messages.editchatphoto",
    "messages.editchatabout",
    "messages.editchatdefaultbannedrights",
    "messages.addchatuser",
    "messages.setbotshippingresults",
    "messages.setbotprecheckoutresults",
    "messages.accepturlauth",
    "messages.hidepeersettingsinmenu",
    "contacts.deletecontacts",
    "contacts.block",
    "contacts.deletebyphones",
    "contacts.resetsaved",
    "contacts.editclosefriends",
})


def normalize_method(name: str) -> str:
    if "." in name:
        return name
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
        "detail": "full userbot MTProto access (messages, media, dialogs, chats) with destructive and auth operations blocked",
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
}

KNOWN = frozenset(PROVIDERS)


def describe(capabilities: tuple[str, ...]) -> str:
    lines = []
    for cap in capabilities:
        meta = PROVIDERS.get(cap)
        if meta is None:
            lines.append(f"{cap}: unknown capability (denied)")
            continue
        lines.append(f"{cap}: {meta['title']} — {meta['detail']}")
    return "\n".join(lines)


class CapabilityHost:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def _envelope(self, module_id: str, side_effect: str) -> Any:
        from hotaru.capabilities import BehaviorEnvelope

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
        return capability in active.loaded.manifest.capabilities

    async def call(self, module_id: str, capability: str, payload: dict[str, Any]) -> Any:
        if capability not in KNOWN:
            raise PermissionError(f"unknown capability: {capability}")
        if not self._allowed(module_id, capability):
            raise PermissionError(f"capability not granted to module: {capability}")
        meta = PROVIDERS[capability]
        with trusted_scope():
            if capability == "mt":
                return await self._mt_call(module_id, payload, meta)
            if capability == "files":
                return self._file_op(module_id, payload, meta)
            if capability == "net":
                return self._net_fetch(module_id, payload, meta)
            if capability == "state":
                return self._state_op(module_id, payload, meta)
        raise PermissionError(f"capability not implemented: {capability}")

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
        if canonical.startswith(("auth.", "phone.")) or canonical in MT_BLOCKED:
            raise PermissionError(f"mt method is blocked by policy: {method}")
        if payload_hits_blocked(payload):
            raise PermissionError("mt payload targets a denied peer")
        if not lowered.startswith(("get", "search")) and lowered not in MT_READ_ONLY:
            self._audit_mt(module_id, lowered, payload)
        kwargs = payload.get("kwargs") or {}
        if not isinstance(kwargs, dict):
            raise PermissionError("mt kwargs must be a mapping")
        kwargs = {k: v for k, v in kwargs.items() if isinstance(k, str) and k not in ("api_id", "api_hash")}
        if lowered.startswith("messages.gethistory"):
            limit = kwargs.get("limit", 100)
            if isinstance(limit, (int, float)) and int(limit) > 500:
                kwargs = dict(kwargs, limit=500)
        result = await app.mt_req(method, **kwargs)
        body = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else result
        return body

    def _audit_mt(self, module_id: str, method: str, payload: dict[str, Any]) -> None:
        observatory = getattr(self.runtime, "observatory", None)
        if observatory is None:
            return
        observatory.emit("caps", "mt_call", module=module_id, method=method)

    def _file_op(self, module_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> Any:
        op = payload.get("op", "read")
        name = payload.get("name")
        if not isinstance(name, str) or not name or "/" in name or "\\" in name or ".." in name or name.startswith(".") or "\x00" in name or len(name) > 190:
            raise PermissionError("file name must be a simple relative name")
        root = self.runtime.config.state_path.parent / "workspaces" / module_id
        root.mkdir(parents=True, exist_ok=True)
        target = root / name
        if op == "read":
            if not target.is_file():
                return None
            return target.read_text(encoding="utf-8", errors="replace")
        if op == "write":
            content = payload.get("content")
            if not isinstance(content, str):
                raise PermissionError("file write requires string content")
            if len(content.encode()) > 1024 * 1024:
                raise PermissionError("file write exceeds 1MB")
            target.write_text(content, encoding="utf-8")
            return {"bytes": len(content.encode())}
        if op == "list":
            return sorted(p.name for p in root.glob("*") if p.is_file())
        raise PermissionError(f"unknown file op: {op}")

    def _check_net_target(self, url: str) -> urllib.parse.ParseResult:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise PermissionError("net url scheme is not allowed")
        hostname = parsed.hostname
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
                if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast or addr.is_unspecified:
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
        timeout = payload.get("timeout", 10)
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
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    host._check_net_target(newurl)
                    return super().redirect_request(req, fp, code, msg, headers, newurl)

            opener = urllib.request.build_opener(_CheckedRedirect())
            with opener.open(request, timeout=min(float(timeout), 15.0)) as response:
                raw = response.read(1024 * 512)
                return {"status": response.status, "body": raw.decode("utf-8", errors="replace")}
        except PermissionError:
            raise
        except Exception as exc:
            raise PermissionError(f"net fetch failed: {type(exc).__name__}")

    def _state_op(self, module_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> Any:
        state = self.runtime.state
        if state is None:
            raise PermissionError("state store is not ready")
        op = payload.get("op", "get")
        key = payload.get("key")
        if not isinstance(key, str) or not key or "\x00" in key or len(key) > 1024:
            raise PermissionError("state op requires a valid key")
        namespace = state.namespace(module_id)
        if op == "get":
            return namespace.get(key)
        if op == "set":
            value = payload.get("value")
            encoded = json.dumps(value, ensure_ascii=False, default=str)
            if len(encoded) > 256 * 1024:
                raise PermissionError("state value exceeds 256KB")
            namespace.set(key, value)
            return {"ok": True}
        if op == "delete":
            return {"deleted": namespace.delete(key)}
        if op == "keys":
            return list(namespace.keys())
        raise PermissionError(f"unknown state op: {op}")
