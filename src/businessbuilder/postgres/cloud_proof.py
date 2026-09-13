from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any

from businessbuilder.commercial import (
    BillingPeriod,
    CommercialNotFound,
    CommercialService,
    EntitlementClass,
    EntitlementStatus,
    NormalizedBillingEvent,
    ProductCode,
    SubscriptionStatus,
    seed_default_catalog,
)
from businessbuilder.company_brain import CompanyBrainService, NotFoundError, Scope, load_billy_bob
from businessbuilder.identity import AuthorizationContext, IdentityService, Role
from businessbuilder.integration import CompanyBrainVerificationAdapter
from businessbuilder.integration.commercial import RuntimeCommercialEventSink, RuntimeEntitlementGuard
from businessbuilder.runtime.audit import AuditLog
from businessbuilder.runtime.events import LocalEventBus
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.verification import ReadinessEvaluator, billy_bob_policy

from .connection import connect_postgres
from .migrations import migrate
from .repositories import (
    PostgresCommercialRepository,
    PostgresCompanyBrainRepository,
    PostgresIdentityRepository,
    PostgresRuntimeRepository,
    PostgresVerificationRepository,
)


PROOF_ID = "billy-bob-commercial-postgres-v1"
TENANT_ID = "tenant_billy_cloud"
COMPANY_ID = "co_billy_bob_lawn"
FIXED_NOW = datetime(2026, 9, 13, 21, 0, tzinfo=timezone.utc)
_PROOF_LOCK = RLock()


class ProofClock:
    def __init__(self) -> None:
        self.now = FIXED_NOW

    def __call__(self) -> datetime:
        return self.now


def _stored_proof(dsn: str | None, schema: str | None, proof_id: str) -> dict[str, Any] | None:
    connection = connect_postgres(
        dsn, schema=schema, application_name="businessbuilder-proof-read"
    )
    try:
        migrate(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, body FROM bb_cloud_proofs WHERE proof_id=%s",
                (proof_id,),
            )
            row = cursor.fetchone()
        if row and row["status"] == "passed" and row["body"]:
            return json.loads(row["body"])
        return None
    finally:
        connection.close()


def get_cloud_proof(
    dsn: str | None = None, *, schema: str | None = None, proof_id: str = PROOF_ID
) -> dict[str, Any] | None:
    return _stored_proof(dsn, schema, proof_id)


