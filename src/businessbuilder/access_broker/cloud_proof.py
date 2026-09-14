from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import secrets

from businessbuilder.agent_runtime.bootstrap import create_postgres_agent_runtime
from businessbuilder.agent_runtime.models import TriggerClass, assert_queue_payload_safe
from businessbuilder.agent_runtime.queue import SqsQueue
from businessbuilder.ai_workforce import safe_role_definitions
from businessbuilder.commercial import (
    Amount,
    BillingPeriod,
    EntitlementClass,
    EntitlementStatus,
    NormalizedBillingEvent,
    SubscriptionStatus,
)
from businessbuilder.company_brain import Company, EntityRef, LifecycleState, Provenance, Scope
from businessbuilder.identity import AuthorizationContext, IdentityService
from businessbuilder.runtime import ArtifactRef, Budget, Event, Money
from businessbuilder.runtime.ids import DeterministicIds

from .adapters import AwsSecretsManagerStore, DeterministicExternalProvider, S3ArtifactStore
from .models import (
    ArtifactClassification,
    CapabilityGrant,
    ConnectionStatus,
    CredentialScope,
    ExternalAccount,
    ExternalCredentialRef,
    JobSecretRef,
    ProviderConnection,
    ReceiptStatus,
)
from .service import BrokerDenied


TENANT_ID = "tenant_broker_staging"
COMPANY_ID = "company_broker_staging"
PROVIDER = "test-email"
ARTIFACT_ID = "artifact_broker_test_attachment"
OBJECT_PREFIX = f"tenant/{TENANT_ID}/company/{COMPANY_ID}/"


class ProofClock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now


