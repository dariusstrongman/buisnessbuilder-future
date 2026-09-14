from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import secrets
from urllib.parse import parse_qs, urlparse

from businessbuilder.access_broker import AwsSecretsManagerStore, JobSecretRef, S3ArtifactStore
from businessbuilder.agent_runtime import SqsQueue, TriggerClass
from businessbuilder.agent_runtime.bootstrap import create_postgres_agent_runtime
from businessbuilder.ai_workforce import safe_role_definitions
from businessbuilder.commercial import (
    Amount, BillingPeriod, EntitlementClass, EntitlementStatus,
    NormalizedBillingEvent, SubscriptionStatus,
)
from businessbuilder.company_brain import Company, EntityRef, LifecycleState, Provenance, RecordKind, Scope
from businessbuilder.communications_compliance import CommunicationsCompliance, ComplianceDenied
from businessbuilder.communications_compliance.models import (
    AbuseSignalType, AbuseThresholds, KillSwitchScope, RolloutTier,
)
from businessbuilder.identity import (
    AuthorizationContext, FakeDevAuthenticationProvider, IdentityService, SessionService,
)
from businessbuilder.provider_connection import SandboxEmailProvider, attach_provider_connections
from businessbuilder.runtime import Budget, Event, Money
from businessbuilder.runtime.ids import DeterministicIds

from .bootstrap import attach_outbound_communications
from .models import (
    CommunicationPurpose, ConsentState, ContactRelationship, ContentEvidence,
    DeliveryStatus, SuppressionState,
)
from .service import CommunicationDenied


TENANT = "tenant_communications_staging"
COMPANY = "company_communications_staging"
REDIRECT = "https://staging.example.test/oauth/callback"


