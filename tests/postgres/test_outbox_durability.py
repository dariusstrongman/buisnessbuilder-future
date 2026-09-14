from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import uuid4

from businessbuilder.commercial import (
    Amount,
    CommercialEvent,
    CommercialOutboxDispatcher,
    CommercialService,
    NormalizedBillingEvent,
    OrderStatus,
    OutboxStatus,
    ProductCode,
    RecordingCommercialEventSink,
    seed_default_catalog,
)
from businessbuilder.identity import (
    AuthorizationContext,
    FakeDevAuthenticationProvider,
    IdentityService,
    PrincipalContextAuthority,
    SessionService,
)
from businessbuilder.integration import IdentityApprovalPrincipalVerifier
from businessbuilder.integration.commercial import RuntimeCommercialEventSink
from businessbuilder.postgres import (
    PostgresCommercialRepository,
    PostgresIdentityRepository,
    PostgresRuntimeRepository,
)
from businessbuilder.runtime.audit import AuditLog
from businessbuilder.runtime.capabilities import CapabilityRegistry
from businessbuilder.runtime.events import LocalEventBus
from businessbuilder.runtime.fakes import FakeCapability
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.runtime.models import ApprovalMode, Money
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.runtime.ports import FakeCompanyStateReader, RecordingVerificationPort


NOW = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)


class FailingSink:
    def publish(self, event):
        raise RuntimeError("simulated unavailable Runtime")


class FailingPostgresCommercialRepository(PostgresCommercialRepository):
    fail_enqueue = False

    def enqueue_outbox(self, event, idempotency_key):
        message = super().enqueue_outbox(event, idempotency_key)
        if self.fail_enqueue:
            raise RuntimeError("simulated rollback before outbox commit")
        return message