def run_cloud_proof(
    dsn: str | None = None, *, schema: str | None = None, proof_id: str = PROOF_ID
) -> dict[str, Any]:
    """Run the deterministic Worker 10 commercial flow once against PostgreSQL."""
    with _PROOF_LOCK:
        existing = _stored_proof(dsn, schema, proof_id)
        if existing:
            return {**existing, "loaded_from_persistence": True}

        marker = connect_postgres(
            dsn, schema=schema, application_name="businessbuilder-proof-marker"
        )
        try:
            migrate(marker)
            with marker.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO bb_cloud_proofs(proof_id, status)
                    VALUES (%s, 'running') ON CONFLICT DO NOTHING
                    RETURNING proof_id
                    """,
                    (proof_id,),
                )
                claimed = cursor.fetchone() is not None
                if not claimed:
                    cursor.execute(
                        "SELECT status FROM bb_cloud_proofs WHERE proof_id=%s",
                        (proof_id,),
                    )
                    status = cursor.fetchone()["status"]
                    raise RuntimeError(f"cloud proof is not retryable from status {status}")
        finally:
            marker.close()

        try:
            result = _execute_cloud_proof(dsn, schema)
        except Exception:
            failed = connect_postgres(
                dsn, schema=schema, application_name="businessbuilder-proof-failure"
            )
            try:
                with failed.cursor() as cursor:
                    cursor.execute(
                        "UPDATE bb_cloud_proofs SET status='failed' WHERE proof_id=%s",
                        (proof_id,),
                    )
            finally:
                failed.close()
            raise

        writer = connect_postgres(
            dsn, schema=schema, application_name="businessbuilder-proof-write"
        )
        try:
            with writer.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE bb_cloud_proofs
                    SET status='passed', body=%s, completed_at=now()
                    WHERE proof_id=%s AND status='running'
                    """,
                    (json.dumps(result, sort_keys=True, separators=(",", ":")), proof_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("cloud proof completion marker was not updated")
        finally:
            writer.close()
        return {**result, "loaded_from_persistence": False}


def _execute_cloud_proof(dsn: str | None, schema: str | None) -> dict[str, Any]:
    clock = ProofClock()
    ids = DeterministicIds()
    identity_repository = PostgresIdentityRepository(dsn, schema=schema)
    brain_repository = PostgresCompanyBrainRepository(dsn, schema=schema)
    runtime_repository = PostgresRuntimeRepository(dsn, schema=schema)
    commercial_repository = PostgresCommercialRepository(dsn, schema=schema)
    verification_repository = PostgresVerificationRepository(dsn, schema=schema)

    try:
        identity = IdentityService(identity_repository, id_factory=ids, clock=clock)
        user, founder = identity.register_founder(
            "billy.cloud@example.test", "Billy Bob"
        )
        tenant, organization, membership = identity.create_account(
            user.user_id,
            "Billy Bob Ventures",
            tenant_id=TENANT_ID,
            organization_id="org_billy_cloud",
            membership_id="membership_billy_cloud_owner",
        )

        brain = CompanyBrainService(brain_repository)
        scope = load_billy_bob(brain, TENANT_ID)
        owner_context = AuthorizationContext(user.user_id, TENANT_ID)
        identity.attach_company(owner_context, COMPANY_ID)
        company_context = AuthorizationContext(user.user_id, TENANT_ID, COMPANY_ID)

        verification_snapshot = CompanyBrainVerificationAdapter(brain)
        readiness = ReadinessEvaluator(verification_repository, billy_bob_policy())
        readiness_before = readiness.evaluate(
            verification_snapshot.get_snapshot(TENANT_ID, COMPANY_ID), at=clock()
        )

        runtime_events = LocalEventBus(
            runtime_repository, AuditLog(runtime_repository, ids, clock)
        )
        fulfillment_events: list[Any] = []
        runtime_events.subscribe(
            "commercial.fulfillment.eligible", fulfillment_events.append
        )

        seed_default_catalog(commercial_repository, effective_at=clock())
        commercial = CommercialService(
            commercial_repository,
            identity.authorization,
            RuntimeCommercialEventSink(runtime_events),
            id_factory=ids,
            clock=clock,
        )

        def purchase(code: ProductCode, ordinal: int) -> tuple[Any, bool, bool, int, int]:
            version_id = f"product_version_{code.value.lower()}_v1"
            order = commercial.create_order(
                company_context,
                version_id,
                order_id=f"order_billy_cloud_{ordinal}",
            )
            checkout = commercial.create_checkout(
                company_context,
                order.order_id,
                f"billy-cloud-checkout-{ordinal}",
            )
            event = NormalizedBillingEvent(
                event_id=f"billing_event_cloud_payment_{ordinal}",
                event_type="billing.payment.succeeded",
                tenant_id=TENANT_ID,
                user_id=user.user_id,
                company_id=COMPANY_ID,
                occurred_at=clock(),
                provider="staging_fixture",
                provider_event_ref=f"provider_cloud_payment_event_{ordinal}",
                correlation_id=f"correlation_cloud_purchase_{ordinal}",
                order_id=order.order_id,
                checkout_intent_id=checkout.checkout_intent_id,
                payment_provider_ref=f"opaque_cloud_payment_{ordinal}",
            )
            first_applied = commercial.handle_billing_event(event)
            history_before_duplicate = len(
                commercial_repository.order_history(
                    TENANT_ID, COMPANY_ID, order.order_id
                )
            )
            duplicate_applied = commercial.handle_billing_event(event)
            history_after_duplicate = len(
                commercial_repository.order_history(
                    TENANT_ID, COMPANY_ID, order.order_id
                )
            )
            return (
                commercial_repository.get_order(
                    TENANT_ID, COMPANY_ID, order.order_id
                ),
                first_applied,
                duplicate_applied,
                history_before_duplicate,
                history_after_duplicate,
            )

        (
            build_order,
            build_event_applied,
            build_duplicate_applied,
            build_history_before,
            build_history_after,
        ) = purchase(ProductCode.BUILD_BUSINESS, 1)

        readiness_after_purchase = readiness.evaluate(
            verification_snapshot.get_snapshot(TENANT_ID, COMPANY_ID), at=clock()
        )

        (
            run_order,
            run_event_applied,
            run_duplicate_applied,
            run_history_before,
            run_history_after,
        ) = purchase(ProductCode.BUILD_AND_RUN, 2)
        period = BillingPeriod(clock(), clock() + timedelta(days=30))
        subscription_event = NormalizedBillingEvent(
            event_id="billing_event_cloud_subscription_1",
            event_type="billing.subscription.created",
            tenant_id=TENANT_ID,
            user_id=user.user_id,
            company_id=COMPANY_ID,
            occurred_at=clock(),
            provider="staging_fixture",
            provider_event_ref="provider_cloud_subscription_event_1",
            correlation_id="correlation_cloud_subscription_1",
            order_id=run_order.order_id,
            subscription_provider_ref="opaque_cloud_subscription_billy",
            subscription_status=SubscriptionStatus.ACTIVE,
            current_period=period,
        )
        subscription_event_applied = commercial.handle_billing_event(subscription_event)
        subscription_duplicate_applied = commercial.handle_billing_event(subscription_event)
        subscription = commercial_repository.get_subscription_by_provider_ref(
            TENANT_ID, COMPANY_ID, "opaque_cloud_subscription_billy"
        )
        if subscription is None:
            raise AssertionError("subscription was not persisted")

        guard = RuntimeEntitlementGuard(commercial)
        guard.require_capability(TENANT_ID, COMPANY_ID, "ai_workforce.execute")
        commercial.request_subscription_cancellation(
            company_context,
            subscription.subscription_id,
            "Billy chose to stop managed operations",
        )
        clock.now = period.ends_at
        commercial.advance_time(TENANT_ID, COMPANY_ID)

        final_subscription = commercial_repository.get_subscription(
            TENANT_ID, COMPANY_ID, subscription.subscription_id
        )
        grants = commercial_repository.get_current_entitlement_grants(
            TENANT_ID, COMPANY_ID
        )
        customer_owned = tuple(
            grant
            for grant in grants
            if grant.entitlement_class is EntitlementClass.CUSTOMER_OWNED
        )
        managed_run = tuple(
            grant
            for grant in grants
            if grant.entitlement_class is EntitlementClass.STROMATION_MANAGED
            and grant.source_subscription_id == subscription.subscription_id
        )

        if membership.role is not Role.OWNER:
            raise AssertionError("founder membership is not OWNER")
        if not build_event_applied or build_duplicate_applied:
            raise AssertionError("BUILD_BUSINESS billing idempotency failed")
        if not run_event_applied or run_duplicate_applied:
            raise AssertionError("BUILD_AND_RUN billing idempotency failed")
        if not subscription_event_applied or subscription_duplicate_applied:
            raise AssertionError("subscription event idempotency failed")
        if build_history_before != build_history_after:
            raise AssertionError("duplicate build payment changed order history")
        if run_history_before != run_history_after:
            raise AssertionError("duplicate run payment changed order history")
        if not fulfillment_events:
            raise AssertionError("Runtime fulfillment event was not emitted")
        if final_subscription.status is not SubscriptionStatus.CANCELED:
            raise AssertionError("subscription did not cancel at period end")
        if not managed_run or any(
            grant.status is not EntitlementStatus.EXPIRED for grant in managed_run
        ):
            raise AssertionError("managed operations did not expire")
        if not customer_owned or any(
            grant.status is not EntitlementStatus.ACTIVE for grant in customer_owned
        ):
            raise AssertionError("customer-owned state did not remain active")
        if readiness_before.ready or readiness_after_purchase.ready:
            raise AssertionError("commercial state improperly changed readiness")

        identifiers = {
            "user_id": user.user_id,
            "order_build": build_order.order_id,
            "order_run": run_order.order_id,
            "subscription_id": subscription.subscription_id,
        }
        first_pass = {
            "identity_audit_count": len(identity_repository.list_audit(TENANT_ID)),
            "commercial_audit_count": len(
                commercial_repository.list_audit(TENANT_ID, COMPANY_ID)
            ),
            "runtime_event_count": len(
                runtime_repository.list_events(TENANT_ID, COMPANY_ID)
            ),
            "fulfillment_event_count": len(fulfillment_events),
        }
    finally:
        identity_repository.close()
        brain_repository.close()
        runtime_repository.close()
        commercial_repository.close()
        verification_repository.close()

    restart = _verify_restart_and_isolation(dsn, schema, identifiers)
    return {
        "status": "passed",
        "proof": PROOF_ID,
        "backend": "postgresql",
        "provider_mode": "simulated_normalized_events",
        "live_stripe": False,
        "account": {
            "user_id": identifiers["user_id"],
            "tenant_id": tenant.tenant_id,
            "organization_id": organization.organization_id,
            "membership_id": membership.membership_id,
            "membership_role": membership.role.value,
            "company_id": scope.company_id,
        },
        "build_business": {
            "order_id": build_order.order_id,
            "status": build_order.status.value,
            "payment_event_applied": build_event_applied,
            "duplicate_event_applied": build_duplicate_applied,
            "history_unchanged_by_duplicate": build_history_before
            == build_history_after,
        },
        "build_and_run": {
            "order_id": run_order.order_id,
            "status": run_order.status.value,
            "subscription_status": final_subscription.status.value,
            "cancel_at_period_end": True,
            "managed_operations_expired": all(
                grant.status is EntitlementStatus.EXPIRED for grant in managed_run
            ),
            "customer_owned_state_active": all(
                grant.status is EntitlementStatus.ACTIVE for grant in customer_owned
            ),
            "managed_grant_count": len(managed_run),
            "customer_owned_grant_count": len(customer_owned),
        },
        "billing_idempotency": {
            "build_payment_duplicate_ignored": not build_duplicate_applied,
            "run_payment_duplicate_ignored": not run_duplicate_applied,
            "subscription_duplicate_ignored": not subscription_duplicate_applied,
        },
        "runtime": first_pass,
        "readiness": {
            "authority": "verification",
            "ready_before_purchase": readiness_before.ready,
            "ready_after_purchase": readiness_after_purchase.ready,
            "fully_set_after_purchase": readiness_after_purchase.fully_set,
        },
        "restart_persistence": restart["persistence"],
        "tenant_isolation": restart["tenant_isolation"],
        "loaded_from_persistence": False,
    }


def _verify_restart_and_isolation(
    dsn: str | None, schema: str | None, identifiers: dict[str, str]
) -> dict[str, Any]:
    identity = PostgresIdentityRepository(dsn, schema=schema)
    brain = PostgresCompanyBrainRepository(dsn, schema=schema)
    runtime = PostgresRuntimeRepository(dsn, schema=schema)
    commercial = PostgresCommercialRepository(dsn, schema=schema)
    verification = PostgresVerificationRepository(dsn, schema=schema)
    try:
        user = identity.get_user(identifiers["user_id"])
        membership = identity.get_active_membership(TENANT_ID, user.user_id)
        company = brain.get_company(Scope(TENANT_ID, COMPANY_ID))
        build_order = commercial.get_order(
            TENANT_ID, COMPANY_ID, identifiers["order_build"]
        )
        subscription = commercial.get_subscription(
            TENANT_ID, COMPANY_ID, identifiers["subscription_id"]
        )
        grants = commercial.get_current_entitlement_grants(TENANT_ID, COMPANY_ID)
        events = runtime.list_events(TENANT_ID, COMPANY_ID)

        commercial_isolated = False
        try:
            commercial.get_order(
                "tenant_intruder", COMPANY_ID, identifiers["order_build"]
            )
        except CommercialNotFound:
            commercial_isolated = True
        brain_isolated = False
        try:
            brain.get_company(Scope("tenant_intruder", COMPANY_ID))
        except NotFoundError:
            brain_isolated = True

        tenant_isolation = {
            "identity": identity.get_active_membership(
                "tenant_intruder", user.user_id
            )
            is None,
            "company_brain": brain_isolated,
            "commercial": commercial_isolated,
            "runtime": runtime.list_events("tenant_intruder", COMPANY_ID) == [],
            "verification": verification.list_for_company(
                "tenant_intruder", COMPANY_ID
            )
            == (),
        }
        if not all(tenant_isolation.values()):
            raise AssertionError("tenant isolation proof failed")
        persistence = {
            "repository_reopen_passed": True,
            "user_restored": user.email == "billy.cloud@example.test",
            "owner_membership_restored": membership is not None
            and membership.role is Role.OWNER,
            "company_restored": company.scope.company_id == COMPANY_ID,
            "build_order_restored": build_order.order_id
            == identifiers["order_build"],
            "subscription_restored": subscription.status
            is SubscriptionStatus.CANCELED,
            "entitlements_restored": bool(grants),
            "runtime_events_restored": bool(events),
        }
        if not all(persistence.values()):
            raise AssertionError("restart persistence proof failed")
        return {"persistence": persistence, "tenant_isolation": tenant_isolation}
    finally:
        identity.close()
        brain.close()
        runtime.close()
        commercial.close()
        verification.close()
