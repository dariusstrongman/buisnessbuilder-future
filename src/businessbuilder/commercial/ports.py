from __future__ import annotations

from typing import Protocol

from .models import Amount, CommercialEvent, Order


class SupervisedPaymentProvider(Protocol):
    """Only the provider can mint a checkout URL or authenticate a billing event."""

    def open_checkout(self, *, order: Order, idempotency_key: str,
                      success_url: str, cancel_url: str) -> tuple[str, str | None]: ...

    def verify_webhook(self, signature: str, raw_body: bytes) -> dict: ...

    def subscription_period(self, provider_subscription_ref: str) -> tuple[int, int]: ...


class BillingProvider(Protocol):
    @property
    def provider_name(self) -> str: ...


class CheckoutProvider(Protocol):
    def create_checkout(self, *, order_id: str, idempotency_key: str) -> str: ...


class SubscriptionProvider(Protocol):
    def cancel_at_period_end(self, provider_subscription_ref: str) -> None: ...

    def suspend(self, provider_subscription_ref: str, reason: str) -> None: ...


class InvoiceProvider(Protocol):
    def invoice_ref_for_subscription(self, provider_subscription_ref: str) -> str | None: ...


class RefundProvider(Protocol):
    def create_refund(self, *, provider_payment_ref: str, amount: Amount, idempotency_key: str) -> str: ...


class CommercialEventSink(Protocol):
    def publish(self, event: CommercialEvent) -> bool: ...


class RecordingCommercialEventSink:
    def __init__(self) -> None:
        self.events: list[CommercialEvent] = []
        self._ids: set[str] = set()

    def publish(self, event: CommercialEvent) -> bool:
        if event.event_id in self._ids:
            return False
        self._ids.add(event.event_id)
        self.events.append(event)
        return True
