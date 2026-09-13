from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .models import CapabilityRequest, CapabilityResult, Money


class Capability(ABC):
    name: str
    version: str

    @abstractmethod
    def validate_request(self, request: CapabilityRequest) -> None: ...

    @abstractmethod
    def estimate(self, request: CapabilityRequest) -> Money: ...

    @abstractmethod
    def execute(self, request: CapabilityRequest) -> CapabilityResult: ...

    @abstractmethod
    def status(self, provider_ref: str) -> str: ...

    @abstractmethod
    def cancel(self, provider_ref: str) -> bool: ...

    @abstractmethod
    def collect_result(self, provider_ref: str) -> CapabilityResult | None: ...


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[tuple[str, str], Capability] = {}

    def register(self, capability: Capability) -> None:
        key = (capability.name, capability.version)
        if key in self._capabilities:
            raise ValueError(f"capability already registered: {key}")
        self._capabilities[key] = capability

    def get(self, name: str, version: str) -> Capability:
        try:
            return self._capabilities[(name, version)]
        except KeyError as exc:
            raise LookupError(f"capability not registered: {name}@{version}") from exc

    def list(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._capabilities))


@dataclass(frozen=True)
class CapabilityDescriptor:
    name: str
    version: str
    provider: str
    status: str = "available"
