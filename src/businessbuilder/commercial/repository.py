from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
import sqlite3
from threading import RLock
from typing import Iterator

from businessbuilder._serialization import decode_record, encode_record

from .models import (
    CancellationRecord,
    CheckoutIntent,
    CommercialQuote,
    CommercialAdmissionRecord,
    CommercialOperatorGrant,
    CommercialEvent,
    EntitlementGrant,
    Order,
    OrderAuditEvent,
    OutboxMessage,
    OutboxStatus,
    PaymentIntentRef,
    Product,
    ProductVersion,
    RefundRecord,
    Subscription,
    SubscriptionAuditEvent,
)


class CommercialNotFound(LookupError):
    pass


class CommercialConflict(RuntimeError):
    pass


class CommercialRepository(ABC):
    @abstractmethod
    def transaction(self) -> Iterator[None]: ...

    @abstractmethod
    def billing_event_transaction(
        self, provider: str, provider_event_ref: str
    ) -> Iterator[bool]: ...

    @abstractmethod
    def enqueue_outbox(self, event, idempotency_key: str) -> OutboxMessage: ...

    @abstractmethod
    def claim_outbox(
        self,
        dispatcher_id: str,
        *,
        at: datetime,
        lease: timedelta,
        limit: int,
    ) -> tuple[OutboxMessage, ...]: ...

    @abstractmethod
    def acknowledge_outbox(
        self, outbox_id: str, dispatcher_id: str, *, at: datetime
    ) -> OutboxMessage: ...

    @abstractmethod
    def release_outbox(
        self,
        outbox_id: str,
        dispatcher_id: str,
        *,
        retry_at: datetime,
        error: str,
    ) -> OutboxMessage: ...

    @abstractmethod
    def list_outbox(self) -> tuple[OutboxMessage, ...]: ...

    @abstractmethod
    def save_product(self, product: Product, version: ProductVersion) -> None: ...

    @abstractmethod
    def get_product_version(self, product_version_id: str) -> ProductVersion: ...

    @abstractmethod
    def append_order(self, order: Order) -> None: ...

    @abstractmethod
    def append_quote(self, quote: CommercialQuote) -> None: ...

    @abstractmethod
    def get_quote(self, tenant_id: str, company_id: str, quote_id: str) -> CommercialQuote: ...

    @abstractmethod
    def append_operator_grant(self, grant: CommercialOperatorGrant) -> None: ...

    @abstractmethod
    def get_operator_grant(self, tenant_id: str, company_id: str, grant_id: str) -> CommercialOperatorGrant: ...

    @abstractmethod
    def append_admission(self, admission: CommercialAdmissionRecord) -> None: ...

    @abstractmethod
    def get_admission(self, tenant_id: str, company_id: str, admission_id: str) -> CommercialAdmissionRecord: ...

    @abstractmethod
    def gate_history(self, tenant_id: str, company_id: str, order_id: str, kind) -> tuple[object, ...]: ...

    @abstractmethod
    def append_release_gate(self, record) -> None: ...

    @abstractmethod
    def get_release_packet(self, tenant_id: str, company_id: str, order_id: str): ...

    @abstractmethod
    def append_release_packet(self, packet) -> None: ...

    @abstractmethod
    def get_order(self, tenant_id: str, company_id: str, order_id: str) -> Order: ...

    @abstractmethod
    def list_current_orders(self, tenant_id: str, company_id: str) -> tuple[Order, ...]: ...

    @abstractmethod
    def order_history(self, tenant_id: str, company_id: str, order_id: str) -> tuple[Order, ...]: ...

    @abstractmethod
    def save_checkout(self, checkout: CheckoutIntent) -> None: ...

    @abstractmethod
    def get_checkout(self, tenant_id: str, company_id: str, checkout_id: str) -> CheckoutIntent: ...

    @abstractmethod
    def get_checkout_by_idempotency(self, tenant_id: str, company_id: str, key: str) -> CheckoutIntent | None: ...

    @abstractmethod
    def get_checkout_by_provider_ref(self, provider_ref: str) -> CheckoutIntent: ...

    @abstractmethod
    def save_payment(self, payment: PaymentIntentRef) -> None: ...

    @abstractmethod
    def get_payment_for_order(self, tenant_id: str, company_id: str, order_id: str) -> PaymentIntentRef | None: ...

    @abstractmethod
    def append_refund(self, refund: RefundRecord) -> None: ...

    @abstractmethod
    def list_refunds(self, tenant_id: str, company_id: str, order_id: str) -> tuple[RefundRecord, ...]: ...

    @abstractmethod
    def append_cancellation(self, record: CancellationRecord) -> None: ...

    @abstractmethod
    def append_subscription(self, subscription: Subscription) -> None: ...

    @abstractmethod
    def get_subscription(self, tenant_id: str, company_id: str, subscription_id: str) -> Subscription: ...

    @abstractmethod
    def get_subscription_by_provider_ref(self, tenant_id: str, company_id: str, provider_ref: str) -> Subscription | None: ...

    @abstractmethod
    def get_subscription_by_provider_ref_any(self, provider_ref: str) -> Subscription: ...

    @abstractmethod
    def subscription_history(self, tenant_id: str, company_id: str, subscription_id: str) -> tuple[Subscription, ...]: ...

    @abstractmethod
    def list_current_subscriptions(self, tenant_id: str, company_id: str) -> tuple[Subscription, ...]: ...

    @abstractmethod
    def append_entitlement_grant(self, grant: EntitlementGrant) -> None: ...

    @abstractmethod
    def get_current_entitlement_grants(self, tenant_id: str, company_id: str) -> tuple[EntitlementGrant, ...]: ...

    @abstractmethod
    def entitlement_history(self, tenant_id: str, company_id: str, grant_id: str) -> tuple[EntitlementGrant, ...]: ...

    @abstractmethod
    def append_order_audit(self, event: OrderAuditEvent) -> None: ...

    @abstractmethod
    def append_subscription_audit(self, event: SubscriptionAuditEvent) -> None: ...

    @abstractmethod
    def list_audit(self, tenant_id: str, company_id: str) -> tuple[object, ...]: ...

    @abstractmethod
    def billing_event_processed(self, provider: str, provider_event_ref: str) -> bool: ...

    @abstractmethod
    def mark_billing_event_processed(self, provider: str, provider_event_ref: str) -> None: ...


