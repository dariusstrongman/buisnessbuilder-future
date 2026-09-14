from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable, TypeVar

from .models import ProviderActionResult


T = TypeVar("T")


class EphemeralBytes:
    """Best-effort zeroized, process-local material that is never serializable."""

    __slots__ = ("_value", "_closed")

    def __init__(self, value: bytes) -> None:
        self._value = bytearray(value)
        self._closed = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(redacted=True, closed={self._closed})"

    def use(self, consumer: Callable[[memoryview], T]) -> T:
        if self._closed:
            raise RuntimeError("temporary material has been discarded")
        return consumer(memoryview(self._value))

    @property
    def discarded(self) -> bool:
        return self._closed

    def close(self) -> None:
        if not self._closed:
            for index in range(len(self._value)):
                self._value[index] = 0
            self._value.clear()
            self._closed = True

    def __enter__(self):
        if self._closed:
            raise RuntimeError("temporary material has been discarded")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class EphemeralSecret(EphemeralBytes):
    pass


class EphemeralArtifact(EphemeralBytes):
    pass


class SecretStorePort(ABC):
    @abstractmethod
    def resolve(self, locator: str) -> EphemeralSecret: ...


class ArtifactStorePort(ABC):
    @abstractmethod
    def put(self, object_key: str, content: bytes, *, content_type: str, content_sha256: str) -> None: ...

    @abstractmethod
    def read(self, object_key: str, *, maximum_bytes: int) -> EphemeralArtifact: ...

    @abstractmethod
    def signed_download(self, object_key: str, *, expires_in_seconds: int) -> str: ...


class ExternalProviderPort(ABC):
    provider: str

    @abstractmethod
    def execute(
        self,
        *,
        operation: str,
        provider_request_id: str,
        idempotency_key: str,
        credential: EphemeralSecret,
        artifacts: tuple[EphemeralArtifact, ...],
    ) -> ProviderActionResult: ...

    def reconcile(self, provider_request_id: str) -> ProviderActionResult | None:
        del provider_request_id
        return None
