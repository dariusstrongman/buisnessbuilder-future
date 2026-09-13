from __future__ import annotations

import json
from threading import RLock
from typing import Any, Sequence

import psycopg

from businessbuilder._serialization import decode_record, encode_record
from businessbuilder.commercial.models import (
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
from businessbuilder.commercial.repository import (
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
            "DO UPDATE SET tenant_id=EXCLUDED.tenant_id, "
            "company_id=EXCLUDED.company_id, body=EXCLUDED.body"
            if replace_row
            else "DO NOTHING"
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO bb_commercial_records(
                    kind, scope_key, version, tenant_id, company_id, body
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (kind, scope_key, version) {conflict}
                """,
                (kind, key, version, tenant_id, company_id, encode_record(value)),
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

    def save_checkout(self, checkout: CheckoutIntent) -> None:
        super().save_checkout(checkout)
        self._insert(
            "checkout",
            self._scope(
                checkout.tenant_id, checkout.company_id, checkout.checkout_intent_id
            ),
            1,
            checkout,
            checkout.tenant_id,
            checkout.company_id,
            replace_row=True,
        )

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


class PostgresRuntimeRepository(RuntimeRepository, _PostgresRepository):
    """Runtime event, job, approval, budget, and audit persistence in PostgreSQL."""

    def __init__(self, dsn: str | None = None, *, schema: str | None = None) -> None:
        self._lock = RLock()
        _PostgresRepository.__init__(
            self, dsn, schema=schema, application_name="businessbuilder-runtime"
        )

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