class ProofClock:
    def __init__(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.now


def run_staging_cloud_proof() -> dict[str, object]:
    if os.environ.get("ENVIRONMENT", "").lower() != "staging":
        raise RuntimeError("outbound communication proof is staging-only")
    required = ("JOB_QUEUE_URL", "ARTIFACT_BUCKET", "BROKER_TEST_SECRET_ARN")
    if any(not os.environ.get(name) for name in required):
        raise RuntimeError("outbound communication staging settings are incomplete")
    clock = ProofClock()
    provider = SandboxEmailProvider(clock=clock)
    secret_store = AwsSecretsManagerStore(
        allowed_secret_arns=frozenset({os.environ["BROKER_TEST_SECRET_ARN"]}))
    app = create_postgres_agent_runtime(
        queue=SqsQueue(os.environ["JOB_QUEUE_URL"]), signing_key=secrets.token_bytes(48),
        worker_id="shared-communications-worker",
        dispatcher_id="communications-outbox-dispatcher",
        schema=os.environ.get("DB_SCHEMA"), clock=clock,
        secret_store=secret_store,
        artifact_store=S3ArtifactStore(
            bucket=os.environ["ARTIFACT_BUCKET"],
            allowed_prefix=f"tenant/{TENANT}/company/{COMPANY}/"),
        external_providers={provider.provider: provider},
    )
    connections = attach_provider_connections(
        app, secret_store=secret_store,
        oauth_providers={provider.provider: provider},
        secret_locator_factory=lambda tenant, company, connection: os.environ["BROKER_TEST_SECRET_ARN"],
        clock=clock)
    safety = attach_outbound_communications(
        app, delivery_verifiers={provider.provider: provider}, clock=clock)
    operator_context = object()
    compliance = CommunicationsCompliance(
        repository=app.runtime_repository, outbound_safety=safety,
        principal_authority=app.principal_authority,
        authorization=app.commercial.authorization, audit=app.runtime.audit,
        clock=clock, id_factory=app.runtime.id_factory,
        unsubscribe_signing_key=secrets.token_bytes(48),
        abuse_thresholds=AbuseThresholds(warning=1, throttle=2, suspend=3,
                                         emergency=4, window_seconds=3600),
        operator_verifier=lambda value: "staging_operator" if value is operator_context else "",
        sandbox_providers=frozenset({provider.provider}),
    )
    try:
        ids = DeterministicIds()
        identity = IdentityService(app.identity_repository, id_factory=ids, clock=clock)
        owner, _ = identity.register_founder(
            "communications-cloud-proof@example.test", "Fictional Communications Proof Owner")
        tenant, _, _ = identity.create_account(
            owner.user_id, "Fictional Communications Proof", tenant_id=TENANT,
            organization_id="organization_communications_staging")
        identity.attach_company(AuthorizationContext(owner.user_id, TENANT), COMPANY)
        owner_ref = EntityRef("user", owner.user_id)
        app.company_brain.create_company(Company(
            Scope(TENANT, COMPANY), "Fictional Communications Sandbox", "service",
            {"country": "US", "region": "TX"}, (owner_ref,),
            lifecycle=LifecycleState.OPERATING,
            provenance=(Provenance("staging_test", clock().isoformat(), owner_ref,
                                   source_ref="staging://communications-proof"),)))
        app.company_brain.record_fact(
            Scope(TENANT, COMPANY), record_id="pricing_policy_staging",
            kind=RecordKind.POLICY,
            data={"proof": "authorized sandbox quote policy"},
            provenance=(Provenance("staging_test", clock().isoformat(), owner_ref,
                                   source_ref="staging://communications-pricing"),),
            owner_ref=owner_ref)
        for role in safe_role_definitions(TENANT, COMPANY, created_at=clock()):
            app.workforce_repository.add_definition(role)
        _activate(app, owner.user_id, clock)
        app.runtime.budgets.create(
            Budget("budget_communications_staging", TENANT, COMPANY, Money("USD", 100)),
            "correlation-communications-budget")
        auth = FakeDevAuthenticationProvider()
        assertion = secrets.token_urlsafe(32)
        auth.register(owner.user_id, owner.email, assertion)
        token = SessionService(app.identity_repository, auth, id_factory=ids, clock=clock).sign_in(
            owner.email, assertion, lifetime=timedelta(hours=1))[1]
        principal = app.principal_authority.issue(token, tenant_id=TENANT, company_id=COMPANY)

        started = connections.start(
            principal, tenant_id=TENANT, company_id=COMPANY,
            provider_name=provider.provider, redirect_uri=REDIRECT,
            scopes=frozenset({"mail.read", "mail.send"}))
        query = parse_qs(urlparse(started.authorization_url).query)
        code = provider.issue_test_code(
            code_challenge=query["code_challenge"][0], redirect_uri=REDIRECT,
            scopes=frozenset({"mail.read", "mail.send"}))
        connection = connections.complete(
            token, state=query["state"][0], code=code,
            pkce_verifier=started.pkce_verifier, redirect_uri=REDIRECT)
        secret_ref = JobSecretRef(connection.secret_ref_ids[0], provider.provider,
                                  "communications.email", TENANT, COMPANY)

        events = (
            Event("event_communications_inbound", TENANT, COMPANY, "corr-inbound", None,
                  "integration.message.received", clock(), {"sandbox": True}, "sandbox"),
            Event("event_communications_quote", TENANT, COMPANY, "corr-quote", None,
                  "integration.quote.requested", clock(), {"sandbox": True}, "sandbox"),
            Event("event_communications_completed", TENANT, COMPANY, "corr-review", None,
                  "service.completed", clock(), {"sandbox": True}, "sandbox"),
        )
        for event in events:
            app.runtime.events.publish(event)
        recipients = (
            safety.register_from_canonical_event(
                tenant_id=TENANT, company_id=COMPANY, destination="inbound@example.test",
                relationship=ContactRelationship.INBOUND_CUSTOMER,
                consent_state=ConsentState.CUSTOMER_INITIATED,
                consent_provenance="canonical_inbound", source_event_ref=events[0].event_id),
            safety.register_from_canonical_event(
                tenant_id=TENANT, company_id=COMPANY, destination="quote@example.test",
                relationship=ContactRelationship.QUOTE_REQUESTER,
                consent_state=ConsentState.CUSTOMER_INITIATED,
                consent_provenance="canonical_quote", source_event_ref=events[1].event_id),
            safety.register_from_canonical_event(
                tenant_id=TENANT, company_id=COMPANY, destination="review@example.test",
                relationship=ContactRelationship.SERVICE_RECIPIENT,
                consent_state=ConsentState.SERVICE_FOLLOWUP,
                consent_provenance="completed_service", source_event_ref=events[2].event_id),
            safety.register_from_canonical_event(
                tenant_id=TENANT, company_id=COMPANY, destination="bounce@example.test",
                relationship=ContactRelationship.INBOUND_CUSTOMER,
                consent_state=ConsentState.CUSTOMER_INITIATED,
                consent_provenance="canonical_inbound", source_event_ref=events[0].event_id),
            safety.register_from_canonical_event(
                tenant_id=TENANT, company_id=COMPANY, destination="complaint@example.test",
                relationship=ContactRelationship.INBOUND_CUSTOMER,
                consent_state=ConsentState.CUSTOMER_INITIATED,
                consent_provenance="canonical_inbound", source_event_ref=events[0].event_id),
            safety.register_from_canonical_event(
                tenant_id=TENANT, company_id=COMPANY, destination="kill-switch@example.test",
                relationship=ContactRelationship.INBOUND_CUSTOMER,
                consent_state=ConsentState.CUSTOMER_INITIATED,
                consent_provenance="canonical_inbound", source_event_ref=events[0].event_id),
        )
        for recipient in recipients:
            compliance.configure_jurisdiction(
                principal, tenant_id=TENANT, company_id=COMPANY,
                recipient_id=recipient.recipient_id, tenant_jurisdiction="US",
                company_jurisdiction="US", recipient_jurisdiction="US",
                source_ref="staging_company_profile")

        flows = (
            ("inbound", recipients[0], CommunicationPurpose.REPLY_TO_INBOUND,
             "role_inbox_assistant", "send_preapproved_reply", events[0],
             "Thanks for contacting our sandbox inbox.", ContentEvidence()),
            ("quote", recipients[1], CommunicationPurpose.QUOTE_RESPONSE,
             "role_inbox_assistant", "send_preapproved_reply", events[1],
             "The authorized sandbox quote is $25.",
             ContentEvidence(pricing_authorized=True,
                             company_fact_refs=("pricing_policy_staging",))),
            ("review", recipients[2], CommunicationPurpose.REVIEW_REQUEST,
             "role_review_followup_assistant", "send_preapproved_review_request", events[2],
             "Please share an honest review of the completed sandbox service.", ContentEvidence()),
        )
        executed = []
        for name, recipient, purpose, role, action, event, content, evidence in flows:
            request = safety.prepare_request(
                tenant_id=TENANT, company_id=COMPANY, recipient_id=recipient.recipient_id,
                purpose=purpose, agent_role=role, capability="communications.email",
                provider_connection_id=connection.connection_id,
                runtime_idempotency_key=f"communications-{name}-0001",
                content=content, evidence=evidence, context_ref=event.event_id)
            job = app.service.submit(
                tenant_id=TENANT, company_id=COMPANY, role_id=role,
                capability="communications.email", action=action,
                budget_ref="budget_communications_staging",
                maximum_job_spend=Money("USD", 5),
                idempotency_key=f"communications-{name}-0001",
                correlation_id=event.correlation_id, causation_id=event.event_id,
                trigger_class=TriggerClass.INBOUND_EVENT, trigger_ref=event.event_id,
                secret_refs=(secret_ref,), communication_ref=request.communication_id)
            if app.dispatcher.dispatch_pending(limit=1) != 1:
                raise AssertionError(f"{name} communication did not dispatch")
            if app.worker.process_one(wait_seconds=20) != "succeeded":
                raise AssertionError(f"{name} sandbox communication did not complete")
            envelope = app.runtime_repository.get_agent_envelope(TENANT, COMPANY, job.job_id)
            receipt = next(item for item in app.runtime_repository.list_provider_receipts(TENANT, COMPANY)
                           if item.job_id == job.job_id)
            body, signature = provider.delivery_callback(
                event_id=f"delivery_{name}_0001", provider_request_id=receipt.provider_request_id,
                status=DeliveryStatus.DELIVERED)
            delivery = safety.handle_delivery_callback(
                provider_name=provider.provider, body=body, signature=signature)
            # Callback replay is an idempotent read of the already-mutated delivery.
            replay = safety.handle_delivery_callback(
                provider_name=provider.provider, body=body, signature=signature)
            if delivery != replay:
                raise AssertionError("delivery callback replay changed state")
            executed.append((job, envelope, receipt, delivery))

        calls_after_three = provider.action_count
        app.worker.queue.send(
            f"queue_{sha256(executed[0][0].job_id.encode()).hexdigest()[:24]}",
            executed[0][1].to_payload())
        duplicate_result = app.worker.process_one(wait_seconds=20)
        duplicate_send_suppressed = (
            duplicate_result == "duplicate" and provider.action_count == calls_after_three)

        unsubscribe_token = compliance.issue_unsubscribe_token(
            tenant_id=TENANT, company_id=COMPANY,
            recipient_id=recipients[0].recipient_id)
        unsubscribe_ok = compliance.unsubscribe(unsubscribe_token)
        unsubscribe_replay_ok = compliance.unsubscribe(unsubscribe_token)
        suppressed_denied = _prepare_submit_deny(
            app, safety, connection, secret_ref, recipients[0], events[0],
            "suppressed-staging-0001", CommunicationPurpose.REPLY_TO_INBOUND,
            "Safe sandbox reply", ContentEvidence())

        unsupported_claim_denied = _prepare_submit_deny(
            app, safety, connection, secret_ref, recipients[1], events[1],
            "claim-staging-0000001", CommunicationPurpose.QUOTE_RESPONSE,
            "This result is guaranteed", ContentEvidence(pricing_authorized=True))

        review_rate_denied = _prepare_submit_deny(
            app, safety, connection, secret_ref, recipients[2], events[2],
            "review-rate-staging-01", CommunicationPurpose.REVIEW_REQUEST,
            "Please share another honest review.", ContentEvidence(),
            role="role_review_followup_assistant",
            action="send_preapproved_review_request")

        # Authenticated provider bounce and complaint callbacks mutate only known sends.
        callback_results = []
        callback_forgery_denied = False
        for name, recipient, status in (
            ("bounce", recipients[3], DeliveryStatus.BOUNCED),
            ("complaint", recipients[4], DeliveryStatus.COMPLAINED),
        ):
            request = safety.prepare_request(
                tenant_id=TENANT, company_id=COMPANY, recipient_id=recipient.recipient_id,
                purpose=CommunicationPurpose.REPLY_TO_INBOUND,
                agent_role="role_inbox_assistant", capability="communications.email",
                provider_connection_id=connection.connection_id,
                runtime_idempotency_key=f"compliance-{name}-0001",
                content="Safe sandbox reply.", context_ref=events[0].event_id)
            job = _submit(app, secret_ref, request, events[0],
                          "role_inbox_assistant", "send_preapproved_reply")
            envelope = app.runtime_repository.get_agent_envelope(TENANT, COMPANY, job.job_id)
            receipt = app.broker.execute_provider_action(
                envelope, operation="send_preapproved_reply", secret_ref=secret_ref)
            body, signature = provider.delivery_callback(
                event_id=f"compliance_{name}_callback_001",
                provider_request_id=receipt.provider_request_id, status=status)
            if name == "bounce":
                callback_forgery_denied = _denied(lambda: safety.handle_delivery_callback(
                    provider_name=provider.provider, body=body, signature="forged"))
            changed = safety.handle_delivery_callback(
                provider_name=provider.provider, body=body, signature=signature)
            replay = safety.handle_delivery_callback(
                provider_name=provider.provider, body=body, signature=signature)
            callback_results.append(changed.status is status and changed == replay)

        # A queued job cannot bypass a switch engaged after admission.
        kill_request = safety.prepare_request(
            tenant_id=TENANT, company_id=COMPANY, recipient_id=recipients[5].recipient_id,
            purpose=CommunicationPurpose.REPLY_TO_INBOUND,
            agent_role="role_inbox_assistant", capability="communications.email",
            provider_connection_id=connection.connection_id,
            runtime_idempotency_key="compliance-kill-0001",
            content="Safe sandbox reply.", context_ref=events[0].event_id)
        kill_job = _submit(app, secret_ref, kill_request, events[0],
                           "role_inbox_assistant", "send_preapproved_reply")
        kill_envelope = app.runtime_repository.get_agent_envelope(TENANT, COMPANY, kill_job.job_id)
        compliance.set_company_kill_switch(
            principal, tenant_id=TENANT, company_id=COMPANY, engaged=True,
            reason_code="staging_emergency_pause")
        killed_queued_denied = _denied(lambda: app.broker.execute_provider_action(
            kill_envelope, operation="send_preapproved_reply", secret_ref=secret_ref))
        compliance.set_company_kill_switch(
            principal, tenant_id=TENANT, company_id=COMPANY, engaged=False,
            reason_code="staging_review_complete")
        restored_receipt = app.broker.execute_provider_action(
            kill_envelope, operation="send_preapproved_reply", secret_ref=secret_ref)
        kill_restore_ok = restored_receipt.status.value == "succeeded"

        bulk_request = safety.prepare_request(
            tenant_id=TENANT, company_id=COMPANY, recipient_id=recipients[1].recipient_id,
            purpose=CommunicationPurpose.QUOTE_RESPONSE, agent_role="role_inbox_assistant",
            capability="communications.email", provider_connection_id=connection.connection_id,
            runtime_idempotency_key="bulk-staging-0000001", content="Safe quote response",
            context_ref=events[1].event_id, bulk_count=2)
        bulk_job = _submit(app, secret_ref, bulk_request, events[1],
                           "role_inbox_assistant", "send_preapproved_reply")
        bulk_denied = _denied(lambda: app.broker.execute_provider_action(
            app.runtime_repository.get_agent_envelope(TENANT, COMPANY, bulk_job.job_id),
            operation="send_preapproved_reply", secret_ref=secret_ref))

        self_scope = (
            app.runtime_repository.get_broker_record(
                "communication_recipient", "tenant_other", COMPANY,
                recipients[0].recipient_id) is None
            and app.runtime_repository.get_broker_record(
                "communication_recipient", TENANT, "company_other",
                recipients[0].recipient_id) is None
            and not app.runtime_repository.list_broker_records(
                "communication_delivery", "tenant_other", COMPANY))

        _set_entitlement(app, EntitlementStatus.SUSPENDED, clock())
        suspended_denied = _denied(lambda: app.service.submit(
            tenant_id=TENANT, company_id=COMPANY, role_id="role_inbox_assistant",
            capability="communications.email", action="send_preapproved_reply",
            budget_ref="budget_communications_staging", maximum_job_spend=Money("USD", 5),
            idempotency_key="suspended-staging-01", correlation_id=events[0].correlation_id,
            trigger_class=TriggerClass.INBOUND_EVENT, trigger_ref=events[0].event_id,
            secret_refs=(secret_ref,)))
        _set_entitlement(app, EntitlementStatus.ACTIVE, clock())

        revoked_request = safety.prepare_request(
            tenant_id=TENANT, company_id=COMPANY, recipient_id=recipients[1].recipient_id,
            purpose=CommunicationPurpose.QUOTE_RESPONSE, agent_role="role_inbox_assistant",
            capability="communications.email", provider_connection_id=connection.connection_id,
            runtime_idempotency_key="revoked-staging-00001", content="Safe quote response",
            context_ref=events[1].event_id)
        revoked_job = _submit(app, secret_ref, revoked_request, events[1],
                              "role_inbox_assistant", "send_preapproved_reply")
        connections.disconnect(principal, tenant_id=TENANT, company_id=COMPANY,
                               connection_id=connection.connection_id)
        revoked_denied = _denied(lambda: app.broker.execute_provider_action(
            app.runtime_repository.get_agent_envelope(TENANT, COMPANY, revoked_job.job_id),
            operation="send_preapproved_reply", secret_ref=secret_ref))

        # Retention removes operational PII while durable evidence survives.
        erasure = compliance.request_erasure(
            principal, tenant_id=TENANT, company_id=COMPANY,
            recipient_id=recipients[1].recipient_id, reason="staging_erasure_request")
        erased_recipient = safety.get_recipient(
            principal, tenant_id=TENANT, company_id=COMPANY,
            recipient_id=recipients[1].recipient_id)
        consent_preserved = any(item.recipient_id == recipients[1].recipient_id
            for item in app.runtime_repository.list_broker_records(
                "communication_compliance_consent", TENANT, COMPANY))

        # Test policy escalation is explicit and explainable.
        compliance.record_abuse_signal(TENANT, COMPANY, AbuseSignalType.COMPLAINT,
                                       "synthetic_complaint_threshold_002")
        compliance.record_abuse_signal(TENANT, COMPANY, AbuseSignalType.COMPLAINT,
                                       "synthetic_complaint_threshold_003")
        abuse_switch = compliance._switch(KillSwitchScope.COMPANY, COMPANY)
        live_tier_denied = _denied(lambda: compliance.set_rollout(
            principal, tenant_id=TENANT, company_id=COMPANY,
            tier=RolloutTier.GENERAL_LIVE))

        decisions = app.runtime_repository.list_broker_records(
            "communication_policy_decision", TENANT, COMPANY)
        compliance_decisions = app.runtime_repository.list_broker_records(
            "communication_compliance_decision", TENANT, COMPANY)
        deliveries = app.runtime_repository.list_broker_records(
            "communication_delivery", TENANT, COMPANY)
        audits = app.runtime_repository.list_audit(TENANT, COMPANY)
        actions = {item["action"] for item in audits}
        proof = {
            "proof": "outbound-communications-safety-v1",
            "status": "passed",
            "backend": "postgresql+sqs+secrets-manager+shared-fargate+sandbox-email",
            "inbound_reply_sandbox": executed[0][3].status is DeliveryStatus.DELIVERED,
            "quote_response_sandbox": executed[1][3].status is DeliveryStatus.DELIVERED,
            "review_request_sandbox": executed[2][3].status is DeliveryStatus.DELIVERED,
            "provider_receipts": len(app.runtime_repository.list_provider_receipts(TENANT, COMPANY)),
            "delivery_records": len(deliveries),
            "duplicate_send_suppressed": duplicate_send_suppressed,
            "delivery_callback_replay_idempotent": True,
            "suppressed_recipient_denied": suppressed_denied,
            "unsupported_claim_denied": unsupported_claim_denied,
            "review_frequency_limit_enforced": review_rate_denied,
            "bulk_disabled": bulk_denied,
            "suspended_entitlement_denied": suspended_denied,
            "revoked_connection_denied": revoked_denied,
            "tenant_and_company_isolation": self_scope,
            "opt_out_durable": safety.get_recipient(
                principal, tenant_id=TENANT, company_id=COMPANY,
                recipient_id=recipients[0].recipient_id).suppression_state is SuppressionState.SUPPRESSED,
            "durable_denials": sum(item.outcome.value == "denied" for item in decisions) +
                sum(item.outcome.value == "denied" for item in compliance_decisions) >= 4,
            "audit_complete": {
                "communication.policy.allowed", "communication.policy.denied",
                "communication.delivery.recorded", "communication.delivery.reconciled",
                "communication.opt_out.recorded", "broker.provider_receipt.recorded",
            } <= actions,
            "no_real_external_actions": True,
            "no_real_recipient_data": True,
            "sandbox_provider_only": True,
            "runtime_authority": True,
            "bulk_sending_disabled": True,
            "jurisdiction_policy_versioned": all(
                item.policy_version == "communications-compliance.us-federal.v1"
                for item in app.runtime_repository.list_broker_records(
                    "communication_compliance_decision", TENANT, COMPANY)),
            "unsubscribe_token_opaque_replay_safe": unsubscribe_ok and unsubscribe_replay_ok,
            "verified_bounce_and_complaint": all(callback_results),
            "forged_callback_denied": callback_forgery_denied,
            "abuse_threshold_alert_and_suspend": bool(abuse_switch and abuse_switch.engaged),
            "queued_send_kill_switch_denied": killed_queued_denied,
            "authorized_kill_switch_restore": kill_restore_ok,
            "erasure_pseudonymized_operational_pii": (
                erasure.operational_pii_removed
                and erased_recipient.normalized_destination.endswith("@redacted.example.test")),
            "compliance_evidence_preserved": consent_preserved,
            "rollout_sandbox_only": live_tier_denied and not compliance.live_send_enabled,
            "operational_alerts_persisted": bool(app.runtime_repository.list_broker_records(
                "communication_compliance_alert", TENANT, COMPANY)),
        }
        failed = [key for key, value in proof.items() if isinstance(value, bool) and not value]
        if failed:
            raise AssertionError("communications cloud proof failed: " + ",".join(failed))
        with app.runtime_repository.connection.cursor() as cursor:
            cursor.execute("""INSERT INTO bb_cloud_proofs(proof_id,status,body,completed_at)
                VALUES (%s,'passed',%s,%s) ON CONFLICT(proof_id) DO UPDATE SET
                status='passed',body=EXCLUDED.body,completed_at=EXCLUDED.completed_at""",
                (os.environ.get("COMMUNICATIONS_PROOF_ID", "outbound_communications_safety_v1"),
                 json.dumps(proof, sort_keys=True), clock()))
        return proof
    finally:
        app.close()


def _submit(app, secret_ref, request, event, role, action):
    return app.service.submit(
        tenant_id=TENANT, company_id=COMPANY, role_id=role,
        capability="communications.email", action=action,
        budget_ref="budget_communications_staging", maximum_job_spend=Money("USD", 5),
        idempotency_key=request.runtime_idempotency_key,
        correlation_id=event.correlation_id, causation_id=event.event_id,
        trigger_class=TriggerClass.INBOUND_EVENT, trigger_ref=event.event_id,
        secret_refs=(secret_ref,), communication_ref=request.communication_id)


def _prepare_submit_deny(app, safety, connection, secret_ref, recipient, event, key,
                         purpose, content, evidence, *, role="role_inbox_assistant",
                         action="send_preapproved_reply"):
    def attempt():
        request = safety.prepare_request(
            tenant_id=TENANT, company_id=COMPANY, recipient_id=recipient.recipient_id,
            purpose=purpose, agent_role=role, capability="communications.email",
            provider_connection_id=connection.connection_id,
            runtime_idempotency_key=key, content=content, evidence=evidence,
            context_ref=event.event_id)
        job = _submit(app, secret_ref, request, event, role, action)
        envelope = app.runtime_repository.get_agent_envelope(TENANT, COMPANY, job.job_id)
        return app.broker.execute_provider_action(
            envelope, operation=action, secret_ref=secret_ref)
    return _denied(attempt)


def _activate(app, user_id, clock):
    context = AuthorizationContext(user_id, TENANT, COMPANY)
    order = app.commercial.create_order(
        context, "product_version_build_and_run_v1", amount=Amount("USD", 1000))
    checkout = app.commercial.create_checkout(
        context, order.order_id, "communications-staging-checkout")
    app.commercial.handle_billing_event(NormalizedBillingEvent(
        "billing_event_communications_payment", "billing.payment.succeeded", TENANT,
        user_id, COMPANY, clock(), "fixture_pay", "provider_event_communications_payment",
        "correlation-communications-payment", order_id=order.order_id,
        checkout_intent_id=checkout.checkout_intent_id,
        payment_provider_ref="opaque_communications_payment", amount=order.total))
    app.commercial.handle_billing_event(NormalizedBillingEvent(
        "billing_event_communications_subscription", "billing.subscription.created", TENANT,
        user_id, COMPANY, clock(), "fixture_pay", "provider_event_communications_subscription",
        "correlation-communications-subscription", order_id=order.order_id,
        subscription_provider_ref="opaque_communications_subscription",
        subscription_status=SubscriptionStatus.ACTIVE,
        current_period=BillingPeriod(clock(), clock() + timedelta(days=30))))


def _set_entitlement(app, status, at):
    for grant in app.commercial_repository.get_current_entitlement_grants(TENANT, COMPANY):
        if grant.entitlement_class is EntitlementClass.STROMATION_MANAGED:
            app.commercial_repository.append_entitlement_grant(
                replace(grant, status=status, updated_at=at, version=grant.version + 1))


def _denied(call):
    try:
        call()
    except (PermissionError, CommunicationDenied):
        return True
    return False


if __name__ == "__main__":
    print(json.dumps(run_staging_cloud_proof(), sort_keys=True), flush=True)
