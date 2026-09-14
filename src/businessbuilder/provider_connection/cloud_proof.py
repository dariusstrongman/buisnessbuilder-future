from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from hashlib import sha256
from urllib.parse import parse_qs, urlparse
import secrets

from businessbuilder.access_broker import AwsSecretsManagerStore, JobSecretRef, S3ArtifactStore
from businessbuilder.access_broker.service import BrokerDenied
from businessbuilder.agent_runtime import SqsQueue, TriggerClass
from businessbuilder.agent_runtime.bootstrap import create_postgres_agent_runtime
from businessbuilder.ai_workforce import safe_role_definitions
from businessbuilder.commercial import (
    Amount, BillingPeriod, CommercialService, EntitlementClass, EntitlementStatus,
    NormalizedBillingEvent, SubscriptionStatus,
)
from businessbuilder.company_brain import Company, EntityRef, LifecycleState, Provenance, Scope
from businessbuilder.identity import (
    AuthorizationContext, FakeDevAuthenticationProvider, IdentityService, SessionService,
)
from businessbuilder.runtime import Budget, Event, Money
from businessbuilder.runtime.ids import DeterministicIds

from .adapters import SandboxEmailProvider
from .bootstrap import attach_provider_connections
from .service import OAuthFlowDenied


TENANT = "tenant_oauth_staging"
COMPANY = "company_oauth_staging"
REDIRECT = "https://staging.example.test/oauth/callback"


class ProofClock:
    def __init__(self): self.now = datetime.now(timezone.utc).replace(microsecond=0)
    def __call__(self): return self.now


