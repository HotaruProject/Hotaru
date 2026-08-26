from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .modules import HmodLoader, LoadedModule


class ActivationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ActiveModule:
    loaded: LoadedModule
    context: Any


Starter = Callable[[LoadedModule], Any]


class ModuleManager:
    def __init__(self, loader: HmodLoader | None = None, *, timeout: float = 5.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.loader = loader or HmodLoader()
        self.timeout = timeout
        self._active: dict[str, ActiveModule] = {}

    async def activate(
        self,
        path: str | Path,
        starter: Starter,
        *,
        health: Callable[[Any], Any] | None = None,
    ) -> ActiveModule:
        loaded = self.loader.load(path)
        module_id = loaded.manifest.module_id
        if module_id in self._active:
            raise ActivationError(f"module is already active: {module_id}")
        try:
            context = starter(loaded)
            if inspect.isawaitable(context):
                context = await asyncio.wait_for(context, timeout=self.timeout)
            if health is not None:
                result = health(context)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=self.timeout)
                if result is False:
                    raise RuntimeError("health check returned false")
        except Exception as exc:
            await self._cleanup(context if "context" in locals() else None)
            raise ActivationError(f"module activation failed: {module_id}") from exc
        active = ActiveModule(loaded, context)
        self._active[module_id] = active
        return active

    async def _cleanup(self, context: Any) -> None:
        if context is None:
            return
        for name in ("stop", "close"):
            method = getattr(context, name, None)
            if method is None:
                continue
            result = method()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=self.timeout)
            return

    async def deactivate(self, module_id: str, stopper: Callable[[ActiveModule], Any] | None = None) -> bool:
        active = self._active.get(module_id)
        if active is None:
            return False
        if stopper is not None:
            result = stopper(active)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=self.timeout)
        del self._active[module_id]
        return True

    def get(self, module_id: str) -> ActiveModule | None:
        return self._active.get(module_id)

    def items(self) -> tuple[ActiveModule, ...]:
        return tuple(self._active.values())
