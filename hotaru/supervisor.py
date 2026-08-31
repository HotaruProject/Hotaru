from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


class Health(enum.Enum):
    READY = "ready"
    DEGRADED = "degraded"
    STOPPED = "stopped"


@dataclass(slots=True)
class SupervisorState:
    health: Health = Health.STOPPED
    connected_at: float | None = None
    last_error: str | None = None
    reconnects: int = 0
    mt_ready: bool = False
    bot_ready: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class ConnectionSupervisor:
    def __init__(
        self,
        *,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        degraded_after: float = 30.0,
        on_change: Callable[[SupervisorState], Any] | None = None,
    ) -> None:
        if base_delay <= 0 or max_delay < base_delay:
            raise ValueError("invalid backoff delays")
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.degraded_after = degraded_after
        self.state = SupervisorState()
        self._on_change = on_change
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def mark_ready(self, *, mt: bool, bot: bool) -> None:
        self.state.health = Health.READY
        self.state.connected_at = time.monotonic()
        self.state.last_error = None
        self.state.mt_ready = mt
        self.state.bot_ready = bot
        self._notify()

    def mark_degraded(self, reason: str) -> None:
        if self.state.health is not Health.STOPPED:
            self.state.health = Health.DEGRADED
            self.state.last_error = reason
            self._notify()

    def mark_stopped(self) -> None:
        self.state.health = Health.STOPPED
        self._notify()

    def note_reconnect(self) -> None:
        self.state.reconnects += 1

    def _notify(self) -> None:
        if self._on_change is None:
            return
        result = self._on_change(self.state)
        if asyncio.iscoroutine(result):
            try:
                asyncio.get_running_loop().create_task(result)
            except RuntimeError:
                pass

    async def run_forever(self, connect: Callable[[], Awaitable[None]]) -> None:
        delay = self.base_delay
        while not self._stop.is_set():
            try:
                await connect()
                delay = self.base_delay
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.last_error = type(exc).__name__
                self.note_reconnect()
                if self.state.health is Health.READY:
                    self.mark_degraded(type(exc).__name__)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, self.max_delay)
        self.mark_stopped()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.mark_stopped()