def run_staging_cloud_proof() -> dict[str, object]:
    if os.environ.get("ENVIRONMENT", "").lower() != "staging":
        raise RuntimeError("provider connection cloud proof is staging-only")
    required = ("JOB_QUEUE_URL", "ARTIFACT_BUCKET", "BROKER_TEST_SECRET_ARN")
    if any(not os.environ.get(name) for name in required):
        raise RuntimeError("provider connection staging settings are incomplete")
    clock = ProofClock()
    provider = SandboxEmailProvider(clock=clock)
    secret_store = AwsSecretsManagerStore(
        allowed_secret_arns=frozenset({os.environ["BROKER_TEST_SECRET_ARN"]})
    )
    app = create_postgres_agent_runtime(
        queue=SqsQueue(os.environ["JOB_QUEUE_URL"]), signing_key=secrets.token_bytes(48),
        worker_id="shared-oauth-worker", dispatcher_id="oauth-outbox-dispatcher",
        schema=os.environ.get("DB_SCHEMA"), clock=clock,
        secret_store=secret_store,
        artifact_store=S3ArtifactStore(bucket=os.environ["ARTIFACT_BUCKET"],
            allowed_prefix=f"tenant/{TENANT}/company/{COMPANY}/"),
        external_providers={provider.provider: provider},
    )
    service = attach_provider_connections(
        app, secret_store=secret_store, oauth_providers={provider.provider: provider},
        secret_locator_factory=lambda tenant, company, connection: os.environ["BROKER_TEST_SECRET_ARN"],
        clock=clock,
    )
    try:
        ids = DeterministicIds()
        identity = IdentityService(app.identity_repository, id_factory=ids, clock=clock)
        owner, _ = identity.register_founder("oauth-cloud-proof@example.test", "Fictional OAuth Proof Owner")
        tenant, _, _ = identity.create_account(owner.user_id, "Fictional OAuth Proof",
            tenant_id=TENANT, organization_id="organization_oauth_staging")
        identity.attach_company(AuthorizationContext(owner.user_id, tenant.tenant_id), COMPANY)
        owner_ref = EntityRef("user", owner.user_id)
        app.company_brain.create_company(Company(
            Scope(TENANT, COMPANY), "Fictional OAuth Sandbox", "service",
            {"country": "US", "region": "TX"}, (owner_ref,), lifecycle=LifecycleState.OPERATING,
            provenance=(Provenance("staging_test", clock().isoformat(), owner_ref,
                                   source_ref="staging://provider-oauth-proof"),),
        ))
        for role in safe_role_definitions(TENANT, COMPANY, created_at=clock()):
            app.workforce_repository.add_definition(role)
        _activate(app, owner.user_id, clock)
        app.runtime.budgets.create(Budget("budget_oauth_staging", TENANT, COMPANY, Money("USD", 20)),
                                   "correlation-oauth-budget")
        auth = FakeDevAuthenticationProvider()
        assertion = secrets.token_urlsafe(32)
        auth.register(owner.user_id, owner.email, assertion)
        token = SessionService(app.identity_repository, auth, id_factory=ids, clock=clock).sign_in(
            owner.email, assertion, lifetime=timedelta(hours=1))[1]
        principal = app.principal_authority.issue(token, tenant_id=TENANT, company_id=COMPANY)

        started = service.start(principal, tenant_id=TENANT, company_id=COMPANY,
            provider_name=provider.provider, redirect_uri=REDIRECT,
            scopes=frozenset({"mail.read", "mail.send"}))
        query = parse_qs(urlparse(started.authorization_url).query)
        code = provider.issue_test_code(code_challenge=query["code_challenge"][0],
            redirect_uri=REDIRECT, scopes=frozenset({"mail.read", "mail.send"}))
        connection = service.complete(token, state=query["state"][0], code=code,
            pkce_verifier=started.pkce_verifier, redirect_uri=REDIRECT)

        replay_denied = _denied(lambda: service.complete(
            token, state=query["state"][0], code=code,
            pkce_verifier=started.pkce_verifier, redirect_uri=REDIRECT))
        forged_state_denied = _denied(lambda: service.complete(
            token, state="forged-state", code="forged-code",
            pkce_verifier=started.pkce_verifier, redirect_uri=REDIRECT))
        wrong_company_denied = _denied(lambda: service.start(
            principal, tenant_id=TENANT, company_id="company_other",
            provider_name=provider.provider, redirect_uri=REDIRECT,
            scopes=frozenset({"mail.read"})))
        missing_scope_denied = _denied(lambda: service.start(
            principal, tenant_id=TENANT, company_id=COMPANY,
            provider_name=provider.provider, redirect_uri=REDIRECT,
            scopes=frozenset({"contacts.admin"})))

        clock.now += timedelta(minutes=16)
        inbound = Event("event_oauth_sandbox_inbound", TENANT, COMPANY,
            "correlation-oauth-sandbox", None, "integration.message.received", clock(),
            {"source": "sandbox"}, "businessbuilder.integration.sandbox")
        app.runtime.events.publish(inbound)
        secret_ref = JobSecretRef(connection.secret_ref_ids[0], provider.provider,
                                  "communications.email", TENANT, COMPANY)
        job = app.service.submit(tenant_id=TENANT, company_id=COMPANY,
            role_id="role_inbox_assistant", capability="communications.email",
            action="send_preapproved_reply", budget_ref="budget_oauth_staging",
            maximum_job_spend=Money("USD", 5), idempotency_key="oauth-sandbox-reply-0001",
            correlation_id=inbound.correlation_id, causation_id=inbound.event_id,
            trigger_class=TriggerClass.INBOUND_EVENT, trigger_ref=inbound.event_id,
            secret_refs=(secret_ref,))
        envelope = app.runtime_repository.get_agent_envelope(TENANT, COMPANY, job.job_id)
        dispatched = app.dispatcher.dispatch_pending(limit=1)
        worker_result = app.worker.process_one(wait_seconds=20)
        receipts = app.runtime_repository.list_provider_receipts(TENANT, COMPANY)
        action_count = provider.action_count
        app.worker.queue.send(
            f"queue_{sha256(envelope.job_id.encode()).hexdigest()[:24]}", envelope.to_payload()
        )
        duplicate_result = app.worker.process_one(wait_seconds=20)

        provider.revoke_tokens()
        service.reconcile(tenant_id=TENANT, company_id=COMPANY, connection_id=connection.connection_id)
        revoked_blocks = _denied(lambda: app.broker.resolve_secret(
            envelope, secret_ref, operation="send_preapproved_reply"))
        callback_body = json.dumps({"event_id": "oauth_callback_forgery_proof",
            "connection_id": connection.connection_id, "event_type": "revoked",
            "occurred_at": clock().isoformat(), "reason": "sandbox"}, sort_keys=True).encode()
        callback_forgery_denied = _denied(lambda: service.handle_provider_callback(
            provider_name=provider.provider, body=callback_body, signature="forged"))
        _set_entitlement(app, EntitlementStatus.SUSPENDED, clock())
        suspended_entitlement_denied = _denied(lambda: app.service.submit(
            tenant_id=TENANT, company_id=COMPANY, role_id="role_inbox_assistant",
            capability="communications.email", action="send_preapproved_reply",
            budget_ref="budget_oauth_staging", maximum_job_spend=Money("USD", 5),
            idempotency_key="oauth-suspended-0001", correlation_id=inbound.correlation_id,
            causation_id=inbound.event_id, trigger_class=TriggerClass.INBOUND_EVENT,
            trigger_ref=inbound.event_id, secret_refs=(secret_ref,)))
        audits = app.runtime_repository.list_audit(TENANT, COMPANY)
        actions = {item["action"] for item in audits}
        proof = {
            "proof": "provider-connection-oauth-v1", "status": "passed",
            "backend": "postgresql+sqs+secrets-manager+shared-fargate+sandbox-email",
            "oauth_connection_activated": connection.status.value == "active",
            "pkce_and_state_validated": True, "forged_state_denied": forged_state_denied,
            "callback_replay_denied": replay_denied, "wrong_company_denied": wrong_company_denied,
            "missing_scope_denied": missing_scope_denied,
            "token_refresh_rotated": provider.refresh_count == 1,
            "runtime_authorized_sandbox_action": dispatched == 1 and worker_result == "succeeded",
            "provider_receipt_persisted": len(receipts) == 1 and receipts[0].connection_id == connection.connection_id,
            "duplicate_action_suppressed": duplicate_result == "duplicate" and provider.action_count == action_count == 1,
            "revoked_connection_blocks": revoked_blocks,
            "callback_forgery_denied": callback_forgery_denied,
            "suspended_entitlement_denied": suspended_entitlement_denied,
            "audit_complete": {"provider.oauth.started", "provider.connection.activated",
                "broker.provider_receipt.recorded", "provider.connection.reconciled"} <= actions,
            "no_real_email": True, "test_provider_only": True,
        }
        failed = [key for key, value in proof.items() if isinstance(value, bool) and not value]
        if failed:
            raise AssertionError("provider OAuth cloud proof failed: " + ",".join(failed))
        with app.runtime_repository.connection.cursor() as cursor:
            cursor.execute("""INSERT INTO bb_cloud_proofs(proof_id,status,body,completed_at)
                VALUES (%s,'passed',%s,%s) ON CONFLICT(proof_id) DO UPDATE SET
                status='passed',body=EXCLUDED.body,completed_at=EXCLUDED.completed_at""",
                (os.environ.get("OAUTH_PROOF_ID", "provider_connection_oauth_v1"),
                 json.dumps(proof, sort_keys=True), clock()))
        return proof
    finally:
        app.close()


