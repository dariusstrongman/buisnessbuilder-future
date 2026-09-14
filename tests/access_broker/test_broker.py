from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import tempfile
import unittest

from businessbuilder.access_broker.adapters import (
    AwsSecretsManagerStore,
    DeterministicExternalProvider,
    InMemoryArtifactStore,
    InMemorySecretStore,
    S3ArtifactStore,
)
from businessbuilder.access_broker.capability import BrokeredAgentCapability
from businessbuilder.access_broker.models import (
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
from businessbuilder.access_broker.service import (
    BrokerDenied,
    ProviderActionSuppressed,
    SecretArtifactBroker,
    SimulatedBrokerCrash,
)
from businessbuilder.agent_runtime import (
    AgentOutboxDispatcher,
    AgentRuntimeService,
    InMemoryQueue,
    ModelCandidate,
    ModelPolicy,
    ModelRouter,
    SharedAgentWorker,
    TriggerClass,
)
from businessbuilder.agent_runtime.models import assert_queue_payload_safe
from businessbuilder.ai_workforce import InMemoryWorkforceRepository, WorkforcePolicyService, safe_role_definitions
from businessbuilder.commercial import (
    Amount,
    BillingPeriod,
    CommercialService,
    EntitlementClass,
    EntitlementStatus,
    InMemoryCommercialRepository,
    NormalizedBillingEvent,
    SubscriptionStatus,
    seed_default_catalog,
)
from businessbuilder.company_brain import (
    Company,
    CompanyBrainService,
    EntityRef,
    LifecycleState,
    Provenance,
    Scope,
    SQLiteCompanyBrainRepository,
)
from businessbuilder.identity import (
    AuthorizationContext,
    FakeDevAuthenticationProvider,
    IdentityService,
    PrincipalContextAuthority,
    SessionService,
    SQLiteIdentityRepository,
)
from businessbuilder.integration import CompanyBrainRuntimeAdapter, IdentityApprovalPrincipalVerifier
from businessbuilder.runtime import ArtifactRef, Budget, CapabilityRegistry, Event, Money, SQLiteRuntimeRepository
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.runtime.ports import RecordingVerificationPort


NOW = datetime(2026, 9, 13, 21, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


class BrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.clock = MutableClock()
        self.ids = DeterministicIds()

        self.identity_repository = SQLiteIdentityRepository(str(root / "identity.sqlite"))
        identity = IdentityService(self.identity_repository, id_factory=self.ids, clock=self.clock)
        self.owner, _ = identity.register_founder("broker-owner@example.test", "Broker Owner")
        self.tenant, self.organization, _ = identity.create_account(self.owner.user_id, "Broker Test")
        self.company_id = "company_broker_test"
        identity.attach_company(AuthorizationContext(self.owner.user_id, self.tenant.tenant_id), self.company_id)
        auth = FakeDevAuthenticationProvider()
        assertion = secrets.token_urlsafe(32)
        auth.register(self.owner.user_id, self.owner.email, assertion)
        sessions = SessionService(self.identity_repository, auth, id_factory=self.ids, clock=self.clock)
        _, session_token = sessions.sign_in(self.owner.email, assertion)
        self.authority = PrincipalContextAuthority(
            self.identity_repository, clock=self.clock, signing_key=secrets.token_bytes(48)
        )
        self.principal = self.authority.issue(
            session_token, tenant_id=self.tenant.tenant_id, company_id=self.company_id
        )

        self.brain_repository = SQLiteCompanyBrainRepository(str(root / "brain.sqlite"))
        self.brain_repository.migrate()
        self.brain = CompanyBrainService(self.brain_repository)
        owner_ref = EntityRef("user", self.owner.user_id)
        self.brain.create_company(
            Company(
                Scope(self.tenant.tenant_id, self.company_id), "Broker Test Company",
                "service", {"country": "US", "region": "TX"}, (owner_ref,),
                lifecycle=LifecycleState.OPERATING,
                provenance=(Provenance("test", NOW.isoformat(), owner_ref, source_ref="test://broker"),),
            )
        )

        self.commercial_repository = InMemoryCommercialRepository()
        seed_default_catalog(self.commercial_repository, effective_at=NOW)
        self.commercial = CommercialService(
            self.commercial_repository, identity.authorization,
            type("Sink", (), {"publish": lambda self, event: None})(),
            id_factory=self.ids, clock=self.clock,
        )
        self.context = AuthorizationContext(self.owner.user_id, self.tenant.tenant_id, self.company_id)
        self._activate_build_and_run()

        self.workforce_repository = InMemoryWorkforceRepository()
        for role in safe_role_definitions(self.tenant.tenant_id, self.company_id, created_at=NOW):
            self.workforce_repository.add_definition(role)
        self.workforce = WorkforcePolicyService(self.workforce_repository, clock=self.clock, id_factory=self.ids)

        self.repository = SQLiteRuntimeRepository(str(root / "runtime.sqlite"))
        self.registry = CapabilityRegistry()
        self.runtime = JobOrchestrator(
            repository=self.repository,
            registry=self.registry,
            company_reader=CompanyBrainRuntimeAdapter(self.brain),
            verification=RecordingVerificationPort(),
            id_factory=self.ids,
            clock=self.clock,
            approval_principals=IdentityApprovalPrincipalVerifier(self.authority),
        )
        self.runtime.budgets.create(
            Budget("budget_broker", self.tenant.tenant_id, self.company_id, Money("USD", 100)),
            "correlation-broker-budget",
        )
        self.agent_runtime = AgentRuntimeService(
            runtime=self.runtime,
            repository=self.repository,
            identity_repository=self.identity_repository,
            principal_authority=self.authority,
            commercial_repository=self.commercial_repository,
            company_brain=self.brain,
            workforce=self.workforce,
            model_router=ModelRouter(),
            model_candidates=lambda capability: (
                ModelCandidate("deterministic-test", "bounded-v1", frozenset({capability, "tools"}), 90, True, True, True, 10, Money("USD", 3)),
            ),
            clock=self.clock,
            id_factory=self.ids,
        )
        self.secret_store = InMemorySecretStore()
        self.artifact_store = InMemoryArtifactStore(clock=self.clock)
        self.provider = DeterministicExternalProvider()
        self.broker = SecretArtifactBroker(
            repository=self.repository,
            agent_runtime=self.agent_runtime,
            secret_store=self.secret_store,
            artifact_store=self.artifact_store,
            providers={self.provider.provider: self.provider},
            clock=self.clock,
            id_factory=self.ids,
        )
        self.registry.register(BrokeredAgentCapability("communications.email", self.broker, estimated_minor=3))
        self._register_connection()
        self.artifact = self.broker.register_artifact(
            tenant_id=self.tenant.tenant_id,
            company_id=self.company_id,
            artifact_id="artifact_test_attachment",
            content=b"bounded attachment content",
            content_type="text/plain",
            classification=ArtifactClassification.EXTERNAL_ATTACHMENT,
            provenance_ref="event_inbound_broker",
            actor_id="fixture_setup",
        )
        self.event = Event(
            "event_inbound_broker", self.tenant.tenant_id, self.company_id,
            "correlation-broker", None, "integration.message.received", self.clock(),
            {"artifact_ref": self.artifact.artifact_id}, "businessbuilder.integration.test",
        )
        self.runtime.events.publish(self.event)
        self.queue = InMemoryQueue(max_receive_count=2)
        self.dispatcher = AgentOutboxDispatcher(self.repository, self.queue, dispatcher_id="broker-dispatcher", clock=self.clock)
        self.worker = SharedAgentWorker(service=self.agent_runtime, queue=self.queue, worker_id="broker-worker", clock=self.clock)

    def tearDown(self) -> None:
        self.repository.close()
        self.brain_repository.close()
        self.identity_repository.close()
        self.temp.cleanup()

    def _activate_build_and_run(self) -> None:
        order = self.commercial.create_order(
            self.context, "product_version_build_and_run_v1", amount=Amount("USD", 1000)
        )
        checkout = self.commercial.create_checkout(self.context, order.order_id, "broker-checkout")
        self.commercial.handle_billing_event(
            NormalizedBillingEvent(
                "billing_event_broker_payment", "billing.payment.succeeded",
                self.tenant.tenant_id, self.owner.user_id, self.company_id, self.clock(),
                "fixture_pay", "provider_event_broker_payment", "correlation-broker-payment",
                order_id=order.order_id, checkout_intent_id=checkout.checkout_intent_id,
                payment_provider_ref="opaque_broker_payment", amount=order.total,
            )
        )
        self.commercial.handle_billing_event(
            NormalizedBillingEvent(
                "billing_event_broker_subscription", "billing.subscription.created",
                self.tenant.tenant_id, self.owner.user_id, self.company_id, self.clock(),
                "fixture_pay", "provider_event_broker_subscription", "correlation-broker-subscription",
                order_id=order.order_id,
                subscription_provider_ref="opaque_broker_subscription",
                subscription_status=SubscriptionStatus.ACTIVE,
                current_period=BillingPeriod(self.clock(), self.clock() + timedelta(days=30)),
            )
        )

    def _register_connection(self) -> None:
        self.account = ExternalAccount(
            "external_account_test_email", self.tenant.tenant_id, self.company_id,
            self.provider.provider, "external_mailbox_test", "Test mailbox",
            ConnectionStatus.ACTIVE, self.clock(), self.clock(),
        )
        self.connection = ProviderConnection(
            "connection_test_email", self.account.account_id, self.tenant.tenant_id,
            self.company_id, self.provider.provider, ConnectionStatus.ACTIVE,
            self.clock(), self.clock(),
        )
        self.credential = ExternalCredentialRef(
            "secretref_test_email", self.connection.connection_id,
            self.tenant.tenant_id, self.company_id, self.provider.provider,
            "email.oauth", "memory://test-email-credential", ConnectionStatus.ACTIVE,
            self.clock(), self.clock(), self.clock() + timedelta(days=7),
        )
        self.job_secret_ref = JobSecretRef(
            self.credential.secret_ref, self.provider.provider, "communications.email",
            self.tenant.tenant_id, self.company_id,
        )
        self.secret_material = secrets.token_urlsafe(48).encode()
        self.secret_store.register(self.credential.secret_locator, self.secret_material)
        self.broker.register_external_account(self.account, actor_id="fixture_setup")
        self.broker.register_connection(self.connection, actor_id="fixture_setup")
        self.broker.register_credential_ref(self.credential, actor_id="fixture_setup")
        self.grant = CapabilityGrant(
            "grant_test_email", self.tenant.tenant_id, self.company_id,
            self.connection.connection_id, "role_inbox_assistant",
            CredentialScope(
                "communications.email", frozenset({"send_preapproved_reply"}),
                frozenset({"email.oauth"}),
            ),
            frozenset({ArtifactClassification.EXTERNAL_ATTACHMENT}), self.clock(),
            expires_at=self.clock() + timedelta(days=7),
        )
        self.broker.register_capability_grant(self.grant, actor_id="fixture_setup")

    def _submit(self, *, key="broker-provider-action-0001", expires_in=timedelta(minutes=15)):
        return self.agent_runtime.submit(
            tenant_id=self.tenant.tenant_id,
            company_id=self.company_id,
            role_id="role_inbox_assistant",
            capability="communications.email",
            action="send_preapproved_reply",
            budget_ref="budget_broker",
            maximum_job_spend=Money("USD", 5),
            idempotency_key=key,
            correlation_id=self.event.correlation_id,
            trigger_class=TriggerClass.INBOUND_EVENT,
            trigger_ref=self.event.event_id,
            causation_id=self.event.event_id,
            input_artifact_refs=(ArtifactRef("attachment", self.artifact.artifact_id),),
            secret_refs=(self.job_secret_ref,),
            model_policy=ModelPolicy(quality_floor=70, requires_tools=True),
            expires_in=expires_in,
        )

    def _envelope(self, job):
        return self.repository.get_agent_envelope(
            self.tenant.tenant_id, self.company_id, job.job_id
        )

    def _set_entitlement(self, status: EntitlementStatus) -> None:
        for grant in self.commercial_repository.get_current_entitlement_grants(
            self.tenant.tenant_id, self.company_id
        ):
            if grant.entitlement_class is EntitlementClass.STROMATION_MANAGED:
                self.commercial_repository.append_entitlement_grant(
                    replace(grant, status=status, updated_at=self.clock(), version=grant.version + 1)
                )

    def test_worker_brokers_secret_artifact_and_writes_receipt(self) -> None:
        job = self._submit()
        envelope = self._envelope(job)
        payload = envelope.to_payload()
        self.assertEqual([self.job_secret_ref.to_contract()], payload["secret_refs"])
        self.assertNotIn(self.credential.secret_locator, envelope.to_json())
        assert_queue_payload_safe(payload)
        self.assertEqual(1, self.dispatcher.dispatch_pending())
        self.assertEqual("succeeded", self.worker.process_one())
        receipts = self.repository.list_provider_receipts(self.tenant.tenant_id, self.company_id)
        self.assertEqual(1, len(receipts))
        self.assertIs(ReceiptStatus.SUCCEEDED, receipts[0].status)
        self.assertEqual(1, self.provider.call_count)
        self.assertNotIn(self.credential.secret_locator, repr(receipts[0]))

    def test_capability_scope_and_tenant_company_mismatch_default_deny(self) -> None:
        envelope = self._envelope(self._submit())
        variants = (
            replace(self.job_secret_ref, tenant_id="tenant_other"),
            replace(self.job_secret_ref, company_id="company_other"),
            replace(self.job_secret_ref, capability="customer.quote"),
            replace(self.job_secret_ref, provider="crm-test"),
        )
        for reference in variants:
            with self.assertRaises(BrokerDenied):
                self.broker.resolve_secret(envelope, reference, operation="send_preapproved_reply")

    def test_secret_type_and_operation_must_be_explicitly_granted(self) -> None:
        envelope = self._envelope(self._submit())
        with self.assertRaises(BrokerDenied):
            self.broker.resolve_secret(envelope, self.job_secret_ref, operation="delete_mailbox")
        changed = replace(self.credential, secret_type="payments.api_token")
        self.repository.save_broker_record(
            "external_credential_ref", changed.secret_ref,
            changed.tenant_id, changed.company_id, changed,
        )
        with self.assertRaises(BrokerDenied):
            self.broker.resolve_secret(envelope, self.job_secret_ref, operation="send_preapproved_reply")

    def test_expired_job_revoked_connection_and_suspended_entitlement_fail_closed(self) -> None:
        job = self._submit()
        envelope = self._envelope(job)
        self.clock.now = envelope.expires_at
        with self.assertRaises(BrokerDenied):
            self.broker.resolve_secret(envelope, self.job_secret_ref, operation="send_preapproved_reply")
        self.clock.now = NOW
        self.repository.save_broker_record(
            "external_credential_ref", self.credential.secret_ref,
            self.tenant.tenant_id, self.company_id,
            replace(self.credential, status=ConnectionStatus.REVOKED),
        )
        with self.assertRaises(BrokerDenied):
            self.broker.resolve_secret(envelope, self.job_secret_ref, operation="send_preapproved_reply")
        self.repository.save_broker_record(
            "external_credential_ref", self.credential.secret_ref,
            self.tenant.tenant_id, self.company_id, self.credential,
        )
        self.broker.revoke_connection(
            self.tenant.tenant_id, self.company_id, self.connection.connection_id,
            reason="test revocation", actor_id=self.owner.user_id,
        )
        with self.assertRaises(BrokerDenied):
            self.broker.resolve_secret(envelope, self.job_secret_ref, operation="send_preapproved_reply")
        self.repository.save_broker_record(
            "provider_connection", self.connection.connection_id,
            self.tenant.tenant_id, self.company_id, self.connection,
        )
        self._set_entitlement(EntitlementStatus.SUSPENDED)
        with self.assertRaises(BrokerDenied):
            self.broker.resolve_secret(envelope, self.job_secret_ref, operation="send_preapproved_reply")

    def test_artifact_scope_classification_quarantine_and_integrity(self) -> None:
        envelope = self._envelope(self._submit())
        with self.broker.resolve_artifact(
            envelope, envelope.input_artifact_refs[0], operation="send_preapproved_reply"
        ) as material:
            self.assertGreater(material.use(len), 0)
        with self.assertRaises(BrokerDenied):
            self.broker.resolve_artifact(
                envelope, ArtifactRef("attachment", "artifact_other"),
                operation="send_preapproved_reply",
            )
        self.artifact_store.tamper_for_test(self.artifact.object_key, b"tampered")
        with self.assertRaises(BrokerDenied):
            self.broker.resolve_artifact(
                envelope, envelope.input_artifact_refs[0], operation="send_preapproved_reply"
            )

    def test_signed_access_is_scoped_hash_verified_and_expires(self) -> None:
        envelope = self._envelope(self._submit())
        signed = self.broker.issue_signed_download(
            envelope, envelope.input_artifact_refs[0],
            operation="send_preapproved_reply", requested_seconds=30,
        )
        self.assertNotIn(signed.consume_url(), repr(signed))
        self.assertEqual(self.artifact.content_sha256, signed.content_sha256)
        self.assertEqual(b"bounded attachment content", self.artifact_store.fetch_signed(signed.consume_url()))
        self.clock.now += timedelta(seconds=30)
        with self.assertRaises(PermissionError):
            self.artifact_store.fetch_signed(signed.consume_url())

    def test_duplicate_provider_action_is_suppressed_by_durable_receipt(self) -> None:
        envelope = self._envelope(self._submit())
        first = self.broker.execute_provider_action(
            envelope, operation="send_preapproved_reply",
            secret_ref=self.job_secret_ref,
            artifact_refs=envelope.input_artifact_refs,
        )
        second = self.broker.execute_provider_action(
            envelope, operation="send_preapproved_reply",
            secret_ref=self.job_secret_ref,
            artifact_refs=envelope.input_artifact_refs,
        )
        self.assertEqual(first, second)
        self.assertEqual(1, self.provider.call_count)

    def test_crash_after_provider_is_reconciled_without_repeating_action(self) -> None:
        envelope = self._envelope(self._submit())
        with self.assertRaises(SimulatedBrokerCrash):
            self.broker.execute_provider_action(
                envelope, operation="send_preapproved_reply",
                secret_ref=self.job_secret_ref,
                artifact_refs=envelope.input_artifact_refs,
                crash_after_provider=True,
            )
        with self.assertRaises(ProviderActionSuppressed):
            self.broker.execute_provider_action(
                envelope, operation="send_preapproved_reply",
                secret_ref=self.job_secret_ref,
                artifact_refs=envelope.input_artifact_refs,
            )
        recovered = self.broker.reconcile_provider_action(
            envelope, provider_name=self.provider.provider,
            operation="send_preapproved_reply",
        )
        self.assertIs(ReceiptStatus.SUCCEEDED, recovered.status)
        self.assertEqual(1, self.provider.call_count)

    def test_duplicate_queue_delivery_never_repeats_provider_action(self) -> None:
        job = self._submit()
        envelope = self._envelope(job)
        self.dispatcher.dispatch_pending()
        self.assertEqual("succeeded", self.worker.process_one())
        self.queue.send(f"queue_{__import__('hashlib').sha256(job.job_id.encode()).hexdigest()[:24]}", envelope.to_payload())
        self.assertEqual("duplicate", self.worker.process_one())
        self.assertEqual(1, self.provider.call_count)

    def test_tenant_cannot_read_foreign_records_receipts_or_artifacts(self) -> None:
        envelope = self._envelope(self._submit())
        self.broker.execute_provider_action(
            envelope, operation="send_preapproved_reply",
            secret_ref=self.job_secret_ref, artifact_refs=envelope.input_artifact_refs,
        )
        self.assertIsNone(self.repository.get_broker_record(
            "external_credential_ref", "tenant_other", self.company_id,
            self.credential.secret_ref,
        ))
        self.assertEqual((), self.repository.list_broker_records(
            "artifact", "tenant_other", self.company_id,
        ))
        self.assertEqual((), self.repository.list_provider_receipts(
            "tenant_other", self.company_id,
        ))

    def test_credential_is_discarded_and_audit_contains_no_material(self) -> None:
        envelope = self._envelope(self._submit())
        material = self.broker.resolve_secret(
            envelope, self.job_secret_ref, operation="send_preapproved_reply"
        )
        material.close()
        self.assertTrue(material.discarded)
        with self.assertRaises(RuntimeError):
            material.use(len)
        encoded_audit = repr(self.repository.list_audit(self.tenant.tenant_id, self.company_id))
        self.assertNotIn(self.credential.secret_locator, encoded_audit)
        database_dump = "\n".join(self.repository.connection.iterdump())
        self.assertNotIn(self.secret_material.decode(), database_dump)
        actions = {item["action"] for item in self.repository.list_audit(self.tenant.tenant_id, self.company_id)}
        self.assertTrue({
            "broker.credential.reference_registered",
            "broker.artifact.registered",
            "broker.secret.authorized",
        } <= actions)

    def test_queue_rejects_raw_sensitive_fields(self) -> None:
        safe = {
            "secret_refs": [self.job_secret_ref.to_contract()],
            "tenant_id": self.tenant.tenant_id,
        }
        assert_queue_payload_safe(safe)
        for key in ("api_key", "password", "oauth_token", "aws_access_key"):
            with self.assertRaises(ValueError):
                assert_queue_payload_safe({key: "prohibited"})
        with self.assertRaises(ValueError):
            assert_queue_payload_safe({"secret_refs": [{**self.job_secret_ref.to_contract(), "secret_locator": "prohibited"}]})

    def test_aws_adapters_enforce_exact_secret_and_object_scope_without_listing(self) -> None:
        class SecretsClient:
            def __init__(self):
                self.calls = []

            def get_secret_value(self, **request):
                self.calls.append(request)
                return {"SecretString": self_secret}

        self_secret = secrets.token_urlsafe(32)
        allowed_arn = "arn:aws:secretsmanager:us-east-1:111122223333:secret:broker-test"
        client = SecretsClient()
        store = AwsSecretsManagerStore(
            allowed_secret_arns=frozenset({allowed_arn}), client=client
        )
        with store.resolve(allowed_arn) as material:
            self.assertTrue(material.use(bool))
        self.assertEqual([{"SecretId": allowed_arn}], client.calls)
        with self.assertRaises(PermissionError):
            store.resolve(allowed_arn + "-other")
        with self.assertRaises(ValueError):
            AwsSecretsManagerStore(
                allowed_secret_arns=frozenset({allowed_arn + "*"}), client=client
            )

        class S3Client:
            def __init__(self):
                self.calls = []

            def put_object(self, **request):
                self.calls.append(("put_object", request))

            def generate_presigned_url(self, operation, *, Params, ExpiresIn):
                self.calls.append(("generate_presigned_url", operation, Params, ExpiresIn))
                return "https://signed.example.test/opaque"

        s3_client = S3Client()
        prefix = f"tenant/{self.tenant.tenant_id}/company/{self.company_id}/"
        s3 = S3ArtifactStore(
            bucket="private-test-bucket", allowed_prefix=prefix, client=s3_client
        )
        key = prefix + "artifacts/example/" + "b" * 64
        content = b"test object"
        s3.put(key, content, content_type="text/plain", content_sha256=__import__("hashlib").sha256(content).hexdigest())
        s3.signed_download(key, expires_in_seconds=60)
        self.assertEqual(["put_object", "generate_presigned_url"], [item[0] for item in s3_client.calls])
        with self.assertRaises(PermissionError):
            s3.signed_download("tenant/tenant_other/company/company_other/object", expires_in_seconds=60)


if __name__ == "__main__":
    unittest.main()
