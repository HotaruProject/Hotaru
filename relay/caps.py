from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROVIDERS: dict[str, dict[str, Any]] = {
    "msg": {
        "title": "Messages",
        "detail": "send messages as the userbot",
        "side_effect": "write",
    },
    "files": {
        "title": "Files",
        "detail": "read and write files inside the module workspace",
        "side_effect": "write",
    },
    "net": {
        "title": "Network",
        "detail": "outbound HTTP(S) requests to the internet",
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
        if capability == "msg":
            return await self._send_message(module_id, payload, meta)
        if capability == "files":
            return self._file_op(module_id, payload, meta)
        if capability == "net":
            return self._net_fetch(module_id, payload, meta)
        if capability == "state":
            return self._state_op(module_id, payload, meta)
        raise PermissionError(f"capability not implemented: {capability}")

    async def _send_message(self, module_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> Any:
        app = self.runtime.app
        if app is None or app.mt is None:
            raise PermissionError("userbot transport is not ready")
        chat_id = payload.get("chat_id")
        text = payload.get("text")
        if not isinstance(chat_id, (int, str)) or not isinstance(text, str) or not text:
            raise PermissionError("msg payload requires chat_id and text")
        fanout_window = self.runtime.state.namespace(module_id).get("msg_fanout", [])
        import time

        now = time.time()
        fanout_window = [t for t in fanout_window if now - t < 60.0][:19] if isinstance(fanout_window, list) else []
        if len(fanout_window) >= 20:
            raise PermissionError("msg rate limit: 20 messages per minute per module")
        fanout_window.append(now)
        self.runtime.state.namespace(module_id).set("msg_fanout", fanout_window)
        peer = await app.mt.resolve_peer(chat_id)
        import secrets

        result = await app.mt_req(
            "messages.sendMessage",
            peer=peer,
            message=text[:4096],
            random_id=secrets.randbits(63),
        )
        body = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else result
        return {"sent": True}

    def _file_op(self, module_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> Any:
        op = payload.get("op", "read")
        name = payload.get("name")
        if not isinstance(name, str) or not name or "/" in name or ".." in name or name.startswith("."):
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

    def _net_fetch(self, module_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> Any:
        url = payload.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise PermissionError("net capability requires a plain https url")
        parsed = urllib.parse.urlparse(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise PermissionError("net url must be plain https without credentials")
        if (parsed.port or 443) != 443:
            raise PermissionError("net capability allows port 443 only")
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
            with urllib.request.urlopen(request, timeout=min(float(timeout), 15.0)) as response:
                raw = response.read(1024 * 512)
                return {"status": response.status, "body": raw.decode("utf-8", errors="replace")}
        except Exception as exc:
            raise PermissionError(f"net fetch failed: {type(exc).__name__}")

    def _state_op(self, module_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> Any:
        state = self.runtime.state
        if state is None:
            raise PermissionError("state store is not ready")
        op = payload.get("op", "get")
        key = payload.get("key")
        if not isinstance(key, str) or not key:
            raise PermissionError("state op requires a key")
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