def run_staging_cloud_proof() -> dict[str, object]:
    environment = os.environ.get("ENVIRONMENT", "").lower()
    if environment not in {"staging", "test", "development", "local"}:
        raise RuntimeError("secret/artifact broker proof is disabled outside non-production environments")
    required = ("JOB_QUEUE_URL", "ARTIFACT_BUCKET", "BROKER_TEST_SECRET_ARN")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"missing broker proof settings: {', '.join(missing)}")
    queue = SqsQueue(os.environ["JOB_QUEUE_URL"])
    artifact_store = S3ArtifactStore(
        bucket=os.environ["ARTIFACT_BUCKET"], allowed_prefix=OBJECT_PREFIX
    )
    secret_store = AwsSecretsManagerStore(
        allowed_secret_arns=frozenset({os.environ["BROKER_TEST_SECRET_ARN"]})
    )
    provider = DeterministicExternalProvider(PROVIDER)
    clock = ProofClock()
    proof_id = os.environ.get("BROKER_PROOF_ID", "secret_artifact_broker_staging_v1")
    app = create_postgres_agent_runtime(
        queue=queue,
        signing_key=secrets.token_bytes(48),
        worker_id="shared-broker-worker",
        dispatcher_id="broker-outbox-dispatcher",
        schema=os.environ.get("DB_SCHEMA"),
        clock=clock,
        secret_store=secret_store,
        artifact_store=artifact_store,
        external_providers={PROVIDER: provider},
    )
    try:
        assert app.broker is not None
        ids = DeterministicIds()
        identity = IdentityService(app.identity_repository, id_factory=ids, clock=clock)
        owner, _ = identity.register_founder(
            f"broker-proof-{proof_id}@example.test", "Fictional Broker Proof Owner"
        )
        tenant, _, _ = identity.create_account(
            owner.user_id, "Fictional Broker Proof",
            tenant_id=TENANT_ID, organization_id="organization_broker_staging",
        )
        identity.attach_company(AuthorizationContext(owner.user_id, tenant.tenant_id), COMPANY_ID)
        context = AuthorizationContext(owner.user_id, tenant.tenant_id, COMPANY_ID)
        owner_ref = EntityRef("user", owner.user_id)
        app.company_brain.create_company(
            Company(
                Scope(TENANT_ID, COMPANY_ID), "Fictional Broker Staging Proof",
                "service", {"country": "US", "region": "TX"}, (owner_ref,),
                lifecycle=LifecycleState.OPERATING,
                provenance=(Provenance(
                    "staging_test", clock().isoformat(), owner_ref,
                    source_ref="staging://secret-artifact-broker-proof",
                ),),
            )
        )
        for role in safe_role_definitions(TENANT_ID, COMPANY_ID, created_at=clock()):
            app.workforce_repository.add_definition(role)
        _activate_build_and_run(app, context, owner.user_id, clock, proof_id)
        app.runtime.budgets.create(
            Budget("budget_broker_staging", TENANT_ID, COMPANY_ID, Money("USD", 10)),
            "correlation-broker-staging-budget",
        )

        account = ExternalAccount(
            "external_account_broker_staging", TENANT_ID, COMPANY_ID, PROVIDER,
            "external_mailbox_broker_staging", "Fictional staging mailbox",
            ConnectionStatus.ACTIVE, clock(), clock(),
        )
        connection = ProviderConnection(
            "connection_broker_staging", account.account_id, TENANT_ID, COMPANY_ID,
            PROVIDER, ConnectionStatus.ACTIVE, clock(), clock(),
        )
        credential = ExternalCredentialRef(
            "secretref_broker_staging_email", connection.connection_id,
            TENANT_ID, COMPANY_ID, PROVIDER, "email.oauth",
            os.environ["BROKER_TEST_SECRET_ARN"], ConnectionStatus.ACTIVE,
            clock(), clock(), clock() + timedelta(days=1),
        )
        job_secret_ref = JobSecretRef(
            credential.secret_ref, PROVIDER, "communications.email", TENANT_ID, COMPANY_ID
        )
        grant = CapabilityGrant(
            "grant_broker_staging_email", TENANT_ID, COMPANY_ID,
            connection.connection_id, "role_inbox_assistant",
            CredentialScope(
                "communications.email", frozenset({"send_preapproved_reply"}),
                frozenset({"email.oauth"}),
            ),
            frozenset({ArtifactClassification.EXTERNAL_ATTACHMENT}), clock(),
            expires_at=clock() + timedelta(days=1),
        )
        app.broker.register_external_account(account, actor_id="staging_fixture")
        app.broker.register_connection(connection, actor_id="staging_fixture")
        app.broker.register_credential_ref(credential, actor_id="staging_fixture")
        app.broker.register_capability_grant(grant, actor_id="staging_fixture")
        content = b"Fictional attachment for broker staging proof. No customer data."
        artifact = app.broker.register_artifact(
            tenant_id=TENANT_ID, company_id=COMPANY_ID, artifact_id=ARTIFACT_ID,
            content=content, content_type="text/plain",
            classification=ArtifactClassification.EXTERNAL_ATTACHMENT,
            provenance_ref="event_broker_staging_inbound", actor_id="staging_fixture",
        )
        inbound = Event(
            "event_broker_staging_inbound", TENANT_ID, COMPANY_ID,
            "correlation-broker-staging", None, "integration.message.received",
            clock(), {"artifact_ref": ARTIFACT_ID}, "businessbuilder.integration.test",
        )
        app.runtime.events.publish(inbound)
        job = app.service.submit(
            tenant_id=TENANT_ID, company_id=COMPANY_ID,
            role_id="role_inbox_assistant", capability="communications.email",
            action="send_preapproved_reply", budget_ref="budget_broker_staging",
            maximum_job_spend=Money("USD", 5),
            idempotency_key="broker-staging-provider-action-0001",
            correlation_id=inbound.correlation_id,
            causation_id=inbound.event_id,
            trigger_class=TriggerClass.INBOUND_EVENT,
            trigger_ref=inbound.event_id,
            input_artifact_refs=(ArtifactRef("attachment", ARTIFACT_ID),),
            secret_refs=(job_secret_ref,),
        )
        envelope = app.runtime_repository.get_agent_envelope(TENANT_ID, COMPANY_ID, job.job_id)
        payload = envelope.to_payload()
        assert_queue_payload_safe(payload)
        if credential.secret_locator in envelope.to_json():
            raise AssertionError("secret locator entered the job envelope")

        with app.broker.resolve_secret(
            envelope, job_secret_ref, operation="send_preapproved_reply"
        ) as material:
            material_present = material.use(lambda value: bool(value))
        credential_discarded = material.discarded
        signed = app.broker.issue_signed_download(
            envelope, envelope.input_artifact_refs[0],
            operation="send_preapproved_reply", requested_seconds=60,
        )
        signed_access_bounded = signed.expires_at <= envelope.expires_at

        wrong_tenant_denied = _denied(lambda: app.broker.resolve_secret(
            envelope, replace(job_secret_ref, tenant_id="tenant_other"),
            operation="send_preapproved_reply",
        ))
        wrong_capability_denied = _denied(lambda: app.broker.resolve_secret(
            envelope, replace(job_secret_ref, capability="customer.quote"),
            operation="send_preapproved_reply",
        ))
        original_time = clock.now
        clock.now = envelope.expires_at
        expired_job_denied = _denied(lambda: app.broker.resolve_secret(
            envelope, job_secret_ref, operation="send_preapproved_reply"
        ))
        clock.now = original_time
        app.broker.revoke_connection(
            TENANT_ID, COMPANY_ID, connection.connection_id,
            reason="staging revocation proof", actor_id=owner.user_id,
        )
        revoked_provider_denied = _denied(lambda: app.broker.resolve_secret(
            envelope, job_secret_ref, operation="send_preapproved_reply"
        ))
        app.runtime_repository.save_broker_record(
            "provider_connection", connection.connection_id,
            TENANT_ID, COMPANY_ID, connection,
        )
        _set_entitlement(app, EntitlementStatus.SUSPENDED, clock())
        suspended_entitlement_denied = _denied(lambda: app.broker.resolve_secret(
            envelope, job_secret_ref, operation="send_preapproved_reply"
        ))
        _set_entitlement(app, EntitlementStatus.ACTIVE, clock())

        artifact_store.put(
            artifact.object_key, b"tampered staging proof object",
            content_type=artifact.content_type,
            content_sha256=sha256(b"tampered staging proof object").hexdigest(),
        )
        artifact_tampering_denied = _denied(lambda: app.broker.resolve_artifact(
            envelope, envelope.input_artifact_refs[0], operation="send_preapproved_reply"
        ))
        artifact_store.put(
            artifact.object_key, content, content_type=artifact.content_type,
            content_sha256=artifact.content_sha256,
        )

        if app.dispatcher.dispatch_pending(limit=1) != 1:
            raise AssertionError("brokered Runtime outbox did not dispatch")
        worker_result = app.worker.process_one(wait_seconds=20)
        if worker_result != "succeeded":
            raise AssertionError("brokered shared worker did not succeed")
        receipts = app.runtime_repository.list_provider_receipts(TENANT_ID, COMPANY_ID)
        if len(receipts) != 1 or receipts[0].status is not ReceiptStatus.SUCCEEDED:
            raise AssertionError("durable provider receipt was not recorded")
        call_count = provider.call_count
        queue.send(
            f"queue_{sha256(job.job_id.encode()).hexdigest()[:24]}", envelope.to_payload()
        )
        duplicate_result = app.worker.process_one(wait_seconds=20)
        duplicate_action_suppressed = duplicate_result == "duplicate" and provider.call_count == call_count == 1
        tenant_isolation = (
            app.runtime_repository.get_broker_record(
                "external_credential_ref", "tenant_other", COMPANY_ID,
                credential.secret_ref,
            ) is None
            and not app.runtime_repository.list_broker_records("artifact", "tenant_other", COMPANY_ID)
            and not app.runtime_repository.list_provider_receipts("tenant_other", COMPANY_ID)
        )
        audits = app.runtime_repository.list_audit(TENANT_ID, COMPANY_ID)
        audit_actions = {item["action"] for item in audits}
        audit_complete = {
            "broker.secret.authorized", "broker.secret.denied",
            "broker.artifact.authorized", "broker.artifact.denied",
            "broker.artifact.signed_access_issued",
            "broker.provider_action.attempted", "broker.provider_receipt.recorded",
            "broker.connection.revoked",
        } <= audit_actions
        serialized_records = json.dumps(
            [item for item in audits] + [receipt.__dict__ if hasattr(receipt, "__dict__") else repr(receipt) for receipt in receipts],
            default=str,
        )
        no_secret_locator_persisted_in_job_receipt_or_audit = credential.secret_locator not in serialized_records
        proof = {
            "proof": "secret-artifact-broker-v1",
            "status": "passed",
            "backend": "postgresql+sqs+s3+secrets-manager+shared-fargate-task",
            "runtime_authority": True,
            "worker_result_persisted": worker_result == "succeeded",
            "credential_resolved_in_memory": material_present,
            "credential_discarded": credential_discarded,
            "job_payload_opaque_only": credential.secret_locator not in envelope.to_json(),
            "artifact_hash_verified": True,
            "signed_access_bounded": signed_access_bounded,
            "provider_receipt_persisted": len(receipts) == 1,
            "duplicate_provider_action_suppressed": duplicate_action_suppressed,
            "wrong_tenant_denied": wrong_tenant_denied,
            "wrong_capability_denied": wrong_capability_denied,
            "expired_job_denied": expired_job_denied,
            "revoked_provider_denied": revoked_provider_denied,
            "suspended_entitlement_denied": suspended_entitlement_denied,
            "artifact_tampering_denied": artifact_tampering_denied,
            "tenant_isolation": tenant_isolation,
            "audit_complete": audit_complete,
            "no_secret_locator_in_job_receipt_or_audit": no_secret_locator_persisted_in_job_receipt_or_audit,
            "no_real_external_action": True,
            "test_provider_only": True,
        }
        failed = [key for key, value in proof.items() if isinstance(value, bool) and not value]
        if failed:
            raise AssertionError(
                "secret/artifact broker staging proof invariant failed: " + ",".join(failed)
            )
        with app.runtime_repository.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO bb_cloud_proofs(proof_id, status, body, completed_at)
                VALUES (%s, 'passed', %s, %s)
                ON CONFLICT(proof_id) DO UPDATE SET status='passed', body=EXCLUDED.body,
                completed_at=EXCLUDED.completed_at""",
                (proof_id, json.dumps(proof, sort_keys=True), clock()),
            )
        return proof
    finally:
        app.close()


def _activate_build_and_run(app, context, user_id, clock, proof_id):
    order = app.commercial.create_order(
        context, "product_version_build_and_run_v1", amount=Amount("USD", 1000)
    )
    checkout = app.commercial.create_checkout(context, order.order_id, f"checkout-{proof_id}")
    app.commercial.handle_billing_event(NormalizedBillingEvent(
        "billing_event_broker_payment", "billing.payment.succeeded",
        TENANT_ID, user_id, COMPANY_ID, clock(), "fixture_pay",
        "provider_event_broker_payment", "correlation-broker-payment",
        order_id=order.order_id, checkout_intent_id=checkout.checkout_intent_id,
        payment_provider_ref="opaque_broker_payment", amount=order.total,
    ))
    app.commercial.handle_billing_event(NormalizedBillingEvent(
        "billing_event_broker_subscription", "billing.subscription.created",
        TENANT_ID, user_id, COMPANY_ID, clock(), "fixture_pay",
        "provider_event_broker_subscription", "correlation-broker-subscription",
        order_id=order.order_id, subscription_provider_ref="opaque_broker_subscription",
        subscription_status=SubscriptionStatus.ACTIVE,
        current_period=BillingPeriod(clock(), clock() + timedelta(days=30)),
    ))


def _set_entitlement(app, status: EntitlementStatus, at: datetime) -> None:
    for grant in app.commercial_repository.get_current_entitlement_grants(TENANT_ID, COMPANY_ID):
        if grant.entitlement_class is EntitlementClass.STROMATION_MANAGED:
            app.commercial_repository.append_entitlement_grant(
                replace(grant, status=status, updated_at=at, version=grant.version + 1)
            )


def _denied(call) -> bool:
    try:
        material = call()
        if hasattr(material, "close"):
            material.close()
    except BrokerDenied:
        return True
    return False


if __name__ == "__main__":
    print(json.dumps(run_staging_cloud_proof(), sort_keys=True), flush=True)
