from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import re
from typing import Any


class ProductCode(StrEnum):
    BUILD_WEBSITE = "BUILD_WEBSITE"
    BUILD_BUSINESS = "BUILD_BUSINESS"
    BUILD_AND_RUN = "BUILD_AND_RUN"


class BillingMode(StrEnum):
    ONE_TIME = "one_time"
    RECURRING = "recurring"


class EntitlementClass(StrEnum):
    CUSTOMER_OWNED = "customer_owned"
    STROMATION_MANAGED = "stromation_managed"


class EntitlementStatus(StrEnum):
    ACTIVE = "active"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    SUSPENDED = "suspended"


class OrderStatus(StrEnum):
    DRAFT = "draft"
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    FULFILLMENT_PENDING = "fulfillment_pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    PAYMENT_FAILED = "payment_failed"
    CANCELED = "canceled"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"


class CheckoutStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELED = "canceled"


class SubscriptionStatus(StrEnum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCEL_AT_PERIOD_END = "cancel_at_period_end"
    CANCELED = "canceled"
    SUSPENDED = "suspended"


class RenewalState(StrEnum):
    WILL_RENEW = "will_renew"
    WILL_CANCEL = "will_cancel"
    ENDED = "ended"


class RefundKind(StrEnum):
    FULL = "full"
    PARTIAL = "partial"


class CancellationTiming(StrEnum):
    BEFORE_FULFILLMENT = "before_fulfillment"
    AFTER_FULFILLMENT_STARTED = "after_fulfillment_started"
    PERIOD_END = "period_end"
    IMMEDIATE_SECURITY = "immediate_security"


@dataclass(frozen=True, slots=True)
class Amount:
    currency: str
    minor_units: int

    def __post_init__(self) -> None:
        if len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError("currency must be an uppercase ISO-style code")
        if not isinstance(self.minor_units, int) or isinstance(self.minor_units, bool) or self.minor_units < 0:
            raise ValueError("minor_units must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class Feature:
    feature_code: str
    description: str
    entitlement_class: EntitlementClass
    retains_read_export_after_end: bool


@dataclass(frozen=True, slots=True)
class Entitlement:
    entitlement_code: str
    feature_code: str
    capability: str
    entitlement_class: EntitlementClass


@dataclass(frozen=True, slots=True)
class Package:
    package_code: ProductCode
    display_name: str
    billing_mode: BillingMode
    entitlement_codes: tuple[str, ...]
    price_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ProductVersion:
    product_version_id: str
    product_code: ProductCode
    version: int
    package: Package
    features: tuple[Feature, ...]
    entitlements: tuple[Entitlement, ...]
    effective_at: datetime
    retired_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Product:
    product_code: ProductCode
    display_name: str
    current_version_id: str


@dataclass(frozen=True, slots=True)
class OrderItem:
    order_item_id: str
    product_code: ProductCode
    product_version_id: str
    package_name_snapshot: str
    billing_mode: BillingMode
    quantity: int
    unit_amount: Amount | None

    def __post_init__(self) -> None:
        if self.quantity < 1:
            raise ValueError("quantity must be positive")


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    tenant_id: str
    user_id: str
    company_id: str
    status: OrderStatus
    items: tuple[OrderItem, ...]
    created_at: datetime
    updated_at: datetime
    version: int = 1
    checkout_intent_id: str | None = None
    payment_intent_ref: str | None = None
    total: Amount | None = None


@dataclass(frozen=True, slots=True)
class CheckoutIntent:
    checkout_intent_id: str
    tenant_id: str
    user_id: str
    company_id: str
    order_id: str
    provider_ref: str | None
    status: CheckoutStatus
    idempotency_key: str
    created_at: datetime
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PaymentIntentRef:
    payment_ref_id: str
    tenant_id: str
    company_id: str
    order_id: str
    provider_ref: str
    status: str
    amount: Amount | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RefundRecord:
    refund_id: str
    tenant_id: str
    company_id: str
    order_id: str
    payment_ref_id: str
    provider_ref: str
    kind: RefundKind
    amount: Amount
    reason: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CancellationRecord:
    cancellation_id: str
    tenant_id: str
    company_id: str
    order_id: str | None
    subscription_id: str | None
    timing: CancellationTiming
    reason: str
    requested_by_user_id: str
    requested_at: datetime
    effective_at: datetime

    def __post_init__(self) -> None:
        if (self.order_id is None) == (self.subscription_id is None):
            raise ValueError("cancellation must target exactly one order or subscription")


@dataclass(frozen=True, slots=True)
class BillingPeriod:
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.ends_at <= self.starts_at:
            raise ValueError("billing period end must follow start")


@dataclass(frozen=True, slots=True)
class GracePeriod:
    starts_at: datetime
    ends_at: datetime
    restrict_automation_at: datetime

    def __post_init__(self) -> None:
        if not self.starts_at <= self.restrict_automation_at <= self.ends_at:
            raise ValueError("automation restriction must occur within grace period")


@dataclass(frozen=True, slots=True)
class CancellationPolicy:
    policy_id: str
    cancel_at_period_end_by_default: bool = True
    immediate_security_suspension_allowed: bool = True
    refund_policy_ref: str | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionPlanRef:
    product_code: ProductCode
    product_version_id: str
    provider_plan_ref: str | None = None


@dataclass(frozen=True, slots=True)
class Subscription:
    subscription_id: str
    tenant_id: str
    user_id: str
    company_id: str
    order_id: str
    provider_ref: str
    plan: SubscriptionPlanRef
    status: SubscriptionStatus
    billing_period: BillingPeriod
    renewal_state: RenewalState
    cancellation_policy: CancellationPolicy
    created_at: datetime
    updated_at: datetime
    version: int = 1
    grace_period: GracePeriod | None = None


@dataclass(frozen=True, slots=True)
class EntitlementGrant:
    grant_id: str
    tenant_id: str
    user_id: str
    company_id: str
    entitlement_code: str
    entitlement_class: EntitlementClass
    status: EntitlementStatus
    source_order_id: str
    source_subscription_id: str | None
    granted_at: datetime
    updated_at: datetime
    version: int = 1
    effective_until: datetime | None = None
    status_reason: str | None = None


@dataclass(frozen=True, slots=True)
class OrderAuditEvent:
    audit_event_id: str
    tenant_id: str
    company_id: str
    actor_id: str
    action: str
    target_type: str
    target_id: str
    occurred_at: datetime
    reason: str
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SubscriptionAuditEvent:
    audit_event_id: str
    tenant_id: str
    company_id: str
    actor_id: str
    subscription_id: str
    prior_status: SubscriptionStatus | None
    new_status: SubscriptionStatus
    occurred_at: datetime
    reason: str
    source: str


@dataclass(frozen=True, slots=True)
class CommercialEvent:
    event_id: str
    event_type: str
    tenant_id: str
    user_id: str
    company_id: str
    occurred_at: datetime
    source: str
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]

    def to_contract(self) -> dict[str, Any]:
        return {
            "schema_version": "commercial-event.v1",
            "event_id": self.event_id,
            "event_type": self.event_type,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "company_id": self.company_id,
            "occurred_at": self.occurred_at.isoformat().replace("+00:00", "Z"),
            "source": self.source,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class NormalizedBillingEvent:
    event_id: str
    event_type: str
    tenant_id: str
    user_id: str
    company_id: str
    occurred_at: datetime
    provider: str
    provider_event_ref: str
    correlation_id: str
    order_id: str | None = None
    checkout_intent_id: str | None = None
    payment_provider_ref: str | None = None
    subscription_provider_ref: str | None = None
    subscription_status: SubscriptionStatus | None = None
    amount: Amount | None = None
    product_version_id: str | None = None
    current_period: BillingPeriod | None = None
    reason: str | None = None
    raw_payload: None = None

    def __post_init__(self) -> None:
        identifier = re.compile(r"^[a-z][a-z0-9_:-]{2,127}$")
        for name in (
            "event_id",
            "tenant_id",
            "user_id",
            "company_id",
            "correlation_id",
        ):
            if not identifier.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a canonical identifier")
        if not self.provider or len(self.provider) > 100:
            raise ValueError("provider must contain 1 through 100 characters")
        if not self.provider_event_ref or len(self.provider_event_ref) > 255:
            raise ValueError(
                "provider_event_ref must contain 1 through 255 characters"
            )
        for name, limit in (
            ("order_id", 128),
            ("checkout_intent_id", 128),
            ("payment_provider_ref", 255),
            ("subscription_provider_ref", 255),
        ):
            value = getattr(self, name)
            if value is not None and (not value or len(value) > limit):
                raise ValueError(f"{name} must contain 1 through {limit} characters")
        if self.reason is not None and len(self.reason) > 2000:
            raise ValueError("reason must not exceed 2000 characters")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")

    def to_contract(self) -> dict[str, Any]:
        return {
            "schema_version": "billing-event.v1",
            "event_id": self.event_id,
            "event_type": self.event_type,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "company_id": self.company_id,
            "occurred_at": self.occurred_at.isoformat().replace("+00:00", "Z"),
            "provider": self.provider,
            "provider_event_ref": self.provider_event_ref,
            "correlation_id": self.correlation_id,
            "order_id": self.order_id,
            "checkout_intent_id": self.checkout_intent_id,
            "payment_provider_ref": self.payment_provider_ref,
            "subscription_provider_ref": self.subscription_provider_ref,
            "subscription_status": self.subscription_status.value if self.subscription_status else None,
            "amount": ({"currency": self.amount.currency, "minor_units": self.amount.minor_units} if self.amount else None),
            "current_period": (
                {
                    "starts_at": self.current_period.starts_at.isoformat().replace("+00:00", "Z"),
                    "ends_at": self.current_period.ends_at.isoformat().replace("+00:00", "Z"),
                }
                if self.current_period else None
            ),
            "reason": self.reason,
        }


ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.DRAFT: frozenset({OrderStatus.PENDING_PAYMENT, OrderStatus.CANCELED}),
    OrderStatus.PENDING_PAYMENT: frozenset({OrderStatus.PAID, OrderStatus.PAYMENT_FAILED, OrderStatus.CANCELED}),
    OrderStatus.PAYMENT_FAILED: frozenset({OrderStatus.PAID, OrderStatus.CANCELED}),
    OrderStatus.PAID: frozenset({OrderStatus.FULFILLMENT_PENDING, OrderStatus.CANCELED, OrderStatus.REFUNDED, OrderStatus.PARTIALLY_REFUNDED}),
    OrderStatus.FULFILLMENT_PENDING: frozenset({OrderStatus.ACTIVE, OrderStatus.CANCELED, OrderStatus.REFUNDED, OrderStatus.PARTIALLY_REFUNDED}),
    OrderStatus.ACTIVE: frozenset({OrderStatus.COMPLETED, OrderStatus.CANCELED, OrderStatus.REFUNDED, OrderStatus.PARTIALLY_REFUNDED}),
    OrderStatus.COMPLETED: frozenset({OrderStatus.PARTIALLY_REFUNDED, OrderStatus.REFUNDED}),
    OrderStatus.PARTIALLY_REFUNDED: frozenset({OrderStatus.REFUNDED}),
    OrderStatus.CANCELED: frozenset({OrderStatus.PARTIALLY_REFUNDED, OrderStatus.REFUNDED}),
    OrderStatus.REFUNDED: frozenset(),
}


SUBSCRIPTION_TRANSITIONS: dict[SubscriptionStatus, frozenset[SubscriptionStatus]] = {
    SubscriptionStatus.TRIALING: frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE, SubscriptionStatus.CANCEL_AT_PERIOD_END, SubscriptionStatus.CANCELED, SubscriptionStatus.SUSPENDED}),
    SubscriptionStatus.ACTIVE: frozenset({SubscriptionStatus.PAST_DUE, SubscriptionStatus.CANCEL_AT_PERIOD_END, SubscriptionStatus.CANCELED, SubscriptionStatus.SUSPENDED}),
    SubscriptionStatus.PAST_DUE: frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCEL_AT_PERIOD_END, SubscriptionStatus.CANCELED, SubscriptionStatus.SUSPENDED}),
    SubscriptionStatus.CANCEL_AT_PERIOD_END: frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELED, SubscriptionStatus.SUSPENDED}),
    SubscriptionStatus.SUSPENDED: frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELED}),
    SubscriptionStatus.CANCELED: frozenset(),
}


ENTITLEMENT_TRANSITIONS: dict[EntitlementStatus, frozenset[EntitlementStatus]] = {
    EntitlementStatus.ACTIVE: frozenset({EntitlementStatus.EXPIRING, EntitlementStatus.EXPIRED, EntitlementStatus.SUSPENDED}),
    EntitlementStatus.EXPIRING: frozenset({EntitlementStatus.ACTIVE, EntitlementStatus.EXPIRED, EntitlementStatus.SUSPENDED}),
    EntitlementStatus.SUSPENDED: frozenset({EntitlementStatus.ACTIVE, EntitlementStatus.EXPIRED}),
    EntitlementStatus.EXPIRED: frozenset(),
}
