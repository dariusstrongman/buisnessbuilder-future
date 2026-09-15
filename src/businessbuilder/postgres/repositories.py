from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
from threading import RLock
from typing import Any, Iterator, Sequence

import psycopg
from psycopg import sql

from businessbuilder._serialization import decode_record, encode_record
from businessbuilder.ai_workforce.models import (
    AuditRecord as WorkforceAuditRecord,
    BudgetCeiling as WorkforceBudgetCeiling,
    CapabilityGrant as WorkforceCapabilityGrant,
    EscalationRule as WorkforceEscalationRule,
    PolicyDecision as WorkforcePolicyDecision,
    PolicyEvaluation as WorkforcePolicyEvaluation,
    RoleDefinition as WorkforceRoleDefinition,
    RoleState as WorkforceRoleState,
)
from businessbuilder.ai_workforce.repository import InMemoryWorkforceRepository
from businessbuilder.commercial.models import (
    CancellationRecord,
    CheckoutIntent,
    CommercialQuote,
    CommercialOperatorGrant,
    CommercialAdmissionRecord,
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
from businessbuilder.commercial.repository import (
    CommercialConflict,
    CommercialNotFound,
    InMemoryCommercialRepository,
    _COMMERCIAL_TYPES,
)
from businessbuilder.company_brain.errors import ConflictError, NotFoundError
from businessbuilder.company_brain.model import (
    BrainRecord,
    Company,
    Dependency,
    EntityRef,
    InvalidationNotice,
    KnowledgeClass,
    LifecycleState,
    Provenance,
    RecordKind,
    Scope,
)
from businessbuilder.identity.models import (
    AccountRecoveryRequest,
    FounderProfile,
    IdentityAuditEvent,
    Membership,
    Organization,
    Session,
    SupportAccessGrant,
    SupportImpersonationSession,
    Tenant,
    User,
)
from businessbuilder.identity.repository import (
    InMemoryIdentityRepository,
    _IDENTITY_TYPES,
)
from businessbuilder.runtime.models import ApprovalRecord, Budget, Event, Job
from businessbuilder.runtime.storage import RuntimeRepository, SQLiteRuntimeRepository, decode, encode
from businessbuilder.verification.models import VerificationRecord
from businessbuilder.verification.repository import VerificationRepository

from .connection import connect_postgres
from .migrations import migrate


class _PostgresRepository:
    def __init__(
        self,
        dsn: str | None = None,
        *,
        schema: str | None = None,
        application_name: str,
    ) -> None:
        self.connection = connect_postgres(
            dsn, schema=schema, application_name=application_name
        )
        migrate(self.connection)

    def close(self) -> None:
        self.connection.close()


class PostgresIdentityRepository(InMemoryIdentityRepository, _PostgresRepository):
    """Worker 10 identity port backed by PostgreSQL JSON records."""

    def __init__(self, dsn: str | None = None, *, schema: str | None = None) -> None:
        InMemoryIdentityRepository.__init__(self)
        _PostgresRepository.__init__(
            self, dsn, schema=schema, application_name="businessbuilder-identity"
        )
        self._load_postgres()

    def _load_postgres(self) -> None:
        targets = {
            "user": self.users,
            "founder_profile": self.founder_profiles,
            "tenant": self.tenants,
            "organization": self.organizations,
            "membership": self.memberships,
            "session": self.sessions,
            "recovery": self.recoveries,
            "support_grant": self.support_grants,
            "support_session": self.support_sessions,
        }
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT kind, record_key, body FROM bb_identity_records ORDER BY kind, record_key"
            )
            for row in cursor:
                targets[row["kind"]][row["record_key"]] = decode_record(
                    row["body"], _IDENTITY_TYPES
                )
            cursor.execute(
                "SELECT body FROM bb_identity_audit_events ORDER BY sequence"
            )
            for row in cursor:
                self.audit_events.append(decode_record(row["body"], _IDENTITY_TYPES))

    def _put(
        self, kind: str, key: str, tenant_id: str | None, value: object
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_identity_records(kind, record_key, tenant_id, body)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (kind, record_key) DO UPDATE
                SET tenant_id=EXCLUDED.tenant_id, body=EXCLUDED.body
                """,
                (kind, key, tenant_id, encode_record(value)),
            )

    def add_user(self, user: User) -> None:
        super().add_user(user)
        self._put("user", user.user_id, None, user)

    def save_user(self, user: User) -> None:
        super().save_user(user)
        self._put("user", user.user_id, None, user)

    def add_founder_profile(self, profile: FounderProfile) -> None:
        super().add_founder_profile(profile)
        self._put("founder_profile", profile.founder_profile_id, None, profile)

    def add_tenant(self, tenant: Tenant) -> None:
        super().add_tenant(tenant)
        self._put("tenant", tenant.tenant_id, tenant.tenant_id, tenant)

    def save_tenant(self, tenant: Tenant) -> None:
        super().save_tenant(tenant)
        self._put("tenant", tenant.tenant_id, tenant.tenant_id, tenant)

    def add_organization(self, organization: Organization) -> None:
        super().add_organization(organization)
        self._put(
            "organization",
            organization.organization_id,
            organization.tenant_id,
            organization,
        )

    def save_organization(self, organization: Organization) -> None:
        super().save_organization(organization)
        self._put(
            "organization",
            organization.organization_id,
            organization.tenant_id,
            organization,
        )

    def add_membership(self, membership: Membership) -> None:
        super().add_membership(membership)
        self._put(
            "membership", membership.membership_id, membership.tenant_id, membership
        )

    def save_membership(self, membership: Membership) -> None:
        super().save_membership(membership)
        self._put(
            "membership", membership.membership_id, membership.tenant_id, membership
        )

    def save_session(self, session: Session) -> None:
        super().save_session(session)
        self._put("session", session.session_id, None, session)

    def save_recovery(self, request: AccountRecoveryRequest) -> None:
        super().save_recovery(request)
        self._put("recovery", request.recovery_id, None, request)

    def save_support_grant(self, grant: SupportAccessGrant) -> None:
        super().save_support_grant(grant)
        self._put("support_grant", grant.grant_id, grant.tenant_id, grant)

    def save_support_session(self, session: SupportImpersonationSession) -> None:
        super().save_support_session(session)
        self._put(
            "support_session",
            session.impersonation_session_id,
            session.tenant_id,
            session,
        )

    def append_audit(self, event: IdentityAuditEvent) -> None:
        super().append_audit(event)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_identity_audit_events(audit_event_id, tenant_id, body)
                VALUES (%s, %s, %s)
                """,
                (event.audit_event_id, event.tenant_id, encode_record(event)),
            )