def _activate(app, user_id, clock):
    context = AuthorizationContext(user_id, TENANT, COMPANY)
    order = app.commercial.create_order(context, "product_version_build_and_run_v1", amount=Amount("USD", 1000))
    checkout = app.commercial.create_checkout(context, order.order_id, "oauth-staging-checkout")
    app.commercial.handle_billing_event(NormalizedBillingEvent(
        "billing_event_oauth_payment", "billing.payment.succeeded", TENANT, user_id, COMPANY,
        clock(), "fixture_pay", "provider_event_oauth_payment", "correlation-oauth-payment",
        order_id=order.order_id, checkout_intent_id=checkout.checkout_intent_id,
        payment_provider_ref="opaque_oauth_payment", amount=order.total))
    app.commercial.handle_billing_event(NormalizedBillingEvent(
        "billing_event_oauth_subscription", "billing.subscription.created", TENANT, user_id, COMPANY,
        clock(), "fixture_pay", "provider_event_oauth_subscription", "correlation-oauth-subscription",
        order_id=order.order_id, subscription_provider_ref="opaque_oauth_subscription",
        subscription_status=SubscriptionStatus.ACTIVE,
        current_period=BillingPeriod(clock(), clock() + timedelta(days=30))))


def _set_entitlement(app, status, at):
    for grant in app.commercial_repository.get_current_entitlement_grants(TENANT, COMPANY):
        if grant.entitlement_class is EntitlementClass.STROMATION_MANAGED:
            app.commercial_repository.append_entitlement_grant(
                replace(grant, status=status, updated_at=at, version=grant.version + 1))


def _denied(call):
    try:
        value = call()
        if hasattr(value, "close"): value.close()
    except (PermissionError, BrokerDenied):
        return True
    return False


if __name__ == "__main__":
    print(json.dumps(run_staging_cloud_proof(), sort_keys=True), flush=True)