class InMemoryCommercialRepository(CommercialRepository):
    """Append-versioned offline ledger with tenant-scoped reads."""

    def __init__(self) -> None:
        self.lock = RLock()
        self._transaction_depth = 0
        self.products: dict[str, Product] = {}
        self.product_versions: dict[str, ProductVersion] = {}
        self.orders: dict[tuple[str, str, str], list[Order]] = {}
        self.quotes: dict[tuple[str, str, str], list[CommercialQuote]] = {}
        self.operator_grants: dict[tuple[str, str, str], list[CommercialOperatorGrant]] = {}
        self.admissions: dict[tuple[str, str, str], CommercialAdmissionRecord] = {}
        self.checkouts: dict[tuple[str, str, str], CheckoutIntent] = {}
        self.payments: dict[tuple[str, str, str], PaymentIntentRef] = {}
        self.refunds: list[RefundRecord] = []
        self.cancellations: list[CancellationRecord] = []
        self.subscriptions: dict[tuple[str, str, str], list[Subscription]] = {}
        self.entitlement_grants: dict[tuple[str, str, str], list[EntitlementGrant]] = {}
        self.release_gates: dict[tuple[str, str, str, str], list[object]] = {}
        self.release_packets: dict[tuple[str, str, str], object] = {}
        self.audit: list[object] = []
        self.processed_events: set[tuple[str, str]] = set()
        self.outbox: dict[str, OutboxMessage] = {}
        self.outbox_order: list[str] = []

    _STATE_FIELDS = (
        "products",
        "product_versions",
        "orders",
        "quotes",
        "operator_grants",
        "admissions",
        "checkouts",
        "payments",
        "refunds",
        "cancellations",
        "subscriptions",
        "entitlement_grants",
        "release_gates",
        "release_packets",
        "audit",
        "processed_events",
        "outbox",
        "outbox_order",
    )

    def _snapshot_state(self) -> dict[str, object]:
        return {name: deepcopy(getattr(self, name)) for name in self._STATE_FIELDS}

    def _restore_state(self, snapshot: dict[str, object]) -> None:
        for name, value in snapshot.items():
            setattr(self, name, value)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.lock:
            if self._transaction_depth:
                self._transaction_depth += 1
                try:
                    yield
                finally:
                    self._transaction_depth -= 1
                return
            snapshot = self._snapshot_state()
            self._transaction_depth = 1
            try:
                yield
            except Exception:
                self._restore_state(snapshot)
                raise
            finally:
                self._transaction_depth = 0

    @contextmanager
    def billing_event_transaction(
        self, provider: str, provider_event_ref: str
    ) -> Iterator[bool]:
        with self.transaction():
            if self.billing_event_processed(provider, provider_event_ref):
                yield False
                return
            yield True
            self.mark_billing_event_processed(provider, provider_event_ref)

    def enqueue_outbox(self, event, idempotency_key: str) -> OutboxMessage:
        if not idempotency_key:
            raise ValueError("outbox idempotency_key is required")
        with self.lock:
            existing = next(
                (
                    item
                    for item in self.outbox.values()
                    if item.idempotency_key == idempotency_key
                ),
                None,
            )
            if existing:
                if existing.event != event:
                    raise CommercialConflict(
                        "outbox idempotency key cannot identify different events"
                    )
                return existing
            outbox_id = "outbox_" + sha256(idempotency_key.encode()).hexdigest()[:24]
            message = OutboxMessage(
                outbox_id,
                idempotency_key,
                event,
                OutboxStatus.PENDING,
                0,
                event.occurred_at,
                event.occurred_at,
            )
            self.outbox[outbox_id] = message
            self.outbox_order.append(outbox_id)
            return message

    def claim_outbox(
        self,
        dispatcher_id: str,
        *,
        at: datetime,
        lease: timedelta,
        limit: int,
    ) -> tuple[OutboxMessage, ...]:
        if not dispatcher_id or lease <= timedelta(0) or limit < 1:
            raise ValueError("dispatcher, positive lease, and positive limit required")
        claimed: list[OutboxMessage] = []
        with self.lock:
            for outbox_id in self.outbox_order:
                message = self.outbox[outbox_id]
                available = (
                    message.status is OutboxStatus.PENDING
                    and message.available_at <= at
                )
                abandoned = (
                    message.status is OutboxStatus.DISPATCHING
                    and message.claimed_until is not None
                    and message.claimed_until <= at
                )
                if not (available or abandoned):
                    continue
                changed = replace(
                    message,
                    status=OutboxStatus.DISPATCHING,
                    attempts=message.attempts + 1,
                    claimed_by=dispatcher_id,
                    claimed_until=at + lease,
                    last_error=None,
                )
                self.outbox[outbox_id] = changed
                claimed.append(changed)
                if len(claimed) == limit:
                    break
        return tuple(claimed)

    def acknowledge_outbox(
        self, outbox_id: str, dispatcher_id: str, *, at: datetime
    ) -> OutboxMessage:
        with self.lock:
            message = self.outbox.get(outbox_id)
            if message is None:
                raise CommercialNotFound("outbox message not found")
            if message.status is OutboxStatus.ACKNOWLEDGED:
                return message
            if (
                message.status is not OutboxStatus.DISPATCHING
                or message.claimed_by != dispatcher_id
            ):
                raise CommercialConflict("outbox acknowledgment claim mismatch")
            changed = replace(
                message,
                status=OutboxStatus.ACKNOWLEDGED,
                claimed_by=None,
                claimed_until=None,
                acknowledged_at=at,
            )
            self.outbox[outbox_id] = changed
            return changed

    def release_outbox(
        self,
        outbox_id: str,
        dispatcher_id: str,
        *,
        retry_at: datetime,
        error: str,
    ) -> OutboxMessage:
        with self.lock:
            message = self.outbox.get(outbox_id)
            if message is None:
                raise CommercialNotFound("outbox message not found")
            if (
                message.status is not OutboxStatus.DISPATCHING
                or message.claimed_by != dispatcher_id
            ):
                raise CommercialConflict("outbox release claim mismatch")
            changed = replace(
                message,
                status=OutboxStatus.PENDING,
                available_at=retry_at,
                claimed_by=None,
                claimed_until=None,
                last_error=error[:200],
            )
            self.outbox[outbox_id] = changed
            return changed

    def list_outbox(self) -> tuple[OutboxMessage, ...]:
        with self.lock:
            return tuple(self.outbox[item] for item in self.outbox_order)

    def save_product(self, product: Product, version: ProductVersion) -> None:
        with self.lock:
            if version.product_code != product.product_code:
                raise CommercialConflict("product/version code mismatch")
            self.products[product.product_code.value] = product
            self.product_versions[version.product_version_id] = version

    def get_product_version(self, product_version_id: str) -> ProductVersion:
        try:
            return self.product_versions[product_version_id]
        except KeyError as exc:
            raise CommercialNotFound("product version not found") from exc

    def append_order(self, order: Order) -> None:
        with self.lock:
            key = (order.tenant_id, order.company_id, order.order_id)
            history = self.orders.setdefault(key, [])
            if history and order.version != history[-1].version + 1:
                raise CommercialConflict("order versions must be append-only and contiguous")
            if not history and order.version != 1:
                raise CommercialConflict("new order must start at version 1")
            history.append(order)

    def append_quote(self, quote: CommercialQuote) -> None:
        with self.lock:
            history = self.quotes.setdefault((quote.tenant_id, quote.company_id, quote.quote_id), [])
            if quote.version != (history[-1].version + 1 if history else 1):
                raise CommercialConflict("quote versions must be contiguous")
            history.append(quote)

    def get_quote(self, tenant_id: str, company_id: str, quote_id: str) -> CommercialQuote:
        history = self.quotes.get((tenant_id, company_id, quote_id))
        if not history:
            raise CommercialNotFound("quote not found in scope")
        return history[-1]

    def append_operator_grant(self, grant: CommercialOperatorGrant) -> None:
        with self.lock:
            history = self.operator_grants.setdefault((grant.tenant_id, grant.company_id, grant.grant_id), [])
            if grant.version != (history[-1].version + 1 if history else 1):
                raise CommercialConflict("operator grant versions must be contiguous")
            history.append(grant)

    def get_operator_grant(self, tenant_id: str, company_id: str, grant_id: str) -> CommercialOperatorGrant:
        history = self.operator_grants.get((tenant_id, company_id, grant_id))
        if not history:
            raise CommercialNotFound("operator grant not found in scope")
        return history[-1]

    def append_admission(self, admission: CommercialAdmissionRecord) -> None:
        with self.lock:
            key = (admission.tenant_id, admission.company_id, admission.admission_id)
            if key in self.admissions:
                raise CommercialConflict("admission records are immutable")
            self.admissions[key] = admission

    def get_admission(self, tenant_id: str, company_id: str, admission_id: str) -> CommercialAdmissionRecord:
        admission = self.admissions.get((tenant_id, company_id, admission_id))
        if admission is None:
            raise CommercialNotFound("admission not found in scope")
        return admission

    def get_order(self, tenant_id: str, company_id: str, order_id: str) -> Order:
        history = self.orders.get((tenant_id, company_id, order_id))
        if not history:
            raise CommercialNotFound("order not found in scope")
        return history[-1]

    def list_current_orders(self, tenant_id: str, company_id: str) -> tuple[Order, ...]:
        with self.lock:
            return tuple(
                history[-1]
                for (tenant, company, _), history in self.orders.items()
                if tenant == tenant_id and company == company_id
            )

    def order_history(self, tenant_id: str, company_id: str, order_id: str) -> tuple[Order, ...]:
        return tuple(self.orders.get((tenant_id, company_id, order_id), ()))

    def save_checkout(self, checkout: CheckoutIntent) -> None:
        with self.lock:
            existing = self.get_checkout_by_idempotency(checkout.tenant_id, checkout.company_id, checkout.idempotency_key)
            if existing and existing.checkout_intent_id != checkout.checkout_intent_id:
                raise CommercialConflict("checkout idempotency key already used")
            if existing and existing.provider_ref and checkout.provider_ref != existing.provider_ref:
                raise CommercialConflict("provider checkout reference is immutable")
            if existing and existing.redirect_url and checkout.redirect_url != existing.redirect_url:
                raise CommercialConflict("checkout redirect URL is immutable")
            if checkout.provider_ref and any(
                item.provider_ref == checkout.provider_ref and item.checkout_intent_id != checkout.checkout_intent_id
                for item in self.checkouts.values()
            ):
                raise CommercialConflict("provider checkout reference already belongs to another order")
            self.checkouts[(checkout.tenant_id, checkout.company_id, checkout.checkout_intent_id)] = checkout

    def get_checkout(self, tenant_id: str, company_id: str, checkout_id: str) -> CheckoutIntent:
        try:
            return self.checkouts[(tenant_id, company_id, checkout_id)]
        except KeyError as exc:
            raise CommercialNotFound("checkout not found in scope") from exc

    def get_checkout_by_idempotency(self, tenant_id: str, company_id: str, key: str) -> CheckoutIntent | None:
        return next((item for scope, item in self.checkouts.items() if scope[:2] == (tenant_id, company_id) and item.idempotency_key == key), None)

    def get_checkout_by_provider_ref(self, provider_ref: str) -> CheckoutIntent:
        matches = [item for item in self.checkouts.values() if item.provider_ref == provider_ref]
        if len(matches) != 1:
            raise CommercialNotFound("provider checkout is not uniquely mapped")
        return matches[0]

    def save_payment(self, payment: PaymentIntentRef) -> None:
        with self.lock:
            key = (payment.tenant_id, payment.company_id, payment.payment_ref_id)
            existing = self.payments.get(key)
            if existing and existing.order_id != payment.order_id:
                raise CommercialConflict("payment reference cannot move orders")
            self.payments[key] = payment

    def get_payment_for_order(self, tenant_id: str, company_id: str, order_id: str) -> PaymentIntentRef | None:
        return next((item for (t, c, _), item in self.payments.items() if (t, c, item.order_id) == (tenant_id, company_id, order_id)), None)

    def get_payment_by_provider_ref(self, provider_ref: str) -> PaymentIntentRef:
        matches = [item for item in self.payments.values() if item.provider_ref == provider_ref]
        if len(matches) != 1:
            raise CommercialNotFound("provider payment is not uniquely mapped")
        return matches[0]

    def append_refund(self, refund: RefundRecord) -> None:
        with self.lock:
            if any(item.provider_ref == refund.provider_ref for item in self.refunds):
                return
            self.refunds.append(refund)

    def list_refunds(self, tenant_id: str, company_id: str, order_id: str) -> tuple[RefundRecord, ...]:
        return tuple(item for item in self.refunds if (item.tenant_id, item.company_id, item.order_id) == (tenant_id, company_id, order_id))

    def append_cancellation(self, record: CancellationRecord) -> None:
        with self.lock:
            if any(item.cancellation_id == record.cancellation_id for item in self.cancellations):
                return
            self.cancellations.append(record)

    def append_subscription(self, subscription: Subscription) -> None:
        with self.lock:
            key = (subscription.tenant_id, subscription.company_id, subscription.subscription_id)
            history = self.subscriptions.setdefault(key, [])
            if history and subscription.version != history[-1].version + 1:
                raise CommercialConflict("subscription versions must be append-only and contiguous")
            if not history and subscription.version != 1:
                raise CommercialConflict("new subscription must start at version 1")
            history.append(subscription)

    def get_subscription(self, tenant_id: str, company_id: str, subscription_id: str) -> Subscription:
        history = self.subscriptions.get((tenant_id, company_id, subscription_id))
        if not history:
            raise CommercialNotFound("subscription not found in scope")
        return history[-1]

    def get_subscription_by_provider_ref(self, tenant_id: str, company_id: str, provider_ref: str) -> Subscription | None:
        return next((history[-1] for (t, c, _), history in self.subscriptions.items() if (t, c) == (tenant_id, company_id) and history[-1].provider_ref == provider_ref), None)

    def get_subscription_by_provider_ref_any(self, provider_ref: str) -> Subscription:
        matches = [history[-1] for history in self.subscriptions.values() if history[-1].provider_ref == provider_ref]
        if len(matches) != 1:
            raise CommercialNotFound("provider subscription is not uniquely mapped")
        return matches[0]

    def subscription_history(self, tenant_id: str, company_id: str, subscription_id: str) -> tuple[Subscription, ...]:
        return tuple(self.subscriptions.get((tenant_id, company_id, subscription_id), ()))

    def list_current_subscriptions(self, tenant_id: str, company_id: str) -> tuple[Subscription, ...]:
        return tuple(history[-1] for (t, c, _), history in self.subscriptions.items() if (t, c) == (tenant_id, company_id))

    def append_entitlement_grant(self, grant: EntitlementGrant) -> None:
        with self.lock:
            key = (grant.tenant_id, grant.company_id, grant.grant_id)
            history = self.entitlement_grants.setdefault(key, [])
            if history and grant.version != history[-1].version + 1:
                raise CommercialConflict("entitlement versions must be append-only and contiguous")
            if not history and grant.version != 1:
                raise CommercialConflict("new entitlement grant must start at version 1")
            history.append(grant)

    def get_current_entitlement_grants(self, tenant_id: str, company_id: str) -> tuple[EntitlementGrant, ...]:
        return tuple(history[-1] for (t, c, _), history in self.entitlement_grants.items() if (t, c) == (tenant_id, company_id))

    def entitlement_history(self, tenant_id: str, company_id: str, grant_id: str) -> tuple[EntitlementGrant, ...]:
        return tuple(self.entitlement_grants.get((tenant_id, company_id, grant_id), ()))

    def append_order_audit(self, event: OrderAuditEvent) -> None:
        with self.lock:
            self.audit.append(event)

    def append_subscription_audit(self, event: SubscriptionAuditEvent) -> None:
        with self.lock:
            self.audit.append(event)

    def list_audit(self, tenant_id: str, company_id: str) -> tuple[object, ...]:
        return tuple(item for item in self.audit if getattr(item, "tenant_id", None) == tenant_id and getattr(item, "company_id", None) == company_id)

    def gate_history(self, tenant_id: str, company_id: str, order_id: str, kind) -> tuple[object, ...]:
        return tuple(self.release_gates.get((tenant_id, company_id, order_id, kind.value), ()))

    def append_release_gate(self, record) -> None:
        with self.lock:
            key = (record.tenant_id, record.company_id, record.order_id, record.kind.value)
            history = self.release_gates.setdefault(key, [])
            if record.version != len(history) + 1:
                raise CommercialConflict("release gate version conflict")
            history.append(record)

    def get_release_packet(self, tenant_id: str, company_id: str, order_id: str):
        return self.release_packets.get((tenant_id, company_id, order_id))

    def append_release_packet(self, packet) -> None:
        with self.lock:
            key = (packet.tenant_id, packet.company_id, packet.order_id)
            if key in self.release_packets:
                raise CommercialConflict("release packet cannot be silently renewed")
            self.release_packets[key] = packet

    def billing_event_processed(self, provider: str, provider_event_ref: str) -> bool:
        return (provider, provider_event_ref) in self.processed_events

    def mark_billing_event_processed(self, provider: str, provider_event_ref: str) -> None:
        with self.lock:
            self.processed_events.add((provider, provider_event_ref))


