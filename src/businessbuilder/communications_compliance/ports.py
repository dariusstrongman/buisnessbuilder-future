from __future__ import annotations

from typing import Protocol

from businessbuilder.outbound_communications.models import VerifiedDeliveryEvent

from .models import OperationalAlert


class ProviderCallbackAuthenticator(Protocol):
    """Provider-neutral authenticity boundary for delivery and compliance callbacks."""

    def verify_delivery_callback(self, *, body: bytes, signature: str) -> VerifiedDeliveryEvent: ...


class OperationalNotificationPort(Protocol):
    def notify(self, alert: OperationalAlert) -> None: ...


class NoopOperationalNotificationAdapter:
    """Test adapter which deliberately performs no external notification."""

    def notify(self, alert: OperationalAlert) -> None:
        del alert

