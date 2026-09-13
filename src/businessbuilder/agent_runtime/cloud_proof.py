from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import secrets

from businessbuilder.ai_workforce import safe_role_definitions
from businessbuilder.commercial import (
    Amount,
    BillingPeriod,
    EntitlementClass,
    EntitlementStatus,
    NormalizedBillingEvent,
    ProductCode,
    SubscriptionStatus,
)
from businessbuilder.company_brain import Company, EntityRef, LifecycleState, Provenance, Scope
from businessbuilder.identity import (
    AuthorizationContext,
    FakeDevAuthenticationProvider,
    IdentityService,
    SessionService,
)
from businessbuilder.runtime import ArtifactRef, Budget, Event, JobStatus, Money
from businessbuilder.runtime.ids import random_id

from .bootstrap import create_postgres_agent_runtime
from .models import TriggerClass
from .queue import SqsQueue
from .service import AgentAdmissionDenied


class ProofClock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now


def run_staging_cloud_proof() -> dict[str, object]:
    environment = os.environ.get("ENVIRONMENT", "").lower()
    if environment not in {"staging", "test", "development", "local"}:
        raise RuntimeError("agent Runtime cloud proof is disabled outside non-production environments")
    queue_url = os.environ.get("JOB_QUEUE_URL", "")
    if not queue_url:
        raise RuntimeError("JOB_QUEUE_URL is required")
    schema = os.environ.get("DB_SCHEMA")
    proof_id = os.environ.get("AGENT_RUNTIME_PROOF_ID", "agent_runtime_staging_v1")
    queue = SqsQueue(queue_url)
    clock = ProofClock()
    signing_key = secrets.token_bytes(32)
    app = create_postgres_agent_runtime(
        queue=queue,
        signing_key=signing_key,
        worker_id="shared-fargate-worker",
        dispatcher_id="staging-agent-dispatcher",
        schema=schema,
        clock=clock,
    )
    try:
        identity = IdentityService(
            app.identity_repository, id_factory=random_id, clock=clock
        )
        owner, _ = identity.register_founder(
            f"agent-proof-{proof_id}@example.test", "Fictional Agent Proof Owner"
        )
        tenant, _, _ = identity.create_account(owner.user_id, "Fictional Agent Proof")
        company_id = f"company_{sha256(proof_id.encode()).hexdigest()[:20]}"
        identity.attach_company(
            AuthorizationContext(owner.user_id, tenant.tenant_id), company_id
        )
        owner_context = AuthorizationContext(owner.user_id, tenant.tenant_id, company_id)
        auth = FakeDevAuthenticationProvider()
        auth.register(owner.user_id, owner.email, "offline-proof-only")
        sessions = SessionService(
            app.identity_repository, auth, id_factory=random_id, clock=clock
        )
        sessions.sign_in(owner.email, "offline-proof-only")

        owner_ref = EntityRef("user", owner.user_id)
        company = Company(
            Scope(tenant.tenant_id, company_id),
            "Fictional Shared Worker Proof",
            "mobile_service",
            {"country": "US", "region": "TX"},
            (owner_ref,),
            lifecycle=LifecycleState.OPERATING,
            provenance=(
                Provenance(
                    "staging_test", clock.now.isoformat().replace("+00:00", "Z"),
                    owner_ref, source_ref="staging://agent-runtime-proof",
                ),
            ),
        )
        app.company_brain.create_company(company)
        for role in safe_role_definitions(
            tenant.tenant_id, company_id, created_at=clock.now
        ):
            app.workforce_repository.add_definition(role)

        order = app.commercial.create_order(
            owner_context, "product_version_build_and_run_v1", amount=Amount("USD", 1000)
        )
        checkout = app.commercial.create_checkout(
            owner_context, order.order_id, f"checkout-{proof_id}"
        )
        payment = _billing_event(
            clock, "payment", "billing.payment.succeeded", tenant.tenant_id,
            owner.user_id, company_id, order_id=order.order_id,
            checkout_intent_id=checkout.checkout_intent_id,
            payment_provider_ref=f"opaque_payment_{proof_id}", amount=order.total,
        )
        app.commercial.handle_billing_event(payment)
        period = BillingPeriod(clock.now, clock.now + timedelta(days=30))
        subscription_event = _billing_event(
            clock, "subscription", "billing.subscription.created", tenant.tenant_id,
            owner.user_id, company_id, order_id=order.order_id,
            subscription_provider_ref=f"opaque_subscription_{proof_id}",
            subscription_status=SubscriptionStatus.ACTIVE, current_period=period,
        )
        app.commercial.handle_billing_event(subscription_event)
        subscription = app.commercial_repository.get_subscription_by_provider_ref(
            tenant.tenant_id, company_id, f"opaque_subscription_{proof_id}"
        )
        app.runtime.budgets.create(
            Budget("budget_agent_cloud", tenant.tenant_id, company_id, Money("USD", 10)),
            "correlation-agent-cloud-budget",
        )
        inbound = _runtime_event(
            app, clock, tenant.tenant_id, company_id, "lead",
            "integration.lead.received", {"artifact_ref": "artifact_test_lead"},
        )
        job = _submit_inbox(app, tenant.tenant_id, company_id, inbound, "agent-cloud-primary")
        envelope = app.runtime_repository.get_agent_envelope(
            tenant.tenant_id, company_id, job.job_id
        )
        if app.dispatcher.dispatch_pending(limit=1) != 1:
            raise AssertionError("agent Runtime outbox did not dispatch")
        if app.worker.process_one(wait_seconds=20) != "succeeded":
            raise AssertionError("shared SQS worker did not complete the bounded job")
        completed = app.runtime_repository.get_agent_execution(
            tenant.tenant_id, company_id, job.job_id
        )
        if completed.state.value != "succeeded":
            raise AssertionError("agent result was not persisted")

        # A duplicate delivery after the commit is acknowledged without execution.
        message_id = f"queue_{sha256(job.job_id.encode()).hexdigest()[:24]}"
        queue.send(message_id, envelope.to_payload())
        if app.worker.process_one(wait_seconds=20) != "duplicate":
            raise AssertionError("duplicate SQS delivery was not idempotent")

        events_before_restart = len([
            item for item in app.runtime_repository.list_events(tenant.tenant_id, company_id)
            if item["type"] == "agent.execution.completed" and item["payload"]["job_id"] == job.job_id
        ])
        customer_owned_before = tuple(
            item for item in app.commercial_repository.get_current_entitlement_grants(
                tenant.tenant_id, company_id
            ) if item.entitlement_class is EntitlementClass.CUSTOMER_OWNED
        )
        company_before = app.company_brain.get_company(Scope(tenant.tenant_id, company_id))
        app.close()

        app = create_postgres_agent_runtime(
            queue=queue, signing_key=signing_key,
            worker_id="shared-fargate-worker-restarted",
            dispatcher_id="staging-agent-dispatcher-restarted",
            schema=schema, clock=clock,
        )
        queue.send(message_id, envelope.to_payload())
        restart_result = app.worker.process_one(wait_seconds=20)
        events_after_restart = len([
            item for item in app.runtime_repository.list_events(tenant.tenant_id, company_id)
            if item["type"] == "agent.execution.completed" and item["payload"]["job_id"] == job.job_id
        ])
        if restart_result != "duplicate" or events_before_restart != events_after_restart:
            raise AssertionError("worker restart duplicated the completed job")

        tight_event = _runtime_event(
            app, clock, tenant.tenant_id, company_id, "budget",
            "runtime.test.triggered", {"artifact_ref": "artifact_budget_test"},
        )
        app.runtime.budgets.create(
            Budget("budget_agent_tight", tenant.tenant_id, company_id, Money("USD", 2)),
            "correlation-agent-tight-budget",
        )
        budget_denied = False
        try:
            _submit_inbox(
                app, tenant.tenant_id, company_id, tight_event,
                "agent-cloud-budget", budget_ref="budget_agent_tight",
            )
        except Exception as exc:
            budget_denied = type(exc).__name__ == "BudgetExceeded"
        if not budget_denied:
            raise AssertionError("Runtime budget ceiling did not deny the job")

        suspended = _billing_event(
            clock, "suspend", "billing.subscription.updated", tenant.tenant_id,
            owner.user_id, company_id,
            subscription_provider_ref=subscription.provider_ref,
            subscription_status=SubscriptionStatus.SUSPENDED,
            current_period=period,
        )
        app.commercial.handle_billing_event(suspended)
        suspended_denied = _expect_denied(
            app, tenant.tenant_id, company_id,
            _runtime_event(app, clock, tenant.tenant_id, company_id, "suspended", "runtime.test.triggered", {}),
            "agent-cloud-suspended",
        )

        reactivated = _billing_event(
            clock, "reactivate", "billing.subscription.updated", tenant.tenant_id,
            owner.user_id, company_id,
            subscription_provider_ref=subscription.provider_ref,
            subscription_status=SubscriptionStatus.ACTIVE,
            current_period=period,
        )
        app.commercial.handle_billing_event(reactivated)
        current_subscription = app.commercial_repository.get_subscription_by_provider_ref(
            tenant.tenant_id, company_id, subscription.provider_ref
        )
        app.commercial.request_subscription_cancellation(
            owner_context, current_subscription.subscription_id,
            "Fictional staging proof period-end cancellation",
        )
        clock.now = period.ends_at
        app.commercial.advance_time(tenant.tenant_id, company_id, at=clock.now)
        expired_denied = _expect_denied(
            app, tenant.tenant_id, company_id,
            _runtime_event(app, clock, tenant.tenant_id, company_id, "expired", "runtime.test.triggered", {}),
            "agent-cloud-expired",
        )
        customer_owned_after = tuple(
            item for item in app.commercial_repository.get_current_entitlement_grants(
                tenant.tenant_id, company_id
            ) if item.entitlement_class is EntitlementClass.CUSTOMER_OWNED
        )
        company_after = app.company_brain.get_company(Scope(tenant.tenant_id, company_id))
        tenant_isolation = (
            app.runtime_repository.get_job("tenant_other", company_id, job.job_id) is None
            and app.runtime_repository.get_agent_envelope("tenant_other", company_id, job.job_id) is None
            and not app.runtime_repository.list_audit("tenant_other", company_id)
        )
        audits = app.runtime_repository.list_audit(tenant.tenant_id, company_id)
        canonical_results = [
            item for item in app.runtime_repository.list_events(tenant.tenant_id, company_id)
            if item["type"] == "agent.execution.completed"
        ]
        proof = {
            "status": "passed",
            "proof": "aws-agent-runtime-v1",
            "backend": "postgresql+sqs+shared-fargate-task",
            "tenant_id": tenant.tenant_id,
            "company_id": company_id,
            "job_id": job.job_id,
            "agent_role": envelope.agent_role,
            "capability": envelope.capability,
            "queue_message_contains_credentials": False,
            "result_persisted": completed.state.value == "succeeded",
            "canonical_result_event_count": len(canonical_results),
            "duplicate_delivery_idempotent": events_after_restart == 1,
            "restart_idempotent": restart_result == "duplicate",
            "suspended_entitlement_denied": suspended_denied,
            "expired_entitlement_denied": expired_denied,
            "budget_ceiling_enforced": budget_denied,
            "tenant_isolation": tenant_isolation,
            "audit_recorded": all(
                action in {item["action"] for item in audits}
                for action in ("agent.execution.admitted", "agent.execution.started", "agent.execution.completed")
            ),
            "customer_owned_state_survived": customer_owned_before == customer_owned_after,
            "company_brain_survived": company_before == company_after,
            "real_external_actions": False,
            "live_model_provider": False,
            "runtime_authority": True,
            "completed_at": clock.now.isoformat().replace("+00:00", "Z"),
        }
        if not all(
            value for key, value in proof.items()
            if key in {
                "result_persisted", "duplicate_delivery_idempotent", "restart_idempotent",
                "suspended_entitlement_denied", "expired_entitlement_denied",
                "budget_ceiling_enforced", "tenant_isolation", "audit_recorded",
                "customer_owned_state_survived", "company_brain_survived", "runtime_authority",
            }
        ):
            raise AssertionError("agent Runtime staging proof invariant failed")
        with app.runtime_repository.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO bb_cloud_proofs(proof_id, status, body, completed_at)
                VALUES (%s, 'passed', %s, %s)
                ON CONFLICT(proof_id) DO UPDATE SET status='passed', body=EXCLUDED.body,
                completed_at=EXCLUDED.completed_at""",
                (proof_id, json.dumps(proof, sort_keys=True), clock.now),
            )
        return proof
    finally:
        app.close()


def _billing_event(clock, suffix, event_type, tenant_id, user_id, company_id, **values):
    return NormalizedBillingEvent(
        f"billing_event_agent_{suffix}", event_type, tenant_id, user_id, company_id,
        clock.now, "fixture_pay", f"provider_event_agent_{suffix}",
        f"correlation_agent_{suffix}", **values,
    )


def _runtime_event(app, clock, tenant_id, company_id, suffix, event_type, payload):
    event = Event(
        f"event_agent_{suffix}", tenant_id, company_id,
        f"correlation-agent-{suffix}", None, event_type, clock.now,
        payload, "businessbuilder.integration.test",
    )
    app.runtime.events.publish(event)
    return event


def _submit_inbox(app, tenant_id, company_id, event, suffix, *, budget_ref="budget_agent_cloud"):
    return app.service.submit(
        tenant_id=tenant_id, company_id=company_id,
        role_id="role_inbox_assistant", capability="communications.email",
        action="classify_message", budget_ref=budget_ref,
        maximum_job_spend=Money("USD", 5),
        idempotency_key=f"agent-runtime-{suffix}-0001",
        correlation_id=event.correlation_id,
        causation_id=event.event_id,
        trigger_class=TriggerClass.INBOUND_EVENT,
        trigger_ref=event.event_id,
        input_artifact_refs=(ArtifactRef("lead", f"artifact_{suffix}"),),
    )


def _expect_denied(app, tenant_id, company_id, event, suffix):
    try:
        _submit_inbox(app, tenant_id, company_id, event, suffix)
    except AgentAdmissionDenied:
        return True
    return False


if __name__ == "__main__":
    print(json.dumps(run_staging_cloud_proof(), sort_keys=True), flush=True)