class PostgresCommercialRepository(InMemoryCommercialRepository, _PostgresRepository):
    """Worker 10 commercial ledger backed by append-versioned PostgreSQL rows."""

    def __init__(self, dsn: str | None = None, *, schema: str | None = None) -> None:
        InMemoryCommercialRepository.__init__(self)
        _PostgresRepository.__init__(
            self, dsn, schema=schema, application_name="businessbuilder-commercial"
        )
        self._load_postgres()

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
                with self.connection.transaction():
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
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO bb_processed_billing_events(provider, provider_event_ref)
                    VALUES (%s, %s) ON CONFLICT DO NOTHING
                    RETURNING provider
                    """,
                    (provider, provider_event_ref),
                )
                claimed = cursor.fetchone() is not None
            if not claimed:
                self.processed_events.add((provider, provider_event_ref))
                yield False
                return
            yield True
            self.processed_events.add((provider, provider_event_ref))

    @staticmethod
    def _scope(tenant_id: str, company_id: str, record_id: str) -> str:
        return f"{tenant_id}\x1f{company_id}\x1f{record_id}"

    def _load_postgres(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT kind, scope_key, version, body FROM bb_commercial_records ORDER BY sequence"
            )
            for row in cursor:
                value = decode_record(row["body"], _COMMERCIAL_TYPES)
                kind = row["kind"]
                key = row["scope_key"]
                if kind == "product":
                    self.products[key] = value
                elif kind == "product_version":
                    self.product_versions[key] = value
                elif kind == "order":
                    self.orders.setdefault(
                        (value.tenant_id, value.company_id, value.order_id), []
                    ).append(value)
                elif kind == "quote":
                    self.quotes.setdefault(
                        (value.tenant_id, value.company_id, value.quote_id), []
                    ).append(value)
                elif kind == "operator_grant":
                    self.operator_grants.setdefault(
                        (value.tenant_id, value.company_id, value.grant_id), []
                    ).append(value)
                elif kind == "admission":
                    self.admissions[(value.tenant_id, value.company_id, value.admission_id)] = value
                elif kind == "checkout":
                    self.checkouts[
                        (value.tenant_id, value.company_id, value.checkout_intent_id)
                    ] = value
                elif kind == "payment":
                    self.payments[
                        (value.tenant_id, value.company_id, value.payment_ref_id)
                    ] = value
                elif kind == "refund":
                    self.refunds.append(value)
                elif kind == "cancellation":
                    self.cancellations.append(value)
                elif kind == "subscription":
                    self.subscriptions.setdefault(
                        (value.tenant_id, value.company_id, value.subscription_id), []
                    ).append(value)
                elif kind == "entitlement":
                    self.entitlement_grants.setdefault(
                        (value.tenant_id, value.company_id, value.grant_id), []
                    ).append(value)
            cursor.execute(
                "SELECT body FROM bb_commercial_audit_events ORDER BY sequence"
            )
            for row in cursor:
                self.audit.append(decode_record(row["body"], _COMMERCIAL_TYPES))
            cursor.execute(
                "SELECT provider, provider_event_ref FROM bb_processed_billing_events"
            )
            self.processed_events.update(
                (row["provider"], row["provider_event_ref"]) for row in cursor
            )

    def _insert(
        self,
        kind: str,
        key: str,
        version: int,
        value: object,
        tenant_id: str | None = None,
        company_id: str | None = None,
        *,
        replace_row: bool = False,
    ) -> None:
        conflict = (
            sql.SQL(
                "DO UPDATE SET tenant_id=EXCLUDED.tenant_id, "
                "company_id=EXCLUDED.company_id, body=EXCLUDED.body"
            )
            if replace_row
            else sql.SQL("DO NOTHING")
        )
        returning = sql.SQL("") if replace_row else sql.SQL("RETURNING version")
        with self.connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                INSERT INTO bb_commercial_records(
                    kind, scope_key, version, tenant_id, company_id, body
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (kind, scope_key, version) {}
                {}
                """
                ).format(conflict, returning),
                (kind, key, version, tenant_id, company_id, encode_record(value)),
            )
            if not replace_row and cursor.fetchone() is None:
                raise CommercialConflict(
                    f"concurrent {kind} version conflict"
                )

    def save_product(self, product: Product, version: ProductVersion) -> None:
        super().save_product(product, version)
        self._insert("product", product.product_code.value, 1, product, replace_row=True)
        self._insert(
            "product_version",
            version.product_version_id,
            version.version,
            version,
            replace_row=True,
        )

    def append_order(self, order: Order) -> None:
        super().append_order(order)
        self._insert(
            "order",
            self._scope(order.tenant_id, order.company_id, order.order_id),
            order.version,
            order,
            order.tenant_id,
            order.company_id,
        )

    def append_quote(self, quote: CommercialQuote) -> None:
        super().append_quote(quote)
        self._insert(
            "quote",
            self._scope(quote.tenant_id, quote.company_id, quote.quote_id),
            quote.version,
            quote,
            quote.tenant_id,
            quote.company_id,
        )

    def append_operator_grant(self, grant: CommercialOperatorGrant) -> None:
        super().append_operator_grant(grant)
        self._insert(
            "operator_grant", self._scope(grant.tenant_id, grant.company_id, grant.grant_id),
            grant.version, grant, grant.tenant_id, grant.company_id,
        )

    def append_admission(self, admission: CommercialAdmissionRecord) -> None:
        super().append_admission(admission)
        self._insert(
            "admission", self._scope(admission.tenant_id, admission.company_id, admission.admission_id),
            1, admission, admission.tenant_id, admission.company_id,
        )

    def save_checkout(self, checkout: CheckoutIntent) -> None:
        with self.transaction():
            super().save_checkout(checkout)
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO bb_commercial_records(
                        kind, scope_key, version, tenant_id, company_id, body,
                        checkout_idempotency_key, checkout_provider_ref
                    ) VALUES ('checkout', %s, 1, %s, %s, %s, %s, %s)
                    ON CONFLICT (kind, scope_key, version) DO UPDATE SET
                        body=EXCLUDED.body,
                        checkout_provider_ref=EXCLUDED.checkout_provider_ref
                    WHERE bb_commercial_records.checkout_idempotency_key=EXCLUDED.checkout_idempotency_key
                      AND (bb_commercial_records.checkout_provider_ref IS NULL
                           OR bb_commercial_records.checkout_provider_ref=EXCLUDED.checkout_provider_ref)
                    RETURNING scope_key
                    """,
                    (
                        self._scope(checkout.tenant_id, checkout.company_id, checkout.checkout_intent_id),
                        checkout.tenant_id, checkout.company_id, encode_record(checkout),
                        checkout.idempotency_key, checkout.provider_ref,
                    ),
                )
                if cursor.fetchone() is None:
                    raise CommercialConflict("provider checkout reference cannot change")

    def get_checkout_by_idempotency(self, tenant_id: str, company_id: str, key: str) -> CheckoutIntent | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT body FROM bb_commercial_records WHERE kind='checkout'
                   AND tenant_id=%s AND company_id=%s AND checkout_idempotency_key=%s""",
                (tenant_id, company_id, key),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        checkout = decode_record(row["body"], _COMMERCIAL_TYPES)
        self.checkouts[(checkout.tenant_id, checkout.company_id, checkout.checkout_intent_id)] = checkout
        return checkout

    def get_checkout_by_provider_ref(self, provider_ref: str) -> CheckoutIntent:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT body FROM bb_commercial_records WHERE kind='checkout' AND checkout_provider_ref=%s",
                (provider_ref,),
            )
            row = cursor.fetchone()
        if row is None:
            raise CommercialNotFound("provider checkout not mapped")
        checkout = decode_record(row["body"], _COMMERCIAL_TYPES)
        self.checkouts[(checkout.tenant_id, checkout.company_id, checkout.checkout_intent_id)] = checkout
        return checkout

    def save_payment(self, payment: PaymentIntentRef) -> None:
        super().save_payment(payment)
        self._insert(
            "payment",
            self._scope(
                payment.tenant_id, payment.company_id, payment.payment_ref_id
            ),
            1,
            payment,
            payment.tenant_id,
            payment.company_id,
            replace_row=True,
        )

    def append_refund(self, refund: RefundRecord) -> None:
        if any(item.provider_ref == refund.provider_ref for item in self.refunds):
            return
        super().append_refund(refund)
        self._insert(
            "refund",
            self._scope(refund.tenant_id, refund.company_id, refund.refund_id),
            1,
            refund,
            refund.tenant_id,
            refund.company_id,
        )

    def append_cancellation(self, record: CancellationRecord) -> None:
        if any(item.cancellation_id == record.cancellation_id for item in self.cancellations):
            return
        super().append_cancellation(record)
        self._insert(
            "cancellation",
            self._scope(
                record.tenant_id, record.company_id, record.cancellation_id
            ),
            1,
            record,
            record.tenant_id,
            record.company_id,
        )

    def append_subscription(self, subscription: Subscription) -> None:
        super().append_subscription(subscription)
        self._insert(
            "subscription",
            self._scope(
                subscription.tenant_id,
                subscription.company_id,
                subscription.subscription_id,
            ),
            subscription.version,
            subscription,
            subscription.tenant_id,
            subscription.company_id,
        )

    def append_entitlement_grant(self, grant: EntitlementGrant) -> None:
        super().append_entitlement_grant(grant)
        self._insert(
            "entitlement",
            self._scope(grant.tenant_id, grant.company_id, grant.grant_id),
            grant.version,
            grant,
            grant.tenant_id,
            grant.company_id,
        )

    def _append_audit_row(self, event: object) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_commercial_audit_events(
                    audit_event_id, tenant_id, company_id, body
                ) VALUES (%s, %s, %s, %s)
                """,
                (
                    event.audit_event_id,
                    event.tenant_id,
                    event.company_id,
                    encode_record(event),
                ),
            )

    def append_order_audit(self, event: OrderAuditEvent) -> None:
        super().append_order_audit(event)
        self._append_audit_row(event)

    def append_subscription_audit(self, event: SubscriptionAuditEvent) -> None:
        super().append_subscription_audit(event)
        self._append_audit_row(event)

    def mark_billing_event_processed(
        self, provider: str, provider_event_ref: str
    ) -> None:
        super().mark_billing_event_processed(provider, provider_event_ref)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_processed_billing_events(provider, provider_event_ref)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
                """,
                (provider, provider_event_ref),
            )

    @staticmethod
    def _outbox_from_row(row: dict[str, Any]) -> OutboxMessage:
        return OutboxMessage(
            outbox_id=row["outbox_id"],
            idempotency_key=row["idempotency_key"],
            event=decode_record(row["body"], _COMMERCIAL_TYPES),
            status=OutboxStatus(row["state"]),
            attempts=row["attempts"],
            available_at=row["available_at"],
            created_at=row["created_at"],
            claimed_by=row["claimed_by"],
            claimed_until=row["claimed_until"],
            acknowledged_at=row["acknowledged_at"],
            last_error=row["last_error"],
        )

    def enqueue_outbox(
        self, event: CommercialEvent, idempotency_key: str
    ) -> OutboxMessage:
        if not idempotency_key:
            raise ValueError("outbox idempotency_key is required")
        outbox_id = "outbox_" + sha256(idempotency_key.encode()).hexdigest()[:24]
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_commercial_outbox(
                    outbox_id, idempotency_key, tenant_id, company_id,
                    event_type, body, state, attempts, available_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'pending', 0, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *
                """,
                (
                    outbox_id,
                    idempotency_key,
                    event.tenant_id,
                    event.company_id,
                    event.event_type,
                    encode_record(event),
                    event.occurred_at,
                    event.occurred_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT * FROM bb_commercial_outbox
                    WHERE idempotency_key=%s
                    """,
                    (idempotency_key,),
                )
                row = cursor.fetchone()
        if row is None:
            raise CommercialConflict("outbox enqueue did not persist")
        message = self._outbox_from_row(row)
        if message.event != event:
            raise CommercialConflict(
                "outbox idempotency key cannot identify different events"
            )
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
        with self.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH candidates AS (
                        SELECT outbox_id
                        FROM bb_commercial_outbox
                        WHERE (
                            state='pending' AND available_at <= %s
                        ) OR (
                            state='dispatching' AND claimed_until <= %s
                        )
                        ORDER BY sequence
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE bb_commercial_outbox AS message
                    SET state='dispatching',
                        attempts=message.attempts + 1,
                        claimed_by=%s,
                        claimed_until=%s,
                        last_error=NULL
                    FROM candidates
                    WHERE message.outbox_id=candidates.outbox_id
                    RETURNING message.*
                    """,
                    (at, at, limit, dispatcher_id, at + lease),
                )
                return tuple(self._outbox_from_row(row) for row in cursor)

    def acknowledge_outbox(
        self, outbox_id: str, dispatcher_id: str, *, at: datetime
    ) -> OutboxMessage:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE bb_commercial_outbox
                SET state='acknowledged', claimed_by=NULL, claimed_until=NULL,
                    acknowledged_at=%s
                WHERE outbox_id=%s AND state='dispatching' AND claimed_by=%s
                RETURNING *
                """,
                (at, outbox_id, dispatcher_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise CommercialConflict("outbox acknowledgment claim mismatch")
        return self._outbox_from_row(row)

    def release_outbox(
        self,
        outbox_id: str,
        dispatcher_id: str,
        *,
        retry_at: datetime,
        error: str,
    ) -> OutboxMessage:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE bb_commercial_outbox
                SET state='pending', available_at=%s, claimed_by=NULL,
                    claimed_until=NULL, last_error=%s
                WHERE outbox_id=%s AND state='dispatching' AND claimed_by=%s
                RETURNING *
                """,
                (retry_at, error[:200], outbox_id, dispatcher_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise CommercialConflict("outbox release claim mismatch")
        return self._outbox_from_row(row)

    def list_outbox(self) -> tuple[OutboxMessage, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT * FROM bb_commercial_outbox ORDER BY sequence")
            return tuple(self._outbox_from_row(row) for row in cursor)


class PostgresRuntimeRepository(RuntimeRepository, _PostgresRepository):
    """Runtime event, job, approval, budget, and audit persistence in PostgreSQL."""

    def __init__(self, dsn: str | None = None, *, schema: str | None = None) -> None:
        self._lock = RLock()
        self._transaction_depth = 0
        _PostgresRepository.__init__(
            self, dsn, schema=schema, application_name="businessbuilder-runtime"
        )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            if self._transaction_depth:
                self._transaction_depth += 1
                try:
                    yield
                finally:
                    self._transaction_depth -= 1
                return
            self._transaction_depth = 1
            try:
                with self.connection.transaction():
                    yield
            finally:
                self._transaction_depth = 0

    def append_event(self, event: Event) -> bool:
        with self._lock, self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_runtime_events(
                    event_id, tenant_id, company_id, event_type, body, recorded_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING RETURNING event_id
                """,
                (
                    event.event_id,
                    event.tenant_id,
                    event.company_id,
                    event.type,
                    encode(event),
                    event.recorded_at or event.occurred_at,
                ),
            )
            return cursor.fetchone() is not None

    def list_events(self, tenant_id: str, company_id: str) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT body FROM bb_runtime_events
                WHERE tenant_id=%s AND company_id=%s ORDER BY sequence
                """,
                (tenant_id, company_id),
            )
            return [decode(row["body"]) for row in cursor]

    def save_job(self, job: Job) -> None:
        with self._lock, self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_runtime_jobs(
                    job_id, tenant_id, company_id, idempotency_key, status, body
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO UPDATE
                SET status=EXCLUDED.status, body=EXCLUDED.body
                """,
                (
                    job.job_id,
                    job.tenant_id,
                    job.company_id,
                    job.idempotency_key,
                    job.status.value,
                    encode(job),
                ),
            )

    @staticmethod
    def _job_from_dict(data: dict[str, Any]) -> Job:
        return SQLiteRuntimeRepository._job_from_dict(None, data)

    def get_job(self, tenant_id: str, company_id: str, job_id: str) -> Job | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT body FROM bb_runtime_jobs
                WHERE tenant_id=%s AND company_id=%s AND job_id=%s
                """,
                (tenant_id, company_id, job_id),
            )
            row = cursor.fetchone()
            return self._job_from_dict(decode(row["body"])) if row else None

    def get_job_by_idempotency(
        self, tenant_id: str, company_id: str, key: str
    ) -> Job | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT body FROM bb_runtime_jobs
                WHERE tenant_id=%s AND company_id=%s AND idempotency_key=%s
                """,
                (tenant_id, company_id, key),
            )
            row = cursor.fetchone()
            return self._job_from_dict(decode(row["body"])) if row else None

    def list_jobs(self, tenant_id: str, company_id: str) -> tuple[Job, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT body FROM bb_runtime_jobs WHERE tenant_id=%s AND company_id=%s ORDER BY job_id",
                (tenant_id, company_id),
            )
            return tuple(self._job_from_dict(decode(row["body"])) for row in cursor)

    def save_approval(self, approval: ApprovalRecord) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_runtime_approvals(
                    approval_id, tenant_id, company_id, job_id, body
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (approval_id) DO UPDATE SET body=EXCLUDED.body
                """,
                (
                    approval.approval_id,
                    approval.tenant_id,
                    approval.company_id,
                    approval.job_id,
                    encode(approval),
                ),
            )

    def get_approval(
        self, tenant_id: str, company_id: str, approval_id: str
    ) -> ApprovalRecord | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT body FROM bb_runtime_approvals
                WHERE tenant_id=%s AND company_id=%s AND approval_id=%s
                """,
                (tenant_id, company_id, approval_id),
            )
            row = cursor.fetchone()
        if not row:
            return None
        from businessbuilder.runtime.models import ApprovalMode, ApprovalState

        data = decode(row["body"])
        data["mode"] = ApprovalMode(data["mode"])
        data["state"] = ApprovalState(data["state"])
        return ApprovalRecord(**data)

    def list_approvals(
        self, tenant_id: str, company_id: str
    ) -> tuple[ApprovalRecord, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT body FROM bb_runtime_approvals WHERE tenant_id=%s AND company_id=%s ORDER BY approval_id",
                (tenant_id, company_id),
            )
            rows = tuple(cursor)
        from businessbuilder.runtime.models import ApprovalMode, ApprovalState
        values: list[ApprovalRecord] = []
        for row in rows:
            data = decode(row["body"])
            data["mode"] = ApprovalMode(data["mode"])
            data["state"] = ApprovalState(data["state"])
            values.append(ApprovalRecord(**data))
        return tuple(values)

    def save_budget(self, budget: Budget) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_runtime_budgets(budget_id, tenant_id, company_id, body)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (budget_id) DO UPDATE SET body=EXCLUDED.body
                """,
                (budget.budget_id, budget.tenant_id, budget.company_id, encode(budget)),
            )

    def get_budget(
        self, tenant_id: str, company_id: str, budget_id: str
    ) -> Budget | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT body FROM bb_runtime_budgets
                WHERE tenant_id=%s AND company_id=%s AND budget_id=%s
                """,
                (tenant_id, company_id, budget_id),
            )
            row = cursor.fetchone()
        if not row:
            return None
        from businessbuilder.runtime.models import Money

        data = decode(row["body"])
        data["ceiling"] = Money(**data["ceiling"])
        return Budget(**data)

    def list_budgets(self, tenant_id: str, company_id: str) -> tuple[Budget, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT body FROM bb_runtime_budgets WHERE tenant_id=%s AND company_id=%s ORDER BY budget_id",
                (tenant_id, company_id),
            )
            rows = tuple(cursor)
        from businessbuilder.runtime.models import Money
        values: list[Budget] = []
        for row in rows:
            data = decode(row["body"])
            data["ceiling"] = Money(**data["ceiling"])
            values.append(Budget(**data))
        return tuple(values)

    def save_reservation(
        self,
        job: Job,
        budget_id: str,
        currency: str,
        reserved_minor: int,
        settled_minor: int,
        state: str,
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_runtime_reservations(
                    job_id, tenant_id, company_id, budget_id, currency,
                    reserved_minor, settled_minor, state
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO UPDATE SET
                    reserved_minor=EXCLUDED.reserved_minor,
                    settled_minor=EXCLUDED.settled_minor,
                    state=EXCLUDED.state
                """,
                (
                    job.job_id,
                    job.tenant_id,
                    job.company_id,
                    budget_id,
                    currency,
                    reserved_minor,
                    settled_minor,
                    state,
                ),
            )

    def begin_invocation(
        self, tenant_id: str, company_id: str, key: str, job_id: str
    ) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_runtime_capability_invocations(
                    tenant_id, company_id, idempotency_key, job_id, state, result
                ) VALUES (%s, %s, %s, %s, 'started', NULL)
                ON CONFLICT DO NOTHING RETURNING idempotency_key
                """,
                (tenant_id, company_id, key, job_id),
            )
            return cursor.fetchone() is not None

    def complete_invocation(
        self, tenant_id: str, company_id: str, key: str, result: Any
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE bb_runtime_capability_invocations
                SET state='completed', result=%s
                WHERE tenant_id=%s AND company_id=%s AND idempotency_key=%s
                """,
                (encode(result), tenant_id, company_id, key),
            )

    def invocation(
        self, tenant_id: str, company_id: str, key: str
    ) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT tenant_id, company_id, idempotency_key, job_id, state, result
                FROM bb_runtime_capability_invocations
                WHERE tenant_id=%s AND company_id=%s AND idempotency_key=%s
                """,
                (tenant_id, company_id, key),
            )
            return cursor.fetchone()

    def append_audit(self, record: dict[str, Any]) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_runtime_audit_events(
                    audit_event_id, tenant_id, company_id, occurred_at, body
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    record["audit_event_id"],
                    record["tenant_id"],
                    record["company_id"],
                    record["occurred_at"],
                    encode(record),
                ),
            )

    def list_audit(self, tenant_id: str, company_id: str) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT body FROM bb_runtime_audit_events
                WHERE tenant_id=%s AND company_id=%s
                ORDER BY occurred_at, audit_event_id
                """,
                (tenant_id, company_id),
            )
            return [decode(row["body"]) for row in cursor]

    @staticmethod
    def _agent_envelope(value: str):
        from businessbuilder.agent_runtime.models import AgentJobEnvelope
        return AgentJobEnvelope.from_payload(decode(value))

    @staticmethod
    def _agent_execution(value: str):
        from businessbuilder.agent_runtime.models import ExecutionRecord, ExecutionState
        from businessbuilder.runtime.models import ArtifactRef, Money
        data = decode(value)
        data["state"] = ExecutionState(data["state"])
        data["actual_cost"] = Money(**data["actual_cost"]) if data.get("actual_cost") else None
        data["artifact_refs"] = tuple(ArtifactRef(**item) for item in data.get("artifact_refs", ()))
        data["emitted_event_ids"] = tuple(data.get("emitted_event_ids", ()))
        return ExecutionRecord(**data)

    @staticmethod
    def _agent_outbox(row):
        from businessbuilder.agent_runtime.models import DeliveryState, QueueOutboxRecord
        return QueueOutboxRecord(
            row["message_id"], row["idempotency_key"], row["tenant_id"],
            row["company_id"], row["job_id"], row["envelope_digest"],
            DeliveryState(row["state"]), row["attempts"], row["available_at"],
            row["created_at"], row["claimed_by"], row["claimed_until"],
            row["acknowledged_at"], row["last_error"],
        )

    def save_agent_admission(self, envelope, execution, outbox) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO bb_runtime_agent_job_envelopes(
                job_id, tenant_id, company_id, envelope_digest, body
                ) VALUES (%s, %s, %s, %s, %s)""",
                (envelope.job_id, envelope.tenant_id, envelope.company_id, envelope.envelope_digest, encode(envelope.to_payload())),
            )
            cursor.execute(
                """INSERT INTO bb_runtime_agent_executions(
                job_id, tenant_id, company_id, state, lease_owner, lease_until, body
                ) VALUES (%s, %s, %s, %s, NULL, NULL, %s)""",
                (execution.job_id, execution.tenant_id, execution.company_id, execution.state.value, encode(execution)),
            )
            cursor.execute(
                """INSERT INTO bb_runtime_agent_queue_outbox(
                message_id, idempotency_key, tenant_id, company_id, job_id,
                envelope_digest, state, attempts, available_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (outbox.message_id, outbox.idempotency_key, outbox.tenant_id, outbox.company_id, outbox.job_id, outbox.envelope_digest, outbox.state.value, outbox.attempts, outbox.available_at, outbox.created_at),
            )

    def get_agent_envelope(self, tenant_id: str, company_id: str, job_id: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT body FROM bb_runtime_agent_job_envelopes WHERE tenant_id=%s AND company_id=%s AND job_id=%s",
                (tenant_id, company_id, job_id),
            )
            row = cursor.fetchone()
        return self._agent_envelope(row["body"]) if row else None

    def get_agent_execution(self, tenant_id: str, company_id: str, job_id: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT body FROM bb_runtime_agent_executions WHERE tenant_id=%s AND company_id=%s AND job_id=%s",
                (tenant_id, company_id, job_id),
            )
            row = cursor.fetchone()
        return self._agent_execution(row["body"]) if row else None

    def list_agent_executions(self, tenant_id: str, company_id: str) -> tuple[Any, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT body FROM bb_runtime_agent_executions WHERE tenant_id=%s AND company_id=%s ORDER BY job_id",
                (tenant_id, company_id),
            )
            return tuple(self._agent_execution(row["body"]) for row in cursor)

    def save_agent_execution(self, execution) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """UPDATE bb_runtime_agent_executions
                SET state=%s, lease_owner=%s, lease_until=%s, body=%s
                WHERE tenant_id=%s AND company_id=%s AND job_id=%s""",
                (execution.state.value, execution.lease_owner, execution.lease_until, encode(execution), execution.tenant_id, execution.company_id, execution.job_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("agent execution not found in tenant/company scope")

    def lease_agent_execution(self, tenant_id, company_id, job_id, envelope_digest, worker_id, *, at, lease):
        from businessbuilder.agent_runtime.models import ExecutionState
        with self.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """SELECT body FROM bb_runtime_agent_executions
                    WHERE tenant_id=%s AND company_id=%s AND job_id=%s FOR UPDATE""",
                    (tenant_id, company_id, job_id),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            current = self._agent_execution(row["body"])
            if current.envelope_digest != envelope_digest:
                return None
            if current.state in {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED}:
                return None
            if current.state is ExecutionState.LEASED and current.lease_until and current.lease_until > at:
                return None
            changed = replace(
                current, state=ExecutionState.LEASED, attempts=current.attempts + 1,
                updated_at=at, lease_owner=worker_id, lease_until=at + lease,
            )
            self.save_agent_execution(changed)
            return changed

    def claim_agent_outbox(self, dispatcher_id, *, at, lease, limit):
        if not dispatcher_id or limit < 1 or lease <= timedelta(0):
            raise ValueError("dispatcher, positive lease, and positive limit required")
        with self.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """WITH candidates AS (
                        SELECT message_id FROM bb_runtime_agent_queue_outbox
                        WHERE (state='pending' AND available_at<=%s)
                           OR (state='dispatching' AND claimed_until<=%s)
                        ORDER BY sequence FOR UPDATE SKIP LOCKED LIMIT %s
                    )
                    UPDATE bb_runtime_agent_queue_outbox outbox
                    SET state='dispatching', attempts=outbox.attempts+1,
                        claimed_by=%s, claimed_until=%s
                    FROM candidates WHERE outbox.message_id=candidates.message_id
                    RETURNING outbox.*""",
                    (at, at, limit, dispatcher_id, at + lease),
                )
                return tuple(self._agent_outbox(row) for row in cursor)

    def acknowledge_agent_outbox(self, message_id, dispatcher_id, *, at):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """UPDATE bb_runtime_agent_queue_outbox SET state='acknowledged',
                acknowledged_at=%s, claimed_by=NULL, claimed_until=NULL
                WHERE message_id=%s AND state='dispatching' AND claimed_by=%s""",
                (at, message_id, dispatcher_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("outbox acknowledgment claim mismatch")

    def release_agent_outbox(self, message_id, dispatcher_id, *, retry_at, error):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """UPDATE bb_runtime_agent_queue_outbox SET state='pending',
                available_at=%s, claimed_by=NULL, claimed_until=NULL, last_error=%s
                WHERE message_id=%s AND state='dispatching' AND claimed_by=%s""",
                (retry_at, error[:200], message_id, dispatcher_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("outbox release claim mismatch")

    @staticmethod
    def _runtime_schedule(value: str):
        from businessbuilder.agent_runtime.models import RuntimeSchedule
        return RuntimeSchedule(**decode(value))

    def save_runtime_schedule(self, schedule) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO bb_runtime_schedules(
                schedule_id, tenant_id, company_id, next_due_at, enabled, body
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(schedule_id) DO UPDATE SET next_due_at=EXCLUDED.next_due_at,
                enabled=EXCLUDED.enabled, body=EXCLUDED.body""",
                (schedule.schedule_id, schedule.tenant_id, schedule.company_id, schedule.next_due_at, schedule.enabled, encode(schedule)),
            )

    def get_runtime_schedule(self, tenant_id: str, company_id: str, schedule_id: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT body FROM bb_runtime_schedules WHERE tenant_id=%s AND company_id=%s AND schedule_id=%s",
                (tenant_id, company_id, schedule_id),
            )
            row = cursor.fetchone()
        return self._runtime_schedule(row["body"]) if row else None

    def list_due_runtime_schedules(self, *, at: datetime, limit: int) -> tuple[Any, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT body FROM bb_runtime_schedules
                WHERE enabled AND next_due_at<=%s ORDER BY next_due_at, schedule_id LIMIT %s""",
                (at, limit),
            )
            return tuple(self._runtime_schedule(row["body"]) for row in cursor)

    @staticmethod
    def _broker_record(kind: str, body: str):
        from businessbuilder.access_broker.serialization import decode_broker_record
        return decode_broker_record(kind, decode(body))

    def save_broker_record(self, kind: str, record_id: str, tenant_id: str, company_id: str, record: Any) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT tenant_id, company_id FROM bb_broker_records WHERE kind=%s AND record_id=%s",
                (kind, record_id),
            )
            existing = cursor.fetchone()
            if existing and (existing["tenant_id"], existing["company_id"]) != (tenant_id, company_id):
                raise PermissionError("broker record identifier is already owned by another scope")
            cursor.execute(
                """INSERT INTO bb_broker_records(kind, record_id, tenant_id, company_id, body)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(kind, record_id) DO UPDATE SET body=EXCLUDED.body
                WHERE bb_broker_records.tenant_id=EXCLUDED.tenant_id
                  AND bb_broker_records.company_id=EXCLUDED.company_id""",
                (kind, record_id, tenant_id, company_id, encode(record)),
            )

    def get_broker_record(self, kind: str, tenant_id: str, company_id: str, record_id: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT body FROM bb_broker_records WHERE kind=%s AND tenant_id=%s
                AND company_id=%s AND record_id=%s""",
                (kind, tenant_id, company_id, record_id),
            )
            row = cursor.fetchone()
        return self._broker_record(kind, row["body"]) if row else None

    def list_broker_records(self, kind: str, tenant_id: str, company_id: str) -> tuple[Any, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT body FROM bb_broker_records WHERE kind=%s AND tenant_id=%s
                AND company_id=%s ORDER BY record_id""",
                (kind, tenant_id, company_id),
            )
            return tuple(self._broker_record(kind, row["body"]) for row in cursor)

    def get_broker_record_by_id(self, kind: str, record_id: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT body FROM bb_broker_records WHERE kind=%s AND record_id=%s",
                (kind, record_id),
            )
            row = cursor.fetchone()
        return self._broker_record(kind, row["body"]) if row else None

    @staticmethod
    def _provider_receipt(body: str):
        from businessbuilder.access_broker.serialization import decode_provider_receipt
        return decode_provider_receipt(decode(body))

    def claim_provider_receipt(self, receipt: Any) -> tuple[Any, bool]:
        from businessbuilder.access_broker.models import ReceiptStatus
        with self.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO bb_provider_receipts(
                    receipt_id, tenant_id, company_id, job_id, capability, provider,
                    operation, idempotency_key, status, body
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING RETURNING receipt_id""",
                    (receipt.receipt_id, receipt.tenant_id, receipt.company_id, receipt.job_id,
                     receipt.capability, receipt.provider, receipt.operation,
                     receipt.idempotency_key, receipt.status.value, encode(receipt)),
                )
                inserted = cursor.fetchone() is not None
                cursor.execute(
                    """SELECT receipt_id, body FROM bb_provider_receipts WHERE tenant_id=%s
                    AND company_id=%s AND provider=%s AND operation=%s AND idempotency_key=%s
                    FOR UPDATE""",
                    (receipt.tenant_id, receipt.company_id, receipt.provider,
                     receipt.operation, receipt.idempotency_key),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("provider receipt claim was not persisted")
                current = self._provider_receipt(row["body"])
                if inserted:
                    return current, True
                if current.status is ReceiptStatus.FAILED and current.retryable:
                    retried = replace(
                        current, status=ReceiptStatus.IN_PROGRESS,
                        attempts=current.attempts + 1, retryable=False, completed_at=None,
                    )
                    cursor.execute(
                        "UPDATE bb_provider_receipts SET status=%s, body=%s WHERE receipt_id=%s",
                        (retried.status.value, encode(retried), retried.receipt_id),
                    )
                    return retried, True
                return current, False

    def complete_provider_receipt(self, receipt: Any) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """UPDATE bb_provider_receipts SET status=%s, body=%s
                WHERE receipt_id=%s AND tenant_id=%s AND company_id=%s AND job_id=%s""",
                (receipt.status.value, encode(receipt), receipt.receipt_id,
                 receipt.tenant_id, receipt.company_id, receipt.job_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("provider receipt not found in tenant/company scope")

    def get_provider_receipt(self, tenant_id: str, company_id: str, provider: str, operation: str, idempotency_key: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT body FROM bb_provider_receipts WHERE tenant_id=%s AND company_id=%s
                AND provider=%s AND operation=%s AND idempotency_key=%s""",
                (tenant_id, company_id, provider, operation, idempotency_key),
            )
            row = cursor.fetchone()
        return self._provider_receipt(row["body"]) if row else None

    def list_provider_receipts(self, tenant_id: str, company_id: str) -> tuple[Any, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT body FROM bb_provider_receipts WHERE tenant_id=%s AND company_id=%s ORDER BY receipt_id",
                (tenant_id, company_id),
            )
            return tuple(self._provider_receipt(row["body"]) for row in cursor)

    def save_oauth_transaction(self, transaction: Any) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO bb_oauth_transactions(
                state_digest, session_id, tenant_id, company_id, redirect_digest,
                verifier_digest, expires_at, body) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(state_digest) DO NOTHING""",
                (transaction.state_digest, transaction.session_id, transaction.tenant_id,
                 transaction.company_id, transaction.redirect_uri_digest,
                 transaction.pkce_verifier_digest, transaction.expires_at, encode(transaction)),
            )

    def get_oauth_transaction(self, state_digest):
        from businessbuilder.provider_connection.serialization import decode_oauth_transaction
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT body FROM bb_oauth_transactions WHERE state_digest=%s", (state_digest,))
            row = cursor.fetchone()
        return decode_oauth_transaction(decode(row["body"])) if row else None

    def consume_oauth_transaction(self, state_digest, session_id, redirect_digest, verifier_digest, *, at):
        from businessbuilder.provider_connection.serialization import decode_oauth_transaction
        with self.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """SELECT body FROM bb_oauth_transactions WHERE state_digest=%s
                    AND session_id=%s AND redirect_digest=%s AND verifier_digest=%s
                    AND consumed_at IS NULL AND expires_at>%s FOR UPDATE""",
                    (state_digest, session_id, redirect_digest, verifier_digest, at),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                value = decode_oauth_transaction(decode(row["body"]))
                consumed = replace(value, consumed_at=at)
                cursor.execute(
                    "UPDATE bb_oauth_transactions SET consumed_at=%s, body=%s WHERE state_digest=%s AND consumed_at IS NULL",
                    (at, encode(consumed), state_digest),
                )
                return consumed

    def claim_provider_refresh(self, tenant_id, company_id, connection_id, owner, *, at, lease):
        with self.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO bb_provider_refresh_leases(connection_id, tenant_id, company_id, owner, lease_until)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT(connection_id) DO UPDATE SET owner=EXCLUDED.owner, lease_until=EXCLUDED.lease_until
                    WHERE bb_provider_refresh_leases.tenant_id=EXCLUDED.tenant_id
                    AND bb_provider_refresh_leases.company_id=EXCLUDED.company_id
                    AND bb_provider_refresh_leases.lease_until<=%s RETURNING owner""",
                    (connection_id, tenant_id, company_id, owner, at + lease, at),
                )
                return cursor.fetchone() is not None

    def release_provider_refresh(self, tenant_id, company_id, connection_id, owner):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """DELETE FROM bb_provider_refresh_leases WHERE connection_id=%s
                AND tenant_id=%s AND company_id=%s AND owner=%s""",
                (connection_id, tenant_id, company_id, owner),
            )

    def claim_provider_callback(self, provider, event_id):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO bb_provider_callback_events(provider, event_id) VALUES (%s,%s)
                ON CONFLICT(provider, event_id) DO NOTHING RETURNING event_id""",
                (provider, event_id),
            )
            return cursor.fetchone() is not None

    @staticmethod
    def _communication_reservation(body: str):
        from businessbuilder.outbound_communications.serialization import decode_communication_record
        return decode_communication_record("communication_rate_reservation", decode(body))

    def reserve_communication_send(self, reservation, limits):
        lock_key = int(sha256(
            f"{reservation.tenant_id}\0{reservation.company_id}\0communications".encode()
        ).hexdigest()[:15], 16)
        windows = (
            ("company_minute", None, timedelta(minutes=1)),
            ("company_hour", None, timedelta(hours=1)),
            ("company_day", None, timedelta(days=1)),
            ("recipient_minute", reservation.recipient_id, timedelta(minutes=1)),
            ("recipient_hour", reservation.recipient_id, timedelta(hours=1)),
            ("recipient_day", reservation.recipient_id, timedelta(days=1)),
            ("burst", reservation.recipient_id, timedelta(seconds=10)),
        )
        with self.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
                cursor.execute(
                    """SELECT body FROM bb_communication_send_reservations
                    WHERE tenant_id=%s AND company_id=%s AND idempotency_key=%s FOR UPDATE""",
                    (reservation.tenant_id, reservation.company_id, reservation.idempotency_key),
                )
                row = cursor.fetchone()
                if row:
                    return self._communication_reservation(row["body"]), False, "duplicate"
                for name, recipient_id, window in windows:
                    query = """SELECT COUNT(*) AS n FROM bb_communication_send_reservations
                        WHERE tenant_id=%s AND company_id=%s AND reserved_at>=%s"""
                    args = [reservation.tenant_id, reservation.company_id,
                            reservation.reserved_at - window]
                    if recipient_id is not None:
                        query += " AND recipient_id=%s"
                        args.append(recipient_id)
                    cursor.execute(query, args)
                    if cursor.fetchone()["n"] >= limits[name]:
                        return reservation, False, f"{name} rate limit exceeded"
                cursor.execute(
                    """INSERT INTO bb_communication_send_reservations(
                    reservation_id,tenant_id,company_id,recipient_id,purpose,
                    idempotency_key,reserved_at,body) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (reservation.reservation_id, reservation.tenant_id, reservation.company_id,
                     reservation.recipient_id, reservation.purpose.value,
                     reservation.idempotency_key, reservation.reserved_at, encode(reservation)),
                )
                return reservation, True, "reserved"

    def claim_communication_event(self, kind, event_id):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO bb_communication_events(kind,event_id) VALUES (%s,%s)
                ON CONFLICT(kind,event_id) DO NOTHING RETURNING event_id""",
                (kind, event_id),
            )
            return cursor.fetchone() is not None

    @staticmethod
    def _canary_reservation(body: str):
        from businessbuilder.live_canary.serialization import decode_canary_record
        return decode_canary_record("live_canary_send_reservation", decode(body))

    def reserve_canary_send(self, reservation, total_limit, hour_limit):
        lock_key = int(sha256(f"{reservation.permit_id}\0canary".encode()).hexdigest()[:15], 16)
        with self.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
                cursor.execute(
                    """SELECT body FROM bb_canary_send_reservations
                    WHERE permit_id=%s AND idempotency_key=%s FOR UPDATE""",
                    (reservation.permit_id, reservation.idempotency_key))
                row = cursor.fetchone()
                if row:
                    return self._canary_reservation(row["body"]), False, "duplicate"
                cursor.execute("SELECT COUNT(*) AS n FROM bb_canary_send_reservations WHERE permit_id=%s",
                               (reservation.permit_id,))
                if cursor.fetchone()["n"] >= total_limit:
                    return reservation, False, "canary total send cap exceeded"
                cursor.execute(
                    """SELECT COUNT(*) AS n FROM bb_canary_send_reservations
                    WHERE permit_id=%s AND reserved_at>=%s""",
                    (reservation.permit_id, reservation.reserved_at - timedelta(hours=1)))
                if cursor.fetchone()["n"] >= hour_limit:
                    return reservation, False, "canary hourly send cap exceeded"
                cursor.execute(
                    """INSERT INTO bb_canary_send_reservations(
                    reservation_id,permit_id,tenant_id,company_id,communication_id,
                    idempotency_key,reserved_at,body) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (reservation.reservation_id, reservation.permit_id,
                     reservation.tenant_id, reservation.company_id,
                     reservation.communication_id, reservation.idempotency_key,
                     reservation.reserved_at, encode(reservation)))
                return reservation, True, "reserved"


class PostgresVerificationRepository(VerificationRepository, _PostgresRepository):
    """Append-versioned PostgreSQL store for Verification-owned readiness evidence."""

    def __init__(self, dsn: str | None = None, *, schema: str | None = None) -> None:
        self._lock = RLock()
        _PostgresRepository.__init__(
            self, dsn, schema=schema, application_name="businessbuilder-verification"
        )

    def save(self, record: VerificationRecord) -> VerificationRecord:
        with self._lock, self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT version FROM bb_verification_records
                    WHERE tenant_id=%s AND company_id=%s AND verification_id=%s
                    ORDER BY version DESC LIMIT 1 FOR UPDATE
                    """,
                    (record.tenant_id, record.company_id, record.verification_id),
                )
                row = cursor.fetchone()
                if row and record.version <= row["version"]:
                    raise ValueError("verification version must increase")
                cursor.execute(
                    """
                    INSERT INTO bb_verification_records(
                        tenant_id, company_id, verification_id, version, state, body
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.tenant_id,
                        record.company_id,
                        record.verification_id,
                        record.version,
                        record.state.value,
                        json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")),
                    ),
                )
        return record

    def get(
        self, tenant_id: str, company_id: str, verification_id: str
    ) -> VerificationRecord:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT body FROM bb_verification_records
                WHERE tenant_id=%s AND company_id=%s AND verification_id=%s
                ORDER BY version DESC LIMIT 1
                """,
                (tenant_id, company_id, verification_id),
            )
            row = cursor.fetchone()
        if not row:
            raise KeyError("verification not found in tenant/company scope")
        return VerificationRecord.from_dict(json.loads(row["body"]))

    def list_for_company(
        self, tenant_id: str, company_id: str
    ) -> tuple[VerificationRecord, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT ON (verification_id) verification_id, body, version
                FROM bb_verification_records
                WHERE tenant_id=%s AND company_id=%s
                ORDER BY verification_id, version DESC
                """,
                (tenant_id, company_id),
            )
            return tuple(
                VerificationRecord.from_dict(json.loads(row["body"])) for row in cursor
            )


_WORKFORCE_TYPES = {
    item.__name__: item
    for item in (
        WorkforceAuditRecord,
        WorkforceBudgetCeiling,
        WorkforceCapabilityGrant,
        WorkforceEscalationRule,
        WorkforcePolicyDecision,
        WorkforcePolicyEvaluation,
        WorkforceRoleDefinition,
        WorkforceRoleState,
    )
}


class PostgresWorkforceRepository(InMemoryWorkforceRepository, _PostgresRepository):
    """AI Workforce policy persistence in the existing PostgreSQL schema."""

    def __init__(self, dsn: str | None = None, *, schema: str | None = None) -> None:
        InMemoryWorkforceRepository.__init__(self)
        _PostgresRepository.__init__(
            self, dsn, schema=schema, application_name="businessbuilder-ai-workforce"
        )
        self._load_postgres()

    @staticmethod
    def _scope(*parts: object) -> str:
        return "\x1f".join(str(item) for item in parts)

    def _put(self, kind: str, key: str, tenant_id: str, company_id: str, value: object) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO bb_ai_workforce_records(kind, scope_key, tenant_id, company_id, body)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (kind, scope_key) DO UPDATE SET body=EXCLUDED.body""",
                (kind, key, tenant_id, company_id, encode_record(value)),
            )

    def _load_postgres(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT kind, scope_key, body FROM bb_ai_workforce_records ORDER BY kind, scope_key"
            )
            for row in cursor:
                kind = row["kind"]
                parts = row["scope_key"].split("\x1f")
                if kind == "role":
                    value = decode_record(row["body"], _WORKFORCE_TYPES)
                    self._definitions[(value.tenant_id, value.company_id, value.role_id, value.version)] = value
                elif kind == "current":
                    self._current[(parts[0], parts[1], parts[2])] = int(
                        decode_record(row["body"], _WORKFORCE_TYPES)
                    )
                elif kind == "evaluation":
                    value = decode_record(row["body"], _WORKFORCE_TYPES)
                    self._evaluations[(parts[0], parts[1], parts[2])] = value
                elif kind == "command":
                    value = decode_record(row["body"], _WORKFORCE_TYPES)
                    self._commands[(parts[0], parts[1], parts[2])] = (
                        value["digest"], value["result"]
                    )
            cursor.execute("SELECT body FROM bb_ai_workforce_audit_events ORDER BY sequence")
            self._audit.extend(
                decode_record(row["body"], _WORKFORCE_TYPES) for row in cursor
            )

    def add_definition(self, definition: WorkforceRoleDefinition, *, make_current: bool = True) -> None:
        super().add_definition(definition, make_current=make_current)
        with self.connection.transaction():
            self._put(
                "role",
                self._scope(definition.tenant_id, definition.company_id, definition.role_id, definition.version),
                definition.tenant_id, definition.company_id, definition,
            )
            if make_current:
                self._put(
                    "current",
                    self._scope(definition.tenant_id, definition.company_id, definition.role_id),
                    definition.tenant_id, definition.company_id, definition.version,
                )

    def replace_definition(self, definition: WorkforceRoleDefinition) -> None:
        super().replace_definition(definition)
        self._put(
            "role",
            self._scope(definition.tenant_id, definition.company_id, definition.role_id, definition.version),
            definition.tenant_id, definition.company_id, definition,
        )

    def save_evaluation(self, idempotency_key: str, evaluation: WorkforcePolicyEvaluation) -> WorkforcePolicyEvaluation:
        value = super().save_evaluation(idempotency_key, evaluation)
        self._put(
            "evaluation",
            self._scope(evaluation.tenant_id, evaluation.company_id, idempotency_key),
            evaluation.tenant_id, evaluation.company_id, value,
        )
        return value

    def save_command(self, tenant_id, company_id, idempotency_key, command_digest, result):
        value = super().save_command(
            tenant_id, company_id, idempotency_key, command_digest, result
        )
        self._put(
            "command", self._scope(tenant_id, company_id, idempotency_key),
            tenant_id, company_id, {"digest": command_digest, "result": value},
        )
        return value

    def append_audit(self, record: WorkforceAuditRecord) -> None:
        super().append_audit(record)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO bb_ai_workforce_audit_events(
                audit_id, tenant_id, company_id, body
                ) VALUES (%s, %s, %s, %s)""",
                (record.audit_id, record.tenant_id, record.company_id, encode_record(record)),
            )


class PostgresCompanyBrainRepository(_PostgresRepository):
    """Company Brain port using scoped, versioned PostgreSQL records."""

    def __init__(self, dsn: str | None = None, *, schema: str | None = None) -> None:
        _PostgresRepository.__init__(
            self, dsn, schema=schema, application_name="businessbuilder-company-brain"
        )

    def migrate(self) -> None:
        migrate(self.connection)

    def create_company(self, company: Company) -> Company:
        payload = json.dumps(company.to_contract(), sort_keys=True, separators=(",", ":"))
        try:
            with self.connection.transaction():
                with self.connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO bb_companies(
                            tenant_id, company_id, payload, lifecycle, readiness,
                            version, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            company.scope.tenant_id,
                            company.scope.company_id,
                            payload,
                            company.lifecycle.value,
                            company.readiness,
                            company.version,
                            company.created_at,
                            company.updated_at,
                        ),
                    )
                    self._insert_company_version(cursor, company, payload)
        except psycopg.errors.UniqueViolation as exc:
            raise ConflictError("company already exists in this tenant") from exc
        return company

    def get_company(self, scope: Scope) -> Company:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM bb_companies WHERE tenant_id=%s AND company_id=%s",
                (scope.tenant_id, scope.company_id),
            )
            row = cursor.fetchone()
        if not row:
            raise NotFoundError("company not found")
        return self._company(row)

    def company_history(self, scope: Scope) -> list[Company]:
        self.get_company(scope)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM bb_company_versions
                WHERE tenant_id=%s AND company_id=%s ORDER BY version
                """,
                (scope.tenant_id, scope.company_id),
            )
            return [self._company(row) for row in cursor]

    def save_company(self, company: Company, *, expected_version: int) -> Company:
        payload = json.dumps(company.to_contract(), sort_keys=True, separators=(",", ":"))
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE bb_companies SET payload=%s, lifecycle=%s, readiness=%s,
                        version=%s, updated_at=%s
                    WHERE tenant_id=%s AND company_id=%s AND version=%s
                    """,
                    (
                        payload,
                        company.lifecycle.value,
                        company.readiness,
                        company.version,
                        company.updated_at,
                        company.scope.tenant_id,
                        company.scope.company_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount == 1:
                    self._insert_company_version(cursor, company, payload)
                    return company
                cursor.execute(
                    "SELECT 1 FROM bb_companies WHERE tenant_id=%s AND company_id=%s",
                    (company.scope.tenant_id, company.scope.company_id),
                )
                if cursor.fetchone():
                    raise ConflictError("company version conflict")
                raise NotFoundError("company not found")

    def append_record(
        self, record: BrainRecord, *, expected_version: int | None
    ) -> BrainRecord:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT version FROM bb_brain_records
                    WHERE tenant_id=%s AND company_id=%s AND record_id=%s
                        AND superseded_by_version IS NULL
                    FOR UPDATE
                    """,
                    (
                        record.scope.tenant_id,
                        record.scope.company_id,
                        record.record_id,
                    ),
                )
                current = cursor.fetchone()
                if current is None and expected_version is not None:
                    raise ConflictError("record does not exist at expected version")
                if current is not None and (
                    expected_version is None or current["version"] != expected_version
                ):
                    raise ConflictError("record version conflict")
                if current is not None:
                    cursor.execute(
                        """
                        UPDATE bb_brain_records SET superseded_by_version=%s
                        WHERE tenant_id=%s AND company_id=%s AND record_id=%s
                            AND version=%s AND superseded_by_version IS NULL
                        """,
                        (
                            record.version,
                            record.scope.tenant_id,
                            record.scope.company_id,
                            record.record_id,
                            current["version"],
                        ),
                    )
                cursor.execute(
                    """
                    INSERT INTO bb_brain_records(
                        tenant_id, company_id, record_id, version, kind, data_json,
                        knowledge_class, confidence, owner_ref_json, provenance_json,
                        lifecycle, created_at, updated_at, supersedes_version,
                        superseded_by_version, invalidated_at, invalidation_reason
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.scope.tenant_id,
                        record.scope.company_id,
                        record.record_id,
                        record.version,
                        record.kind.value,
                        json.dumps(dict(record.data), sort_keys=True, separators=(",", ":")),
                        record.knowledge_class.value,
                        record.confidence,
                        json.dumps(record.owner_ref.to_dict(), sort_keys=True),
                        json.dumps([p.to_dict() for p in record.provenance], sort_keys=True),
                        record.lifecycle,
                        record.created_at,
                        record.updated_at,
                        record.supersedes_version,
                        record.superseded_by_version,
                        record.invalidated_at,
                        record.invalidation_reason,
                    ),
                )
        return record

    def get_record(self, scope: Scope, record_id: str) -> BrainRecord:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM bb_brain_records
                WHERE tenant_id=%s AND company_id=%s AND record_id=%s
                    AND superseded_by_version IS NULL
                """,
                (scope.tenant_id, scope.company_id, record_id),
            )
            row = cursor.fetchone()
        if not row:
            raise NotFoundError("record not found")
        return self._record(row)

    def list_current(
        self, scope: Scope, *, kinds: Sequence[RecordKind] | None = None
    ) -> list[BrainRecord]:
        query = (
            "SELECT * FROM bb_brain_records WHERE tenant_id=%s AND company_id=%s "
            "AND superseded_by_version IS NULL"
        )
        params: list[object] = [scope.tenant_id, scope.company_id]
        if kinds:
            query += " AND kind = ANY(%s)"
            params.append([kind.value for kind in kinds])
        query += " ORDER BY kind, record_id"
        with self.connection.cursor() as cursor:
            cursor.execute(query, params)
            return [self._record(row) for row in cursor]

    def history(
        self, scope: Scope, record_id: str | None = None
    ) -> list[BrainRecord]:
        query = "SELECT * FROM bb_brain_records WHERE tenant_id=%s AND company_id=%s"
        params: list[object] = [scope.tenant_id, scope.company_id]
        if record_id:
            query += " AND record_id=%s"
            params.append(record_id)
        query += " ORDER BY kind, record_id, version"
        with self.connection.cursor() as cursor:
            cursor.execute(query, params)
            return [self._record(row) for row in cursor]

    def add_dependency(self, dependency: Dependency) -> None:
        if dependency.source_record_id.startswith("company."):
            if dependency.source_record_id not in {
                "company.owners",
                "company.jurisdiction",
            }:
                raise NotFoundError("unsupported company dependency source")
            self.get_company(dependency.scope)
        else:
            self.get_record(dependency.scope, dependency.source_record_id)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_brain_dependencies(
                    tenant_id, company_id, source_record_id, dependent_type,
                    dependent_id, dependent_version, trigger, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    dependency.scope.tenant_id,
                    dependency.scope.company_id,
                    dependency.source_record_id,
                    dependency.dependent_ref.type,
                    dependency.dependent_ref.id,
                    dependency.dependent_ref.version,
                    dependency.trigger,
                    dependency.created_at,
                ),
            )

    def dependencies_for(
        self, scope: Scope, source_record_id: str
    ) -> list[Dependency]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM bb_brain_dependencies
                WHERE tenant_id=%s AND company_id=%s AND source_record_id=%s
                ORDER BY dependent_type, dependent_id
                """,
                (scope.tenant_id, scope.company_id, source_record_id),
            )
            return [
                Dependency(
                    scope,
                    row["source_record_id"],
                    EntityRef(
                        row["dependent_type"],
                        row["dependent_id"],
                        row["dependent_version"],
                    ),
                    row["trigger"],
                    row["created_at"],
                )
                for row in cursor
            ]

    def append_invalidation(self, notice: InvalidationNotice) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bb_invalidation_notices(
                    tenant_id, company_id, notice_id, source_record_id,
                    source_version, dependent_type, dependent_id,
                    dependent_version, trigger, reason, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    notice.scope.tenant_id,
                    notice.scope.company_id,
                    notice.notice_id,
                    notice.source_record_id,
                    notice.source_version,
                    notice.dependent_ref.type,
                    notice.dependent_ref.id,
                    notice.dependent_ref.version,
                    notice.trigger,
                    notice.reason,
                    notice.occurred_at,
                ),
            )

    def list_invalidations(self, scope: Scope) -> list[InvalidationNotice]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM bb_invalidation_notices
                WHERE tenant_id=%s AND company_id=%s ORDER BY occurred_at, notice_id
                """,
                (scope.tenant_id, scope.company_id),
            )
            return [
                InvalidationNotice(
                    scope,
                    row["notice_id"],
                    row["source_record_id"],
                    row["source_version"],
                    EntityRef(
                        row["dependent_type"],
                        row["dependent_id"],
                        row["dependent_version"],
                    ),
                    row["trigger"],
                    row["reason"],
                    row["occurred_at"],
                )
                for row in cursor
            ]

    @staticmethod
    def _insert_company_version(
        cursor: psycopg.Cursor[dict[str, Any]], company: Company, payload: str
    ) -> None:
        cursor.execute(
            """
            INSERT INTO bb_company_versions(
                tenant_id, company_id, version, payload, lifecycle, readiness,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                company.scope.tenant_id,
                company.scope.company_id,
                company.version,
                payload,
                company.lifecycle.value,
                company.readiness,
                company.created_at,
                company.updated_at,
            ),
        )

    @staticmethod
    def _company(row: dict[str, Any]) -> Company:
        value = json.loads(row["payload"])
        return Company(
            scope=Scope(row["tenant_id"], row["company_id"]),
            display_name=value["display_name"],
            legal_name=value.get("legal_name"),
            archetype=value["archetype"],
            jurisdiction=value["jurisdiction"],
            owner_refs=tuple(EntityRef(**item) for item in value["owner_refs"]),
            lifecycle=LifecycleState(row["lifecycle"]),
            readiness=row["readiness"],
            permissions=tuple(value.get("permissions", [])),
            provenance=tuple(
                PostgresCompanyBrainRepository._provenance(item)
                for item in value["provenance"]
            ),
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _provenance(value: dict[str, Any]) -> Provenance:
        converted = dict(value)
        converted["actor_ref"] = EntityRef(**converted["actor_ref"])
        return Provenance(**converted)

    @staticmethod
    def _record(row: dict[str, Any]) -> BrainRecord:
        return BrainRecord(
            scope=Scope(row["tenant_id"], row["company_id"]),
            record_id=row["record_id"],
            kind=RecordKind(row["kind"]),
            data=json.loads(row["data_json"]),
            knowledge_class=KnowledgeClass(row["knowledge_class"]),
            confidence=row["confidence"],
            owner_ref=EntityRef(**json.loads(row["owner_ref_json"])),
            provenance=tuple(
                PostgresCompanyBrainRepository._provenance(item)
                for item in json.loads(row["provenance_json"])
            ),
            lifecycle=row["lifecycle"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            supersedes_version=row["supersedes_version"],
            superseded_by_version=row["superseded_by_version"],
            invalidated_at=row["invalidated_at"],
            invalidation_reason=row["invalidation_reason"],
        )
