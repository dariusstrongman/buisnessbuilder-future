"""Signed Stripe events translated using persisted provider references only."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

from .models import Amount, BillingPeriod, NormalizedBillingEvent, SubscriptionStatus
from .repository import CommercialConflict, CommercialRepository
from .service import CommercialService
from .stripe_test import StripeTestPaymentProvider


class StripeWebhookIngress:
    def __init__(self, repository: CommercialRepository, commercial: CommercialService,
                 provider: StripeTestPaymentProvider) -> None:
        self.repository = repository
        self.commercial = commercial
        self.provider = provider

    def handle(self, signature: str, raw_body: bytes) -> int:
        event = self.provider.verify_webhook(signature, raw_body)
        if event.get("livemode") is not False:
            raise ValueError("only Stripe test-mode events are accepted")
        event_type = event["type"]
        value = event["data"].get("object")
        if not isinstance(value, dict):
            raise ValueError("provider object is invalid")
        created = event.get("created")
        if not isinstance(created, int):
            raise ValueError("provider event timestamp is invalid")
        occurred_at = datetime.fromtimestamp(created, timezone.utc)
        event_ref = event["id"]
        if event_type in {
            "checkout.session.completed", "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed", "checkout.session.expired",
        }:
            return self._session(event_type, value, event_ref, occurred_at)
        if event_type in {"customer.subscription.updated", "customer.subscription.deleted", "invoice.payment_failed", "invoice.paid"}:
            return self._subscription(event_type, value, event_ref, occurred_at)
        if event_type == "refund.created":
            return self._refund(value, event_ref, occurred_at)
        return 0  # Unknown events cannot mutate Commercial.

    @staticmethod
    def _event_id(ref: str, stage: str) -> str:
        return "billing_stripe_" + sha256((ref + ":" + stage).encode()).hexdigest()[:32]

    def _normalized(self, *, ref: str, stage: str, event_type: str,
                    order, occurred_at: datetime, **values) -> NormalizedBillingEvent:
        return NormalizedBillingEvent(
            self._event_id(ref, stage), event_type,
            order.tenant_id, order.user_id, order.company_id, occurred_at,
            "stripe", ref + ":" + stage,
            self._event_id(ref, "correlation"), order_id=order.order_id,
            **values,
        )

    def _session(self, event_type: str, value: dict, ref: str,
                 occurred_at: datetime) -> int:
        session_ref = value.get("id")
        if not isinstance(session_ref, str):
            raise ValueError("session reference required")
        checkout = self.repository.get_checkout_by_provider_ref(session_ref)
        order = self.repository.get_order(checkout.tenant_id, checkout.company_id, checkout.order_id)
        if value.get("client_reference_id") != order.order_id:
            raise CommercialConflict("provider session owner/order mismatch")
        if event_type == "checkout.session.expired":
            return int(self.commercial.handle_billing_event(self._normalized(
                ref=ref, stage="expiry", event_type="billing.checkout.expired", order=order,
                occurred_at=occurred_at, checkout_intent_id=checkout.checkout_intent_id,
            )))
        if event_type == "checkout.session.async_payment_failed":
            return int(self.commercial.handle_billing_event(self._normalized(
                ref=ref, stage="failure", event_type="billing.payment.failed", order=order,
                occurred_at=occurred_at, checkout_intent_id=checkout.checkout_intent_id,
            )))
        if value.get("payment_status") != "paid":
            # Completing the browser flow does not prove money moved.
            return int(self.commercial.handle_billing_event(self._normalized(
                ref=ref, stage="checkout", event_type="billing.checkout.completed", order=order,
                occurred_at=occurred_at, checkout_intent_id=checkout.checkout_intent_id,
            )))
        currency = value.get("currency")
        subtotal = value.get("amount_subtotal")
        total = value.get("amount_total")
        if currency != "usd" or not isinstance(subtotal, int) or not isinstance(total, int) or (
            order.total is None or subtotal != order.total.minor_units or total < subtotal
        ):
            raise CommercialConflict("provider currency/subtotal does not match canonical order")
        applied = int(self.commercial.handle_billing_event(self._normalized(
            ref=ref, stage="checkout", event_type="billing.checkout.completed", order=order,
            occurred_at=occurred_at, checkout_intent_id=checkout.checkout_intent_id,
        )))
        payment_ref = value.get("payment_intent") or session_ref
        if not isinstance(payment_ref, str):
            raise CommercialConflict("provider payment reference is absent")
        applied += int(self.commercial.handle_billing_event(self._normalized(
            ref=ref, stage="payment", event_type="billing.payment.succeeded", order=order,
            occurred_at=occurred_at, checkout_intent_id=checkout.checkout_intent_id,
            payment_provider_ref=payment_ref, amount=Amount("USD", total),
        )))
        subscription_ref = value.get("subscription")
        if subscription_ref:
            starts, ends = self.provider.subscription_period(subscription_ref)
            applied += int(self.commercial.handle_billing_event(self._normalized(
                ref=ref, stage="subscription", event_type="billing.subscription.created", order=order,
                occurred_at=occurred_at, subscription_provider_ref=subscription_ref,
                current_period=BillingPeriod(datetime.fromtimestamp(starts, timezone.utc),
                                             datetime.fromtimestamp(ends, timezone.utc)),
                subscription_status=SubscriptionStatus.ACTIVE,
            )))
        return applied

    def _subscription(self, event_type: str, value: dict, ref: str,
                      occurred_at: datetime) -> int:
        subscription_ref = value.get("id") if event_type.startswith("customer.") else value.get("subscription")
        if not subscription_ref and isinstance(value.get("parent"), dict):
            details = value["parent"].get("subscription_details")
            if isinstance(details, dict):
                subscription_ref = details.get("subscription")
        if not isinstance(subscription_ref, str):
            raise ValueError("subscription reference is absent")
        subscription = self.repository.get_subscription_by_provider_ref_any(subscription_ref)
        order = self.repository.get_order(subscription.tenant_id, subscription.company_id, subscription.order_id)
        if event_type == "invoice.payment_failed":
            normalized = self._normalized(
                ref=ref, stage="renewal_failure", event_type="billing.payment.failed",
                order=order, occurred_at=occurred_at,
                subscription_provider_ref=subscription_ref,
            )
        elif event_type == "customer.subscription.deleted":
            normalized = self._normalized(
                ref=ref, stage="canceled", event_type="billing.subscription.canceled",
                order=order, occurred_at=occurred_at,
                subscription_provider_ref=subscription_ref,
            )
        else:
            if event_type == "invoice.paid":
                status = SubscriptionStatus.ACTIVE
            elif value.get("cancel_at_period_end"):
                status = SubscriptionStatus.CANCEL_AT_PERIOD_END
            else:
                status_map = {"active": SubscriptionStatus.ACTIVE, "past_due": SubscriptionStatus.PAST_DUE,
                              "canceled": SubscriptionStatus.CANCELED, "unpaid": SubscriptionStatus.SUSPENDED}
                status = status_map.get(value.get("status"))
                if status is None:
                    raise ValueError("unsupported subscription status")
            normalized = self._normalized(
                ref=ref, stage="status", event_type=(
                    "billing.subscription.canceled" if status is SubscriptionStatus.CANCELED
                    else "billing.subscription.updated"
                ),
                order=order, occurred_at=occurred_at,
                subscription_provider_ref=subscription_ref,
                subscription_status=(None if status is SubscriptionStatus.CANCELED else status),
            )
        return int(self.commercial.handle_billing_event(normalized))

    def _refund(self, value: dict, ref: str, occurred_at: datetime) -> int:
        payment_ref = value.get("payment_intent")
        amount = value.get("amount")
        if not isinstance(payment_ref, str) or not isinstance(amount, int) or amount <= 0:
            raise ValueError("refund references are invalid")
        payment = self.repository.get_payment_by_provider_ref(payment_ref)
        order = self.repository.get_order(payment.tenant_id, payment.company_id, payment.order_id)
        normalized = self._normalized(
            ref=ref, stage="refund", event_type="billing.refund.created", order=order,
            occurred_at=occurred_at, payment_provider_ref=payment_ref,
            amount=Amount("USD", amount),
        )
        return int(self.commercial.handle_billing_event(normalized))
