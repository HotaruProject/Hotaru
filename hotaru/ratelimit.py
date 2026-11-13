from __future__ import annotations

import asyncio
import collections
import contextvars
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

_ALERT = contextvars.ContextVar("hotaru_apifw_alert", default=False)


class ApiRateLimiter:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._lock = asyncio.Lock()
        self._history: collections.deque[tuple[str, float]] = collections.deque()
        self.suspend_until: float = 0.0
        self._pause_until: float = 0.0
        self.total_blocked: int = 0
        self._load()

    def _state(self) -> Any:
        state = getattr(self.runtime, "state", None)
        return state.namespace("shield") if state is not None else None

    def _load(self) -> None:
        ns = self._state()
        if ns is None:
            self.enabled = True
            self.threshold = 100
            self.time_sample = 15.0
            self.local_floodwait = 30.0
            return
        self.enabled = ns.get("enabled", True)
        self.threshold = int(ns.get("threshold", 100))
        self.time_sample = float(ns.get("time_sample", 15.0))
        self.local_floodwait = float(ns.get("local_floodwait", 30.0))

    def _save(self) -> None:
        ns = self._state()
        if ns is None:
            return
        ns.set("enabled", self.enabled)
        ns.set("threshold", self.threshold)
        ns.set("time_sample", self.time_sample)
        ns.set("local_floodwait", self.local_floodwait)

    def suspend(self, seconds: int) -> float:
        self.suspend_until = time.monotonic() + seconds if seconds > 0 else 0.0
        return self.suspend_until

    def toggle(self) -> bool:
        self.enabled = not self.enabled
        self._save()
        return self.enabled

    def stats(self) -> dict[str, Any]:
        now = time.monotonic()
        recent = [act for act, stamp in self._history if now - stamp < self.time_sample]
        counts = collections.Counter(recent)
        return {
            "enabled": self.enabled,
            "threshold": self.threshold,
            "time_sample": self.time_sample,
            "local_floodwait": self.local_floodwait,
            "suspended": now < self.suspend_until,
            "suspend_remaining": max(0, round(self.suspend_until - now)) if now < self.suspend_until else 0,
            "recent_count": len(recent),
            "top_methods": counts.most_common(5),
            "total_cooldowns": self.total_blocked,
        }

    def _paused(self) -> bool:
        now = time.monotonic()
        return self.enabled and now >= self.suspend_until and now < self._pause_until

    async def check(self, method: str) -> None:
        if not self.enabled or _ALERT.get():
            return
        triggered = 0
        async with self._lock:
            now = time.monotonic()
            if now < self.suspend_until:
                return
            cutoff = now - self.time_sample
            while self._history and self._history[0][1] < cutoff:
                self._history.popleft()
            self._history.append((method, now))
            count = len(self._history)
            if now >= self._pause_until:
                if count <= self.threshold:
                    return
                self._pause_until = now + self.local_floodwait
                self.total_blocked += 1
                triggered = count
        if triggered:
            self._announce(triggered)
        while self._paused():
            await asyncio.sleep(min(0.5, max(0.0, self._pause_until - time.monotonic())))

    def _announce(self, count: int) -> None:
        log.warning(
            "apifw: threshold exceeded (%d requests in %ss, limit %d), pausing for %ss",
            count,
            self.time_sample,
            self.threshold,
            self.local_floodwait,
        )
        if self.runtime.observatory is not None:
            self.runtime.observatory.emit(
                "security",
                "apifw_triggered",
                "warning",
                count=count,
                threshold=self.threshold,
                duration=self.local_floodwait,
            )
        asyncio.create_task(self._notify_alert(count))

    async def _notify_alert(self, count: int) -> None:
        token = _ALERT.set(True)
        try:
            flood = getattr(getattr(self.runtime, "responses", None), "flood", None)
            if flood is None:
                return
            try:
                text = self.runtime.t(
                    "kernel.shield.alert",
                    count=count,
                    sample=int(self.time_sample),
                    threshold=self.threshold,
                    cooldown=int(self.local_floodwait),
                )
            except Exception:
                try:
                    text = self.runtime.t(
                        "kernel.apifw.alert",
                        count=count,
                        sample=int(self.time_sample),
                        threshold=self.threshold,
                        cooldown=int(self.local_floodwait),
                    )
                except Exception:
                    text = (
                        f"<b>API Flood Protection triggered!</b>\n\n"
                        f"Rate reached {count} calls in {int(self.time_sample)}s (threshold: {self.threshold}).\n"
                        f"Paused API requests for {int(self.local_floodwait)}s to protect your account."
                    )
            config = getattr(self.runtime, "config", None)
            target = getattr(config, "owner_id", None) or getattr(getattr(self.runtime, "kernel", None), "owner_id", None) or getattr(config, "log_chat", None)
            if isinstance(target, int):
                await flood.send(target, text, src="bot")
        finally:
            _ALERT.reset(token)
