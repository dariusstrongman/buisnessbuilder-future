from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from businessbuilder.commercial import (
    Amount,
    CommercialOutboxDispatcher,
    CommercialService,
    InMemoryCommercialRepository,
    NormalizedBillingEvent,
    OrderStatus,
    OutboxStatus,
    ProductCode,
    RecordingCommercialEventSink,
    SQLiteCommercialRepository,
    seed_default_catalog,
)
from businessbuilder.identity import (
    AuthorizationContext,
    IdentityService,
    InMemoryIdentityRepository,
)
from businessbuilder.runtime.ids import DeterministicIds


NOW = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)


class FailingEventSink:
    def publish(self, event) -> None:
        raise RuntimeError("simulated downstream failure")


class FailingEnqueueRepository(InMemoryCommercialRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_enqueue = False

    def enqueue_outbox(self, event, idempotency_key):
        message = super().enqueue_outbox(event, idempotency_key)
        if self.fail_enqueue:
            raise RuntimeError("simulated transaction failure")
        return message


class FailingSQLiteEnqueueRepository(SQLiteCommercialRepository):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.fail_enqueue = False

    def enqueue_outbox(self, event, idempotency_key):
        message = super().enqueue_outbox(event, idempotency_key)
        if self.fail_enqueue:
            raise RuntimeError("simulated transaction failure")
        return message


class CommercialDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ids = DeterministicIds()
        identity_repository = InMemoryIdentityRepository()
        identity = IdentityService(
            identity_repository, id_factory=self.ids, clock=lambda: NOW
        )
        self.user, _ = identity.register_founder(
            "security-fixture@example.test", "Security Fixture"
        )
        self.tenant, _, _ = identity.create_account(
            self.user.user_id, "Security Fixture"
        )
        self.company_id = "company_security_fixture"
        identity.attach_company(
            AuthorizationContext(self.user.user_id, self.tenant.tenant_id),
            self.company_id,
        )
        self.context = AuthorizationContext(
            self.user.user_id, self.tenant.tenant_id, self.company_id
        )
        self.authorization = identity.authorization

    def service(self, repository, sink) -> CommercialService:
        return CommercialService(
            repository,
            self.authorization,
            sink,
            id_factory=self.ids,
            clock=lambda: NOW,
        )

    def pending_order(self, repository, service, counter: int = 1):
        order = service.create_order(
            self.context,
            f"product_version_{ProductCode.BUILD_BUSINESS.value.lower()}_v1",
            amount=Amount("USD", 10000),
        )
        checkout = service.create_checkout(
            self.context, order.order_id, f"security-checkout-{counter}"
        )
        return repository.get_order(
            self.tenant.tenant_id, self.company_id, order.order_id
        ), checkout

    def payment_event(self, order, counter: int = 1) -> NormalizedBillingEvent:
        return NormalizedBillingEvent(
            event_id=f"billing_event_security_{counter}",
            event_type="billing.payment.succeeded",
            tenant_id=self.tenant.tenant_id,
            user_id=self.user.user_id,
            company_id=self.company_id,
            occurred_at=NOW,
            provider="fixture_pay",
            provider_event_ref=f"provider_event_security_{counter}",
            correlation_id=f"correlation_security_{counter}",
            order_id=order.order_id,
            payment_provider_ref=f"opaque_payment_security_{counter}",
            amount=order.total,
        )

    def test_concurrent_duplicate_delivery_has_exactly_one_effect(self) -> None:
        repository = InMemoryCommercialRepository()
        seed_default_catalog(repository, effective_at=NOW)
        sink = RecordingCommercialEventSink()
        service = self.service(repository, sink)
        order, _ = self.pending_order(repository, service)
        event = self.payment_event(order)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(lambda _: service.handle_billing_event(event), range(8))
            )

        self.assertEqual(1, results.count(True))
        self.assertEqual(7, results.count(False))
        self.assertEqual(
            5,
            len(
                repository.order_history(
                    order.tenant_id, order.company_id, order.order_id
                )
            ),
        )
        self.assertEqual(
            1,
            sum(
                item.event_type == "commercial.fulfillment.eligible"
                for item in sink.events
            ),
        )

    def test_failed_in_memory_delivery_commits_and_can_retry(self) -> None:
        repository = InMemoryCommercialRepository()
        seed_default_catalog(repository, effective_at=NOW)
        setup_service = self.service(repository, RecordingCommercialEventSink())
        order, _ = self.pending_order(repository, setup_service)
        event = self.payment_event(order)

        with self.assertRaises(RuntimeError):
            self.service(repository, FailingEventSink()).handle_billing_event(event)

        self.assert_committed_pending_delivery(repository, order, event)
        sink = RecordingCommercialEventSink()
        dispatcher = CommercialOutboxDispatcher(
            repository,
            sink,
            dispatcher_id="retry-worker",
            clock=lambda: NOW + timedelta(seconds=2),
        )
        self.assertGreater(dispatcher.dispatch_pending(), 0)
        self.assertTrue(all(item.status is OutboxStatus.ACKNOWLEDGED for item in repository.list_outbox()))
        self.assertEqual(1, sum(item.event_type == "commercial.fulfillment.eligible" for item in sink.events))

    def test_failed_sqlite_delivery_commits_on_disk_and_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "commercial.sqlite")
            repository = SQLiteCommercialRepository(path)
            seed_default_catalog(repository, effective_at=NOW)
            setup_service = self.service(repository, RecordingCommercialEventSink())
            order, _ = self.pending_order(repository, setup_service)
            event = self.payment_event(order)

            with self.assertRaises(RuntimeError):
                self.service(repository, FailingEventSink()).handle_billing_event(event)
            self.assert_committed_pending_delivery(repository, order, event)
            repository.close()

            reopened = SQLiteCommercialRepository(path)
            self.assert_committed_pending_delivery(reopened, order, event)
            sink = RecordingCommercialEventSink()
            dispatcher = CommercialOutboxDispatcher(
                reopened,
                sink,
                dispatcher_id="restart-worker",
                clock=lambda: NOW + timedelta(seconds=2),
            )
            self.assertGreater(dispatcher.dispatch_pending(), 0)
            self.assertTrue(all(item.status is OutboxStatus.ACKNOWLEDGED for item in reopened.list_outbox()))
            reopened.close()

    def test_transaction_rollback_never_leaves_in_memory_outbox_event(self) -> None:
        repository = FailingEnqueueRepository()
        seed_default_catalog(repository, effective_at=NOW)
        service = self.service(repository, RecordingCommercialEventSink())
        order, _ = self.pending_order(repository, service)
        event = self.payment_event(order)
        repository.fail_enqueue = True

        with self.assertRaises(RuntimeError):
            service.handle_billing_event(event)

        self.assert_rolled_back(repository, order, event)
        self.assertEqual((), repository.list_outbox())

    def test_transaction_rollback_never_leaves_sqlite_outbox_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "commercial.sqlite")
            repository = FailingSQLiteEnqueueRepository(path)
            seed_default_catalog(repository, effective_at=NOW)
            service = self.service(repository, RecordingCommercialEventSink())
            order, _ = self.pending_order(repository, service)
            event = self.payment_event(order)
            repository.fail_enqueue = True

            with self.assertRaises(RuntimeError):
                service.handle_billing_event(event)

            self.assert_rolled_back(repository, order, event)
            self.assertEqual((), repository.list_outbox())
            repository.close()

            reopened = SQLiteCommercialRepository(path)
            self.assert_rolled_back(reopened, order, event)
            self.assertEqual((), reopened.list_outbox())
            reopened.close()

    def assert_committed_pending_delivery(self, repository, order, event) -> None:
        current = repository.get_order(order.tenant_id, order.company_id, order.order_id)
        self.assertEqual(OrderStatus.FULFILLMENT_PENDING, current.status)
        self.assertIsNotNone(repository.get_payment_for_order(order.tenant_id, order.company_id, order.order_id))
        self.assertTrue(repository.billing_event_processed(event.provider, event.provider_event_ref))
        self.assertTrue(any(item.status is OutboxStatus.PENDING for item in repository.list_outbox()))

    def assert_rolled_back(self, repository, order, event) -> None:
        current = repository.get_order(
            order.tenant_id, order.company_id, order.order_id
        )
        self.assertEqual(OrderStatus.PENDING_PAYMENT, current.status)
        self.assertIsNone(
            repository.get_payment_for_order(
                order.tenant_id, order.company_id, order.order_id
            )
        )
        self.assertEqual(
            (),
            repository.get_current_entitlement_grants(
                order.tenant_id, order.company_id
            ),
        )
        self.assertFalse(
            repository.billing_event_processed(
                event.provider, event.provider_event_ref
            )
        )

    def test_late_payment_failure_does_not_emit_false_failure_state(self) -> None:
        repository = InMemoryCommercialRepository()
        seed_default_catalog(repository, effective_at=NOW)
        sink = RecordingCommercialEventSink()
        service = self.service(repository, sink)
        order, _ = self.pending_order(repository, service)
        self.assertTrue(service.handle_billing_event(self.payment_event(order)))

        late = NormalizedBillingEvent(
            event_id="billing_event_security_late",
            event_type="billing.payment.failed",
            tenant_id=order.tenant_id,
            user_id=order.user_id,
            company_id=order.company_id,
            occurred_at=NOW,
            provider="fixture_pay",
            provider_event_ref="provider_event_security_late",
            correlation_id="correlation_security_late",
            order_id=order.order_id,
            reason="late delivery",
        )
        self.assertTrue(service.handle_billing_event(late))
        self.assertEqual(
            OrderStatus.FULFILLMENT_PENDING,
            repository.get_order(
                order.tenant_id, order.company_id, order.order_id
            ).status,
        )
        self.assertNotIn(
            "order.payment_failed", [item.event_type for item in sink.events]
        )

    def test_malformed_normalized_event_is_rejected_before_processing(self) -> None:
        valid = dict(
            event_id="billing_event_security_valid",
            event_type="billing.payment.failed",
            tenant_id=self.tenant.tenant_id,
            user_id=self.user.user_id,
            company_id=self.company_id,
            occurred_at=NOW,
            provider="fixture_pay",
            provider_event_ref="provider_event_security_valid",
            correlation_id="correlation_security_valid",
            order_id="order_security_valid",
        )
        for field, invalid in (
            ("tenant_id", ""),
            ("provider", ""),
            ("provider_event_ref", ""),
            ("payment_provider_ref", "x" * 256),
            ("reason", "x" * 2001),
            ("occurred_at", datetime(2026, 9, 13, 20, 0)),
        ):
            values = {**valid, field: invalid}
            with self.subTest(field=field), self.assertRaises(ValueError):
                NormalizedBillingEvent(**values)


if __name__ == "__main__":
    unittest.main()