@unittest.skipUnless(
    os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"),
    "requires an isolated PostgreSQL test database",
)
class PostgresOutboxDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        self.schema = f"{prefix}_outbox_{uuid4().hex[:10]}"

    def _commercial_fixture(self, repository_class=PostgresCommercialRepository):
        ids = DeterministicIds()
        identity_repository = PostgresIdentityRepository(self.dsn, schema=self.schema)
        identity = IdentityService(
            identity_repository, id_factory=ids, clock=lambda: NOW
        )
        user, _ = identity.register_founder(
            "postgres-outbox@example.test", "PostgreSQL Outbox"
        )
        tenant, _, _ = identity.create_account(user.user_id, "PostgreSQL Outbox")
        company_id = "company_postgres_outbox"
        identity.attach_company(
            AuthorizationContext(user.user_id, tenant.tenant_id), company_id
        )
        context = AuthorizationContext(user.user_id, tenant.tenant_id, company_id)

        repository = repository_class(self.dsn, schema=self.schema)
        seed_default_catalog(repository, effective_at=NOW)
        service = CommercialService(
            repository,
            identity.authorization,
            RecordingCommercialEventSink(),
            id_factory=ids,
            clock=lambda: NOW,
            auto_dispatch_outbox=False,
        )
        order = service.create_order(
            context,
            f"product_version_{ProductCode.BUILD_BUSINESS.value.lower()}_v1",
            amount=Amount("USD", 10000),
        )
        service.create_checkout(context, order.order_id, "postgres-outbox-checkout")
        event = NormalizedBillingEvent(
            event_id="billing_event_postgres_outbox",
            event_type="billing.payment.succeeded",
            tenant_id=tenant.tenant_id,
            user_id=user.user_id,
            company_id=company_id,
            occurred_at=NOW,
            provider="fixture_pay",
            provider_event_ref="provider_event_postgres_outbox",
            correlation_id="correlation_postgres_outbox",
            order_id=order.order_id,
            payment_provider_ref="payment_postgres_outbox",
            amount=order.total,
        )
        identity_repository.close()
        return repository, service, order, event

    def _runtime_sink(self):
        runtime_repository = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        audit = AuditLog(runtime_repository, DeterministicIds(), lambda: NOW)
        return runtime_repository, RuntimeCommercialEventSink(
            LocalEventBus(runtime_repository, audit)
        )

    def test_migrations_are_repeatable_and_rollback_leaves_no_event(self) -> None:
        repository, service, order, event = self._commercial_fixture(
            FailingPostgresCommercialRepository
        )
        repository.fail_enqueue = True
        with self.assertRaises(RuntimeError):
            service.handle_billing_event(event)
        repository.close()

        reopened = PostgresCommercialRepository(self.dsn, schema=self.schema)
        self.assertEqual(
            OrderStatus.PENDING_PAYMENT,
            reopened.get_order(order.tenant_id, order.company_id, order.order_id).status,
        )
        self.assertFalse(
            reopened.billing_event_processed(event.provider, event.provider_event_ref)
        )
        self.assertEqual((), reopened.list_outbox())
        with reopened.connection.cursor() as cursor:
            cursor.execute("SELECT version FROM bb_schema_migrations ORDER BY version")
            self.assertEqual([4], [row["version"] for row in cursor])
        reopened.close()

        migrated_again = PostgresCommercialRepository(self.dsn, schema=self.schema)
        self.assertEqual((), migrated_again.list_outbox())
        migrated_again.close()

    def test_commit_before_dispatch_recovers_after_repository_restart(self) -> None:
        repository, service, order, event = self._commercial_fixture()
        self.assertTrue(service.handle_billing_event(event))
        self.assertTrue(repository.list_outbox())
        repository.close()

        runtime_repository, sink = self._runtime_sink()
        reopened = PostgresCommercialRepository(self.dsn, schema=self.schema)
        dispatcher = CommercialOutboxDispatcher(
            reopened,
            sink,
            dispatcher_id="restart-dispatcher",
            clock=lambda: NOW,
        )
        self.assertGreater(dispatcher.dispatch_pending(), 0)
        self.assertTrue(
            all(item.status is OutboxStatus.ACKNOWLEDGED for item in reopened.list_outbox())
        )
        runtime_events = runtime_repository.list_events(order.tenant_id, order.company_id)
        self.assertTrue(
            any(item["type"] == "commercial.fulfillment.eligible" for item in runtime_events)
        )
        reopened.close()
        runtime_repository.close()

    def test_publish_before_ack_is_redelivered_idempotently(self) -> None:
        repository, service, order, event = self._commercial_fixture()
        self.assertTrue(service.handle_billing_event(event))
        runtime_repository, sink = self._runtime_sink()

        first = repository.claim_outbox(
            "crashed-dispatcher", at=NOW, lease=timedelta(seconds=30), limit=1
        )[0]
        self.assertTrue(sink.publish(first.event))
        repository.close()

        reopened = PostgresCommercialRepository(self.dsn, schema=self.schema)
        recovered = CommercialOutboxDispatcher(
            reopened,
            sink,
            dispatcher_id="recovery-dispatcher",
            clock=lambda: NOW + timedelta(seconds=31),
        )
        self.assertEqual(1, recovered.dispatch_pending(limit=1))
        message = next(
            item for item in reopened.list_outbox() if item.outbox_id == first.outbox_id
        )
        self.assertEqual(2, message.attempts)
        self.assertIs(message.status, OutboxStatus.ACKNOWLEDGED)
        matching = [
            item
            for item in runtime_repository.list_events(order.tenant_id, order.company_id)
            if item["event_id"] == first.event.event_id
        ]
        self.assertEqual(1, len(matching))
        reopened.close()
        runtime_repository.close()

    def test_retry_and_concurrent_dispatchers_deliver_each_event_once(self) -> None:
        seed = PostgresCommercialRepository(self.dsn, schema=self.schema)
        events = tuple(
            CommercialEvent(
                event_id=f"event_concurrent_{index}",
                event_type="commercial.test",
                tenant_id="tenant_concurrent",
                user_id="user_concurrent",
                company_id="company_concurrent",
                occurred_at=NOW,
                source="postgres.outbox.test",
                correlation_id="correlation_concurrent",
                causation_id=None,
                payload={"index": index},
            )
            for index in range(12)
        )
        with seed.transaction():
            for event in events:
                seed.enqueue_outbox(event, event.event_id)

        retry = CommercialOutboxDispatcher(
            seed,
            FailingSink(),
            dispatcher_id="failing-dispatcher",
            clock=lambda: NOW,
        )
        with self.assertRaises(RuntimeError):
            retry.dispatch_pending(limit=1)
        self.assertEqual("RuntimeError", seed.list_outbox()[0].last_error)
        seed.close()

        runtime_repository, sink = self._runtime_sink()
        first_repository = PostgresCommercialRepository(self.dsn, schema=self.schema)
        second_repository = PostgresCommercialRepository(self.dsn, schema=self.schema)
        first = CommercialOutboxDispatcher(
            first_repository,
            sink,
            dispatcher_id="concurrent-a",
            clock=lambda: NOW + timedelta(seconds=2),
        )
        second = CommercialOutboxDispatcher(
            second_repository,
            sink,
            dispatcher_id="concurrent-b",
            clock=lambda: NOW + timedelta(seconds=2),
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            delivered = list(executor.map(lambda item: item.dispatch_pending(), (first, second)))
        self.assertEqual(12, sum(delivered))
        messages = first_repository.list_outbox()
        self.assertTrue(all(item.status is OutboxStatus.ACKNOWLEDGED for item in messages))
        self.assertEqual(12, len(runtime_repository.list_events("tenant_concurrent", "company_concurrent")))
        first_repository.close()
        second_repository.close()
        runtime_repository.close()

    def test_denied_runtime_approval_audit_survives_postgres_restart(self) -> None:
        ids = DeterministicIds()
        identity_repository = PostgresIdentityRepository(self.dsn, schema=self.schema)
        identity = IdentityService(
            identity_repository, id_factory=ids, clock=lambda: NOW
        )
        owner, _ = identity.register_founder(
            "postgres-authz@example.test", "PostgreSQL Authorization"
        )
        tenant, _, _ = identity.create_account(
            owner.user_id, "PostgreSQL Authorization"
        )
        company_id = "company_postgres_authorization"
        identity.attach_company(
            AuthorizationContext(owner.user_id, tenant.tenant_id), company_id
        )
        provider = FakeDevAuthenticationProvider()
        provider.register(owner.user_id, owner.email, "offline-proof")
        sessions = SessionService(
            identity_repository, provider, id_factory=ids, clock=lambda: NOW
        )
        _, token = sessions.sign_in(owner.email, "offline-proof")
        authority = PrincipalContextAuthority(
            identity_repository,
            clock=lambda: NOW,
            signing_key=b"postgres-proof-process-key",
        )
        valid_principal = authority.issue(
            token, tenant_id=tenant.tenant_id, company_id=company_id
        )

        runtime_repository = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        registry = CapabilityRegistry()
        registry.register(FakeCapability("fake.postgres.approval", estimate_minor=1))
        runtime = JobOrchestrator(
            repository=runtime_repository,
            registry=registry,
            company_reader=FakeCompanyStateReader({(tenant.tenant_id, company_id)}),
            verification=RecordingVerificationPort(),
            id_factory=ids,
            clock=lambda: NOW,
            approval_principals=IdentityApprovalPrincipalVerifier(authority),
        )
        job = runtime.create_job(
            tenant_id=tenant.tenant_id,
            company_id=company_id,
            capability="fake.postgres.approval",
            inputs={"objective": "prove durable authorization denial"},
            budget_ref="unused_for_approval",
            per_job_ceiling=Money("USD", 1),
            idempotency_key="postgres-authz-denial-0001",
            correlation_id="correlation-postgres-authz",
            approval_mode=ApprovalMode.FOUNDER_ONLY,
        )
        with self.assertRaises(PermissionError):
            runtime.approve_job(
                tenant_id=tenant.tenant_id,
                company_id=company_id,
                job_id=job.job_id,
                approval_id=job.approval_ids[0],
                principal={"copied_from": valid_principal.principal_id},
            )
        runtime_repository.close()
        identity_repository.close()

        reopened = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        denied = [
            item
            for item in reopened.list_audit(tenant.tenant_id, company_id)
            if item["action"] == "authorization.denied"
        ]
        self.assertEqual(1, len(denied))
        self.assertEqual("unknown", denied[0]["actor_ref"]["id"])
        self.assertEqual("runtime.approval.decide", denied[0]["permission"])
        self.assertEqual("businessbuilder.runtime.approvals", denied[0]["source"])
        reopened.close()


if __name__ == "__main__":
    unittest.main()
