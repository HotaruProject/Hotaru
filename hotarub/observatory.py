from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class Observatory:
    def __init__(self, path: str | Path = "observatory/runtime/events.jsonl", *, max_value: int = 2048) -> None:
        if max_value < 1:
            raise ValueError("max_value must be positive")
        self.path = Path(path)
        self.max_value = max_value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.path.touch(exist_ok=True)
        self.path.chmod(0o600)

    def emit(self, component: str, event: str, **fields: Any) -> None:
        payload = {
            "ts": time.time(),
            "component": component,
            "event": event,
            **{key: self._redact(key, value) for key, value in fields.items()},
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
        os.chmod(self.path, 0o600)

    def _redact(self, key: str, value: Any) -> Any:
        lowered = key.casefold()
        if any(secret in lowered for secret in ("token", "password", "api_hash", "auth_key", "vault")):
            return "[REDACTED]"
        if isinstance(value, str):
            return value[: self.max_value]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(k): self._redact(str(k), v) for k, v in list(value.items())[:64]}
        if isinstance(value, (list, tuple)):
            return [self._redact(key, item) for item in value[:64]]
        return str(value)[: self.max_value]