from .models import (
    Amount, BillingMode, BillingPeriod, CancellationPolicy, CancellationTiming,
    CheckoutStatus, Entitlement, EntitlementClass, EntitlementStatus, Feature,
    GracePeriod, OrderItem, OrderStatus, Package, ProductCode, RefundKind,
    PaymentEligibility, TaxDisposition, TaxReviewState, QuoteStatus,
    RenewalState, SubscriptionPlanRef, SubscriptionStatus,
)
from .paid_pilot_release import GateKind, GateStatus, ReleaseStatus, GateRecord, FirstCustomerPacket

_COMMERCIAL_TYPES = {
    item.__name__: item
    for item in (
        Product, ProductVersion, Feature, Entitlement, Package, Order, OrderItem, CommercialQuote,
        CommercialOperatorGrant, CommercialAdmissionRecord,
        CheckoutIntent, PaymentIntentRef, RefundRecord, CancellationRecord,
        Subscription, SubscriptionPlanRef, EntitlementGrant, OrderAuditEvent,
        SubscriptionAuditEvent, Amount, BillingPeriod, GracePeriod,
        CancellationPolicy, ProductCode, BillingMode, EntitlementClass,
        EntitlementStatus, OrderStatus, CheckoutStatus, SubscriptionStatus,
        RenewalState, RefundKind, CancellationTiming, CommercialEvent,
        OutboxMessage, OutboxStatus, PaymentEligibility, TaxDisposition, TaxReviewState, QuoteStatus,
        GateKind, GateStatus, ReleaseStatus, GateRecord, FirstCustomerPacket,
    )
}


