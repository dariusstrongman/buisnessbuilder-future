from __future__ import annotations

from typing import Any, Protocol

from .models import NormalizedBillingEvent


class BillingEventTranslator(Protocol):
    """Provider adapter seam. Domain code accepts only this normalized result."""

    def normalize(self, provider_headers: dict[str, str], provider_body: bytes) -> NormalizedBillingEvent:
        """Verify authenticity and translate provider payload outside the domain."""


class FixtureBillingEventTranslator:
    """Offline-only translator for already constructed, secret-free fixtures."""

    def normalize_fixture(self, value: NormalizedBillingEvent) -> NormalizedBillingEvent:
        if value.raw_payload is not None:
            raise ValueError("fixtures cannot carry raw provider payloads")
        return value
