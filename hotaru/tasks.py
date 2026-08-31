from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable
from typing import Any


class TaskLimitError(RuntimeError):
    pass


class TaskSupervisor:
    def __init__(self, *, max_total: int = 128, max_per_module: int = 16) -> None:
        if max_total < 1 or max_per_module < 1:
            raise ValueError("task limits must be positive")
        self.max_total = max_total
        self.max_per_module = max_per_module
        self._tasks: dict[str, set[asyncio.Task[Any]]] = defaultdict(set)

    def spawn(self, module_id: str, work: Awaitable[Any], *, name: str | None = None) -> asyncio.Task[Any]:
        tasks = self._tasks[module_id]
        if sum(len(items) for items in self._tasks.values()) >= self.max_total:
            work.close()
            raise TaskLimitError("task supervisor is full")
        if len(tasks) >= self.max_per_module:
            work.close()
            raise TaskLimitError("module task limit reached")
        task = asyncio.create_task(work, name=name or f"hotaru:{module_id}")
        tasks.add(task)
        task.add_done_callback(lambda finished: self._forget(module_id, finished))
        return task

    async def cancel_module(self, module_id: str) -> None:
        tasks = tuple(self._tasks.get(module_id, ()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.pop(module_id, None)

    async def close(self) -> None:
        for module_id in tuple(self._tasks):
            await self.cancel_module(module_id)

    def active(self, module_id: str | None = None) -> int:
        if module_id is not None:
            return len(self._tasks.get(module_id, ()))
        return sum(len(tasks) for tasks in self._tasks.values())

    def _forget(self, module_id: str, task: asyncio.Task[Any]) -> None:
        tasks = self._tasks.get(module_id)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            self._tasks.pop(module_id, None)
