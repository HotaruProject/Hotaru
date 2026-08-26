from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


class CapabilityDenied(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class BehaviorEnvelope:
    actor: int | str | None
    module_id: str
    target: int | str | None
    fanout: int
    reversible: bool
    persistence: bool
    network: str
    side_effect: str


@dataclass(frozen=True, slots=True)
class CapabilityProvider:
    provider_id: str
    version: str
    handler: Callable[[dict[str, Any]], Any]
    trust_modes: frozenset[str]
    side_effect: str


class CapabilityBroker:
    def __init__(self, policy: Callable[[CapabilityProvider, BehaviorEnvelope], bool] | None = None) -> None:
        self._providers: dict[str, CapabilityProvider] = {}
        self._policy = policy

    def register(self, provider: CapabilityProvider) -> None:
        if not provider.provider_id or provider.provider_id in self._providers:
            raise ValueError("capability provider is already registered")
        self._providers[provider.provider_id] = provider

    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    async def call(
        self,
        provider_id: str,
        payload: dict[str, Any],
        envelope: BehaviorEnvelope,
        *,
        trust_mode: str,
    ) -> object:
        provider = self._providers.get(provider_id)
        if provider is None or trust_mode not in provider.trust_modes:
            raise CapabilityDenied("capability is not available")
        if provider.side_effect != envelope.side_effect:
            raise CapabilityDenied("capability side effect does not match the request")
        if self._policy is None or not self._policy(provider, envelope):
            raise CapabilityDenied("capability request denied by policy")
        result = provider.handler(payload)
        if inspect.isawaitable(result):
            return await result
        return result
