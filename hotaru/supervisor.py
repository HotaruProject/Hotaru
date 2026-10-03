from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from goygram.errors import ConnectionClosedError

from .observatory import Observatory


TRANSPORT_ERRORS = (ConnectionError, ConnectionClosedError, TimeoutError, asyncio.TimeoutError)


class ConnectionRecovery:
    """Gate new RPCs while GoyGram's existing reader reconnects; never reconnect here."""

    def __init__(self, mt: Any, observatory: Observatory | None, name: str, *, ready: asyncio.Event | None = None) -> None:
        self.mt = mt
        self.observatory = observatory
        self.name = name
        self.ready = ready
        self.timeout = 45.0
        self.probe_timeout = 5.0
        self.interval = 0.25
        self._rpc: Callable[..., Awaitable[Any]] = mt._rpc_call
        self._recovery: asyncio.Task[None] | None = None
        self._retry_at = 0.0
        self._failed_writer: Any = None
        self._closed = False
        mt._rpc_call = self.call

    def _available(self) -> bool:
        reader = self.mt.reader_task
        writer = self.mt.wr
        return bool(reader is not None and not reader.done() and writer is not None
                    and not writer.is_closing() and self.mt.auth_ready.is_set())

    def _emit(self, event: str, level: str = "info", **fields: Any) -> None:
        if self.observatory is not None:
            self.observatory.emit("connection", event, level, connection=self.name, **fields)

    def recover(self) -> asyncio.Task[None]:
        task = self._recovery
        fresh_writer = self._available() and self.mt.wr is not self._failed_writer
        if task is None or (task.done() and (time.monotonic() >= self._retry_at or fresh_writer)):
            if self.ready is not None:
                self.ready.clear()
            task = self._recovery = asyncio.create_task(self._recover(), name=f"hotaru:recover-{self.name}")
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return task

    async def _recover(self) -> None:
        self._emit("recovering")
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                if self._closed or self.mt.stop_ev.is_set():
                    raise ConnectionClosedError("connection stopped")
                reader = self.mt.reader_task
                if reader is None or reader.done():
                    raise ConnectionClosedError("native reader stopped")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("native connection recovery timed out")
                if self._available():
                    try:
                        await asyncio.wait_for(self._rpc("users.getUsers", id=[{"_": "inputUserSelf"}]),
                                               min(self.probe_timeout, remaining))
                        break
                    except TRANSPORT_ERRORS:
                        pass
                await asyncio.sleep(min(self.interval, max(0.0, deadline - time.monotonic())))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed_writer = self.mt.wr
            self._retry_at = time.monotonic() + 30.0
            self._emit("recovery_failed", "error", error=type(exc).__name__, detail=str(exc)[:240])
            raise
        self._retry_at = 0.0
        if self.ready is not None:
            self.ready.set()
        self._emit("recovered")

    async def wait_ready(self) -> None:
        if self._closed or self.mt.stop_ev.is_set():
            raise ConnectionClosedError("connection stopped")
        task = self._recovery
        if task is not None and (not task.done() or self._retry_at):
            await asyncio.shield(self.recover())
        elif not self._available():
            await asyncio.shield(self.recover())

    async def call(self, act: str, **kwargs: Any) -> Any:
        if act == "ping":
            return await self._rpc(act, **kwargs)
        await self.wait_ready()
        safe_read = act in {"users.getUsers", "updates.getState"}
        try:
            request = self._rpc(act, **kwargs)
            return await asyncio.wait_for(request, 15.0) if safe_read else await request
        except TRANSPORT_ERRORS:
            task = self.recover()
            if not safe_read:
                raise
            await asyncio.shield(task)
            return await asyncio.wait_for(self._rpc(act, **kwargs), 15.0)

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(30.0)
            try:
                await self.call("users.getUsers", id=[{"_": "inputUserSelf"}])
            except Exception as exc:
                self._emit("health_error", "error", error=type(exc).__name__, detail=str(exc)[:240])

    async def close(self) -> None:
        self._closed = True
        if self.ready is not None:
            self.ready.clear()
        task = self._recovery
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class Health(enum.Enum):
    READY = "ready"
    DEGRADED = "degraded"
    STOPPED = "stopped"


@dataclass
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