class SQLiteCommercialRepository(InMemoryCommercialRepository):
    """Durable SQLite ledger; mutable aggregates are stored as immutable versions."""

    def __init__(self, path: str = ":memory:") -> None:
        super().__init__()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS commercial_records (
                kind TEXT NOT NULL, scope_key TEXT NOT NULL, version INTEGER NOT NULL,
                tenant_id TEXT, company_id TEXT, body TEXT NOT NULL,
                PRIMARY KEY(kind, scope_key, version)
            );
            CREATE INDEX IF NOT EXISTS commercial_scope ON commercial_records(tenant_id, company_id, kind);
            CREATE TABLE IF NOT EXISTS commercial_audit_events (
                audit_event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                company_id TEXT NOT NULL, body TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS commercial_audit_no_update BEFORE UPDATE ON commercial_audit_events
            BEGIN SELECT RAISE(ABORT, 'commercial audit is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS commercial_audit_no_delete BEFORE DELETE ON commercial_audit_events
            BEGIN SELECT RAISE(ABORT, 'commercial audit is append-only'); END;
            CREATE TABLE IF NOT EXISTS processed_billing_events (
                provider TEXT NOT NULL, provider_event_ref TEXT NOT NULL,
                PRIMARY KEY(provider, provider_event_ref)
            );
            CREATE TABLE IF NOT EXISTS commercial_outbox (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                outbox_id TEXT NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE,
                body TEXT NOT NULL
            );
            """
        )
        self._load_sqlite()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.lock:
            if self._transaction_depth:
                self._transaction_depth += 1
                try:
                    yield
                finally:
                    self._transaction_depth -= 1
                return
            snapshot = self._snapshot_state()
            self.connection.execute("BEGIN IMMEDIATE")
            self._transaction_depth = 1
            try:
                yield
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                self._restore_state(snapshot)
                raise
            finally:
                self._transaction_depth = 0

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _scope(tenant_id: str, company_id: str, record_id: str) -> str:
        return f"{tenant_id}\x1f{company_id}\x1f{record_id}"

    def _insert(self, kind: str, key: str, version: int, value: object, tenant_id: str | None = None, company_id: str | None = None, *, replace_row: bool = False) -> None:
        verb = "INSERT OR REPLACE" if replace_row else "INSERT"
        if self._transaction_depth:
            self.connection.execute(
                f"{verb} INTO commercial_records VALUES (?, ?, ?, ?, ?, ?)",
                (kind, key, version, tenant_id, company_id, encode_record(value)),
            )
        else:
            with self.connection:
                self.connection.execute(
                    f"{verb} INTO commercial_records VALUES (?, ?, ?, ?, ?, ?)",
                    (kind, key, version, tenant_id, company_id, encode_record(value)),
                )

    def _load_sqlite(self) -> None:
        for kind, key, version, body in self.connection.execute(
            "SELECT kind, scope_key, version, body FROM commercial_records ORDER BY rowid"
        ):
            value = decode_record(body, _COMMERCIAL_TYPES)
            if kind == "product": self.products[key] = value
            elif kind == "product_version": self.product_versions[key] = value
            elif kind == "order": self.orders.setdefault((value.tenant_id, value.company_id, value.order_id), []).append(value)
            elif kind == "quote": self.quotes.setdefault((value.tenant_id, value.company_id, value.quote_id), []).append(value)
            elif kind == "operator_grant": self.operator_grants.setdefault((value.tenant_id, value.company_id, value.grant_id), []).append(value)
            elif kind == "admission": self.admissions[(value.tenant_id, value.company_id, value.admission_id)] = value
            elif kind == "checkout": self.checkouts[(value.tenant_id, value.company_id, value.checkout_intent_id)] = value
            elif kind == "payment": self.payments[(value.tenant_id, value.company_id, value.payment_ref_id)] = value
            elif kind == "refund": self.refunds.append(value)
            elif kind == "cancellation": self.cancellations.append(value)
            elif kind == "subscription": self.subscriptions.setdefault((value.tenant_id, value.company_id, value.subscription_id), []).append(value)
            elif kind == "entitlement": self.entitlement_grants.setdefault((value.tenant_id, value.company_id, value.grant_id), []).append(value)
            elif kind == "release_gate": self.release_gates.setdefault((value.tenant_id, value.company_id, value.order_id, value.kind.value), []).append(value)
            elif kind == "release_packet": self.release_packets[(value.tenant_id, value.company_id, value.order_id)] = value
        for (body,) in self.connection.execute("SELECT body FROM commercial_audit_events ORDER BY rowid"):
            self.audit.append(decode_record(body, _COMMERCIAL_TYPES))
        self.processed_events.update(self.connection.execute("SELECT provider, provider_event_ref FROM processed_billing_events"))
        for outbox_id, body in self.connection.execute(
            "SELECT outbox_id, body FROM commercial_outbox ORDER BY sequence"
        ):
            self.outbox[outbox_id] = decode_record(body, _COMMERCIAL_TYPES)
            self.outbox_order.append(outbox_id)

    def append_release_gate(self, record) -> None:
        with self.transaction():
            super().append_release_gate(record)
            self._insert("release_gate", self._scope(record.tenant_id, record.company_id,
                f"{record.order_id}\x1f{record.kind.value}"), record.version, record,
                record.tenant_id, record.company_id)

    def append_release_packet(self, packet) -> None:
        with self.transaction():
            super().append_release_packet(packet)
            self._insert("release_packet", self._scope(packet.tenant_id, packet.company_id,
                packet.order_id), packet.version, packet, packet.tenant_id, packet.company_id)

    def _save_outbox(self, message: OutboxMessage) -> None:
        statement = """
            INSERT INTO commercial_outbox(outbox_id, idempotency_key, body)
            VALUES (?, ?, ?)
            ON CONFLICT(outbox_id) DO UPDATE SET body=excluded.body
        """
        parameters = (
            message.outbox_id,
            message.idempotency_key,
            encode_record(message),
        )
        if self._transaction_depth:
            self.connection.execute(statement, parameters)
        else:
            with self.connection:
                self.connection.execute(statement, parameters)

    def enqueue_outbox(
        self, event: CommercialEvent, idempotency_key: str
    ) -> OutboxMessage:
        message = super().enqueue_outbox(event, idempotency_key)
        self._save_outbox(message)
        return message

    def claim_outbox(
        self,
        dispatcher_id: str,
        *,
        at: datetime,
        lease: timedelta,
        limit: int,
    ) -> tuple[OutboxMessage, ...]:
        with self.transaction():
            messages = super().claim_outbox(
                dispatcher_id, at=at, lease=lease, limit=limit
            )
            for message in messages:
                self._save_outbox(message)
            return messages

    def acknowledge_outbox(
        self, outbox_id: str, dispatcher_id: str, *, at: datetime
    ) -> OutboxMessage:
        with self.transaction():
            message = super().acknowledge_outbox(
                outbox_id, dispatcher_id, at=at
            )
            self._save_outbox(message)
            return message

    def release_outbox(
        self,
        outbox_id: str,
        dispatcher_id: str,
        *,
        retry_at: datetime,
        error: str,
    ) -> OutboxMessage:
        with self.transaction():
            message = super().release_outbox(
                outbox_id,
                dispatcher_id,
                retry_at=retry_at,
                error=error,
            )
            self._save_outbox(message)
            return message

    def save_product(self, product: Product, version: ProductVersion) -> None:
        super().save_product(product, version)
        self._insert("product", product.product_code.value, 1, product, replace_row=True)
        self._insert("product_version", version.product_version_id, version.version, version, replace_row=True)

    def append_order(self, order: Order) -> None:
        super().append_order(order)
        self._insert("order", self._scope(order.tenant_id, order.company_id, order.order_id), order.version, order, order.tenant_id, order.company_id)

    def append_quote(self, quote: CommercialQuote) -> None:
        super().append_quote(quote)
        self._insert("quote", self._scope(quote.tenant_id, quote.company_id, quote.quote_id), quote.version, quote, quote.tenant_id, quote.company_id)

    def append_operator_grant(self, grant: CommercialOperatorGrant) -> None:
        super().append_operator_grant(grant)
        self._insert("operator_grant", self._scope(grant.tenant_id, grant.company_id, grant.grant_id), grant.version, grant, grant.tenant_id, grant.company_id)

    def append_admission(self, admission: CommercialAdmissionRecord) -> None:
        super().append_admission(admission)
        self._insert("admission", self._scope(admission.tenant_id, admission.company_id, admission.admission_id), 1, admission, admission.tenant_id, admission.company_id)

    def save_checkout(self, checkout: CheckoutIntent) -> None:
        super().save_checkout(checkout)
        self._insert("checkout", self._scope(checkout.tenant_id, checkout.company_id, checkout.checkout_intent_id), 1, checkout, checkout.tenant_id, checkout.company_id, replace_row=True)

    def save_payment(self, payment: PaymentIntentRef) -> None:
        super().save_payment(payment)
        self._insert("payment", self._scope(payment.tenant_id, payment.company_id, payment.payment_ref_id), 1, payment, payment.tenant_id, payment.company_id, replace_row=True)

    def append_refund(self, refund: RefundRecord) -> None:
        if any(item.provider_ref == refund.provider_ref for item in self.refunds):
            return
        super().append_refund(refund)
        self._insert("refund", self._scope(refund.tenant_id, refund.company_id, refund.refund_id), 1, refund, refund.tenant_id, refund.company_id)

    def append_cancellation(self, record: CancellationRecord) -> None:
        if any(item.cancellation_id == record.cancellation_id for item in self.cancellations):
            return
        super().append_cancellation(record)
        self._insert("cancellation", self._scope(record.tenant_id, record.company_id, record.cancellation_id), 1, record, record.tenant_id, record.company_id)

    def append_subscription(self, subscription: Subscription) -> None:
        super().append_subscription(subscription)
        self._insert("subscription", self._scope(subscription.tenant_id, subscription.company_id, subscription.subscription_id), subscription.version, subscription, subscription.tenant_id, subscription.company_id)

    def append_entitlement_grant(self, grant: EntitlementGrant) -> None:
        super().append_entitlement_grant(grant)
        self._insert("entitlement", self._scope(grant.tenant_id, grant.company_id, grant.grant_id), grant.version, grant, grant.tenant_id, grant.company_id)

    def _append_audit_row(self, event: object) -> None:
        if self._transaction_depth:
            self.connection.execute(
                "INSERT INTO commercial_audit_events VALUES (?, ?, ?, ?)",
                (event.audit_event_id, event.tenant_id, event.company_id, encode_record(event)),
            )
        else:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO commercial_audit_events VALUES (?, ?, ?, ?)",
                    (event.audit_event_id, event.tenant_id, event.company_id, encode_record(event)),
                )

    def append_order_audit(self, event: OrderAuditEvent) -> None:
        super().append_order_audit(event); self._append_audit_row(event)

    def append_subscription_audit(self, event: SubscriptionAuditEvent) -> None:
        super().append_subscription_audit(event); self._append_audit_row(event)

    def mark_billing_event_processed(self, provider: str, provider_event_ref: str) -> None:
        if self._transaction_depth:
            self.connection.execute(
                "INSERT OR IGNORE INTO processed_billing_events VALUES (?, ?)",
                (provider, provider_event_ref),
            )
        else:
            with self.connection:
                self.connection.execute(
                    "INSERT OR IGNORE INTO processed_billing_events VALUES (?, ?)",
                    (provider, provider_event_ref),
                )
        super().mark_billing_event_processed(provider, provider_event_ref)

    def billing_event_processed(self, provider: str, provider_event_ref: str) -> bool:
        with self.lock:
            found = self.connection.execute(
                """
                SELECT 1 FROM processed_billing_events
                WHERE provider=? AND provider_event_ref=?
                """,
                (provider, provider_event_ref),
            ).fetchone()
            if found:
                self.processed_events.add((provider, provider_event_ref))
            return found is not None
