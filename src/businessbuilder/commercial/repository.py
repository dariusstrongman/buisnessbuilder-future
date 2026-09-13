from __future__ import annotations

from abc import ABC, abstractmethod
import sqlite3
from threading import RLock

from businessbuilder._serialization import decode_record, encode_record

from .models import (
    CancellationRecord,
    CheckoutIntent,
    EntitlementGrant,
    Order,
    OrderAuditEvent,
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
    def save_product(self, product: Product, version: ProductVersion) -> None: ...

    @abstractmethod
    def get_product_version(self, product_version_id: str) -> ProductVersion: ...

    @abstractmethod
    def append_order(self, order: Order) -> None: ...

    @abstractmethod
    def get_order(self, tenant_id: str, company_id: str, order_id: str) -> Order: ...

    @abstractmethod
    def order_history(self, tenant_id: str, company_id: str, order_id: str) -> tuple[Order, ...]: ...

    @abstractmethod
    def save_checkout(self, checkout: CheckoutIntent) -> None: ...

    @abstractmethod
    def get_checkout(self, tenant_id: str, company_id: str, checkout_id: str) -> CheckoutIntent: ...

    @abstractmethod
    def get_checkout_by_idempotency(self, tenant_id: str, company_id: str, key: str) -> CheckoutIntent | None: ...

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
        self.products: dict[str, Product] = {}
        self.product_versions: dict[str, ProductVersion] = {}
        self.orders: dict[tuple[str, str, str], list[Order]] = {}
        self.checkouts: dict[tuple[str, str, str], CheckoutIntent] = {}
        self.payments: dict[tuple[str, str, str], PaymentIntentRef] = {}
        self.refunds: list[RefundRecord] = []
        self.cancellations: list[CancellationRecord] = []
        self.subscriptions: dict[tuple[str, str, str], list[Subscription]] = {}
        self.entitlement_grants: dict[tuple[str, str, str], list[EntitlementGrant]] = {}
        self.audit: list[object] = []
        self.processed_events: set[tuple[str, str]] = set()

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

    def get_order(self, tenant_id: str, company_id: str, order_id: str) -> Order:
        history = self.orders.get((tenant_id, company_id, order_id))
        if not history:
            raise CommercialNotFound("order not found in scope")
        return history[-1]

    def order_history(self, tenant_id: str, company_id: str, order_id: str) -> tuple[Order, ...]:
        return tuple(self.orders.get((tenant_id, company_id, order_id), ()))

    def save_checkout(self, checkout: CheckoutIntent) -> None:
        with self.lock:
            existing = self.get_checkout_by_idempotency(checkout.tenant_id, checkout.company_id, checkout.idempotency_key)
            if existing and existing.checkout_intent_id != checkout.checkout_intent_id:
                raise CommercialConflict("checkout idempotency key already used")
            self.checkouts[(checkout.tenant_id, checkout.company_id, checkout.checkout_intent_id)] = checkout

    def get_checkout(self, tenant_id: str, company_id: str, checkout_id: str) -> CheckoutIntent:
        try:
            return self.checkouts[(tenant_id, company_id, checkout_id)]
        except KeyError as exc:
            raise CommercialNotFound("checkout not found in scope") from exc

    def get_checkout_by_idempotency(self, tenant_id: str, company_id: str, key: str) -> CheckoutIntent | None:
        return next((item for scope, item in self.checkouts.items() if scope[:2] == (tenant_id, company_id) and item.idempotency_key == key), None)

    def save_payment(self, payment: PaymentIntentRef) -> None:
        with self.lock:
            key = (payment.tenant_id, payment.company_id, payment.payment_ref_id)
            existing = self.payments.get(key)
            if existing and existing.order_id != payment.order_id:
                raise CommercialConflict("payment reference cannot move orders")
            self.payments[key] = payment

    def get_payment_for_order(self, tenant_id: str, company_id: str, order_id: str) -> PaymentIntentRef | None:
        return next((item for (t, c, _), item in self.payments.items() if (t, c, item.order_id) == (tenant_id, company_id, order_id)), None)

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

    def billing_event_processed(self, provider: str, provider_event_ref: str) -> bool:
        return (provider, provider_event_ref) in self.processed_events

    def mark_billing_event_processed(self, provider: str, provider_event_ref: str) -> None:
        with self.lock:
            self.processed_events.add((provider, provider_event_ref))


from .models import (
    Amount, BillingMode, BillingPeriod, CancellationPolicy, CancellationTiming,
    CheckoutStatus, Entitlement, EntitlementClass, EntitlementStatus, Feature,
    GracePeriod, OrderItem, OrderStatus, Package, ProductCode, RefundKind,
    RenewalState, SubscriptionPlanRef, SubscriptionStatus,
)

_COMMERCIAL_TYPES = {
    item.__name__: item
    for item in (
        Product, ProductVersion, Feature, Entitlement, Package, Order, OrderItem,
        CheckoutIntent, PaymentIntentRef, RefundRecord, CancellationRecord,
        Subscription, SubscriptionPlanRef, EntitlementGrant, OrderAuditEvent,
        SubscriptionAuditEvent, Amount, BillingPeriod, GracePeriod,
        CancellationPolicy, ProductCode, BillingMode, EntitlementClass,
        EntitlementStatus, OrderStatus, CheckoutStatus, SubscriptionStatus,
        RenewalState, RefundKind, CancellationTiming,
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
            """
        )
        self._load_sqlite()

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _scope(tenant_id: str, company_id: str, record_id: str) -> str:
        return f"{tenant_id}\x1f{company_id}\x1f{record_id}"

    def _insert(self, kind: str, key: str, version: int, value: object, tenant_id: str | None = None, company_id: str | None = None, *, replace_row: bool = False) -> None:
        verb = "INSERT OR REPLACE" if replace_row else "INSERT"
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
            elif kind == "checkout": self.checkouts[(value.tenant_id, value.company_id, value.checkout_intent_id)] = value
            elif kind == "payment": self.payments[(value.tenant_id, value.company_id, value.payment_ref_id)] = value
            elif kind == "refund": self.refunds.append(value)
            elif kind == "cancellation": self.cancellations.append(value)
            elif kind == "subscription": self.subscriptions.setdefault((value.tenant_id, value.company_id, value.subscription_id), []).append(value)
            elif kind == "entitlement": self.entitlement_grants.setdefault((value.tenant_id, value.company_id, value.grant_id), []).append(value)
        for (body,) in self.connection.execute("SELECT body FROM commercial_audit_events ORDER BY rowid"):
            self.audit.append(decode_record(body, _COMMERCIAL_TYPES))
        self.processed_events.update(self.connection.execute("SELECT provider, provider_event_ref FROM processed_billing_events"))

    def save_product(self, product: Product, version: ProductVersion) -> None:
        super().save_product(product, version)
        self._insert("product", product.product_code.value, 1, product, replace_row=True)
        self._insert("product_version", version.product_version_id, version.version, version, replace_row=True)

    def append_order(self, order: Order) -> None:
        super().append_order(order)
        self._insert("order", self._scope(order.tenant_id, order.company_id, order.order_id), order.version, order, order.tenant_id, order.company_id)

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
        super().mark_billing_event_processed(provider, provider_event_ref)
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO processed_billing_events VALUES (?, ?)",
                (provider, provider_event_ref),
            )
