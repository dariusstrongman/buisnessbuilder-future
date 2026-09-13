from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from businessbuilder.commercial import (
    Amount,
    BillingPeriod,
    CommercialConflict,
    CommercialService,
    CancellationTiming,
    EntitlementClass,
    EntitlementStatus,
    InMemoryCommercialRepository,
    NormalizedBillingEvent,
    OrderStatus,
    ProductCode,
    RecordingCommercialEventSink,
    SQLiteCommercialRepository,
    SubscriptionStatus,
    seed_default_catalog,
)
from businessbuilder.identity import AuthorizationContext, IdentityService, InMemoryIdentityRepository
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.runtime.contracts import ContractValidator


NOW = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


class CommercialTestCase(unittest.TestCase):
    repository_class = InMemoryCommercialRepository

    def setUp(self) -> None:
        self.ids = DeterministicIds()
        self.clock = MutableClock()
        self.identity_repo = InMemoryIdentityRepository()
        self.identity = IdentityService(self.identity_repo, id_factory=self.ids, clock=self.clock)
        self.user, _ = self.identity.register_founder("billy@example.com", "Billy Bob")
        self.tenant, _, _ = self.identity.create_account(self.user.user_id, "Billy Bob Ventures")
        self.company_id = "co_billy"
        self.base_context = AuthorizationContext(self.user.user_id, self.tenant.tenant_id)
        self.identity.attach_company(self.base_context, self.company_id)
        self.context = AuthorizationContext(self.user.user_id, self.tenant.tenant_id, self.company_id)
        self.repo = self.repository_class()
        seed_default_catalog(self.repo, effective_at=NOW)
        self.events = RecordingCommercialEventSink()
        self.service = CommercialService(
            self.repo, self.identity.authorization, self.events,
            id_factory=self.ids, clock=self.clock,
            grace_duration=timedelta(days=7), automation_restriction_delay=timedelta(days=2),
        )

    def event(self, event_type: str, counter: int, **values) -> NormalizedBillingEvent:
        defaults = dict(
            event_id=f"billing_event_{counter}", event_type=event_type,
            tenant_id=self.tenant.tenant_id, user_id=self.user.user_id,
            company_id=self.company_id, occurred_at=self.clock.now,
            provider="fixture_pay", provider_event_ref=f"provider_event_{counter}",
            correlation_id=f"correlation_{counter}",
        )
        defaults.update(values)
        return NormalizedBillingEvent(**defaults)

    def open_order(self, code: ProductCode, counter: int = 1):
        version_id = f"product_version_{code.value.lower()}_v1"
        order = self.service.create_order(self.context, version_id, amount=Amount("USD", 250000))
        checkout = self.service.create_checkout(self.context, order.order_id, f"checkout-key-{counter}")
        return self.repo.get_order(self.tenant.tenant_id, self.company_id, order.order_id), checkout

    def pay_order(self, code: ProductCode, counter: int = 1):
        order, checkout = self.open_order(code, counter)
        event = self.event(
            "billing.payment.succeeded", counter, order_id=order.order_id,
            checkout_intent_id=checkout.checkout_intent_id,
            payment_provider_ref=f"opaque_payment_{counter}", amount=order.total,
        )
        self.assertTrue(self.service.handle_billing_event(event))
        return self.repo.get_order(self.tenant.tenant_id, self.company_id, order.order_id), event


class CatalogAndOrderTests(CommercialTestCase):
    def test_normalized_and_outbound_events_match_public_contracts(self):
        order, checkout = self.open_order(ProductCode.BUILD_WEBSITE, 90)
        incoming = self.event(
            "billing.payment.succeeded", 90, order_id=order.order_id,
            checkout_intent_id=checkout.checkout_intent_id,
            payment_provider_ref="opaque_payment_contract", amount=order.total,
        )
        root = Path(__file__).resolve().parents[2]
        validator = ContractValidator(root / "contracts")
        validator.validate("billing-event.schema.json", incoming.to_contract())
        self.service.handle_billing_event(incoming)
        for event in self.events.events:
            validator.validate("commercial-event.schema.json", event.to_contract())

    def test_catalog_has_three_unpriced_machine_readable_packages(self):
        for code in ProductCode:
            version = self.repo.get_product_version(f"product_version_{code.value.lower()}_v1")
            self.assertIsNone(version.package.price_ref)
            self.assertTrue(version.entitlements)
        recurring = self.repo.get_product_version("product_version_build_and_run_v1")
        self.assertIn("ai_workforce.execute", recurring.package.entitlement_codes)

    def test_order_history_is_immutable_and_follows_paid_fulfillment_lifecycle(self):
        order, _ = self.pay_order(ProductCode.BUILD_BUSINESS)
        history = self.repo.order_history(order.tenant_id, order.company_id, order.order_id)
        self.assertEqual(
            [OrderStatus.DRAFT, OrderStatus.PENDING_PAYMENT, OrderStatus.PENDING_PAYMENT, OrderStatus.PAID, OrderStatus.FULFILLMENT_PENDING],
            [item.status for item in history],
        )
        self.assertEqual([1, 2, 3, 4, 5], [item.version for item in history])
        self.assertEqual("Build My Business", history[0].items[0].package_name_snapshot)

    def test_duplicate_payment_event_does_not_duplicate_grants_or_orders(self):
        order, event = self.pay_order(ProductCode.BUILD_BUSINESS)
        grants_before = self.repo.get_current_entitlement_grants(order.tenant_id, order.company_id)
        history_before = self.repo.order_history(order.tenant_id, order.company_id, order.order_id)
        self.assertFalse(self.service.handle_billing_event(event))
        self.assertEqual(grants_before, self.repo.get_current_entitlement_grants(order.tenant_id, order.company_id))
        self.assertEqual(history_before, self.repo.order_history(order.tenant_id, order.company_id, order.order_id))

    def test_semantically_repeated_payment_with_new_delivery_id_is_also_safe(self):
        order, event = self.pay_order(ProductCode.BUILD_BUSINESS, 6)
        counts = (len(self.repo.order_history(order.tenant_id, order.company_id, order.order_id)), len(self.repo.get_current_entitlement_grants(order.tenant_id, order.company_id)))
        redelivery = self.event("billing.payment.succeeded", 7, order_id=order.order_id, payment_provider_ref=event.payment_provider_ref, amount=order.total)
        self.assertTrue(self.service.handle_billing_event(redelivery))
        self.assertEqual(counts, (len(self.repo.order_history(order.tenant_id, order.company_id, order.order_id)), len(self.repo.get_current_entitlement_grants(order.tenant_id, order.company_id))))

    def test_order_can_cancel_before_fulfillment_with_typed_record(self):
        order, _ = self.open_order(ProductCode.BUILD_WEBSITE, 8)
        canceled = self.service.cancel_order(self.context, order.order_id, CancellationTiming.BEFORE_FULFILLMENT, "Changed mind before work")
        self.assertEqual(OrderStatus.CANCELED, canceled.status)
        self.assertEqual(CancellationTiming.BEFORE_FULFILLMENT, self.repo.cancellations[-1].timing)

    def test_owner_authorized_admin_override_is_audit_only(self):
        self.service.record_admin_override(self.context, "order", "order_external", "Manual reconciliation approved", {"ticket": "T-200"})
        event = self.repo.list_audit(self.tenant.tenant_id, self.company_id)[-1]
        self.assertEqual("admin.override_recorded", event.action)
        with self.assertRaises(LookupError):
            self.repo.get_order(self.tenant.tenant_id, self.company_id, "order_external")

    def test_one_time_purchase_grants_explicit_entitlements_without_readiness(self):
        order, _ = self.pay_order(ProductCode.BUILD_BUSINESS)
        grants = self.repo.get_current_entitlement_grants(order.tenant_id, order.company_id)
        self.assertEqual(
            {"website.build", "website.source_export", "company.build", "company.read_export", "artifacts.read_export"},
            {item.entitlement_code for item in grants},
        )
        serialized_events = repr(self.events.events).lower()
        self.assertNotIn("fully_set", serialized_events)
        self.assertNotIn("not_ready", serialized_events)
        self.assertNotIn("readiness", serialized_events)
        self.assertIn("commercial.fulfillment.eligible", [item.event_type for item in self.events.events])

    def test_payment_failure_can_retry_successfully(self):
        order, _ = self.open_order(ProductCode.BUILD_WEBSITE)
        failed = self.event("billing.payment.failed", 2, order_id=order.order_id, reason="declined")
        self.service.handle_billing_event(failed)
        self.assertEqual(OrderStatus.PAYMENT_FAILED, self.repo.get_order(order.tenant_id, order.company_id, order.order_id).status)
        success = self.event("billing.payment.succeeded", 3, order_id=order.order_id, payment_provider_ref="opaque_payment_retry", amount=order.total)
        self.service.handle_billing_event(success)
        self.assertEqual(OrderStatus.FULFILLMENT_PENDING, self.repo.get_order(order.tenant_id, order.company_id, order.order_id).status)

    def test_cross_tenant_order_access_fails(self):
        order, _ = self.open_order(ProductCode.BUILD_BUSINESS)
        with self.assertRaises(LookupError):
            self.repo.get_order("tenant_other", order.company_id, order.order_id)
        wrong = self.event("billing.payment.succeeded", 4, tenant_id="tenant_other", order_id=order.order_id, payment_provider_ref="opaque_wrong")
        with self.assertRaises(LookupError):
            self.service.handle_billing_event(wrong)

    def test_raw_webhook_payload_is_rejected(self):
        order, _ = self.open_order(ProductCode.BUILD_BUSINESS)
        event = self.event("billing.payment.succeeded", 5, order_id=order.order_id, payment_provider_ref="opaque", raw_payload={"provider": "raw"})
        with self.assertRaises(ValueError):
            self.service.handle_billing_event(event)


class SubscriptionLifecycleTests(CommercialTestCase):
    def create_subscription(self, counter: int = 10):
        order, _ = self.pay_order(ProductCode.BUILD_AND_RUN, counter)
        period = BillingPeriod(self.clock.now, self.clock.now + timedelta(days=30))
        created = self.event(
            "billing.subscription.created", counter + 1, order_id=order.order_id,
            subscription_provider_ref=f"opaque_subscription_{counter}",
            subscription_status=SubscriptionStatus.ACTIVE, current_period=period,
        )
        self.service.handle_billing_event(created)
        subscription = self.repo.get_subscription_by_provider_ref(order.tenant_id, order.company_id, created.subscription_provider_ref)
        return order, subscription

    def test_recurring_entitlement_activates_only_with_subscription(self):
        order, subscription = self.create_subscription()
        self.assertEqual(SubscriptionStatus.ACTIVE, subscription.status)
        self.assertTrue(self.service.capability_allowed(order.tenant_id, order.company_id, "ai_workforce.execute"))
        self.assertTrue(all(item.status is EntitlementStatus.ACTIVE for item in self.repo.get_current_entitlement_grants(order.tenant_id, order.company_id)))

    def test_semantically_repeated_subscription_creation_does_not_double_grant(self):
        order, subscription = self.create_subscription(11)
        before = len(self.repo.get_current_entitlement_grants(order.tenant_id, order.company_id))
        duplicate = self.event(
            "billing.subscription.created", 13, order_id=order.order_id,
            subscription_provider_ref=subscription.provider_ref,
            subscription_status=SubscriptionStatus.ACTIVE,
            current_period=subscription.billing_period,
        )
        self.service.handle_billing_event(duplicate)
        self.assertEqual(before, len(self.repo.get_current_entitlement_grants(order.tenant_id, order.company_id)))

    def test_cancel_at_period_end_expires_operations_but_retains_customer_assets(self):
        order, subscription = self.create_subscription(20)
        changed = self.service.request_subscription_cancellation(self.context, subscription.subscription_id, "No longer needed")
        self.assertEqual(SubscriptionStatus.CANCEL_AT_PERIOD_END, changed.status)
        self.assertTrue(self.service.capability_allowed(order.tenant_id, order.company_id, "ai_workforce.execute"))
        self.clock.now = subscription.billing_period.ends_at
        self.service.advance_time(order.tenant_id, order.company_id)
        grants = self.repo.get_current_entitlement_grants(order.tenant_id, order.company_id)
        managed = [item for item in grants if item.entitlement_class is EntitlementClass.STROMATION_MANAGED]
        owned = [item for item in grants if item.entitlement_class is EntitlementClass.CUSTOMER_OWNED]
        self.assertTrue(managed and all(item.status is EntitlementStatus.EXPIRED for item in managed))
        self.assertTrue(owned and all(item.status is EntitlementStatus.ACTIVE for item in owned))
        self.assertFalse(self.service.capability_allowed(order.tenant_id, order.company_id, "ai_workforce.execute"))
        self.assertTrue(self.service.capability_allowed(order.tenant_id, order.company_id, "company.read_export"))

    def test_payment_failure_grace_restriction_suspension_and_recovery(self):
        order, subscription = self.create_subscription(30)
        failed = self.event(
            "billing.payment.failed", 32,
            subscription_provider_ref=subscription.provider_ref, reason="renewal declined",
        )
        self.service.handle_billing_event(failed)
        past_due = self.repo.get_subscription(order.tenant_id, order.company_id, subscription.subscription_id)
        self.assertEqual(SubscriptionStatus.PAST_DUE, past_due.status)
        self.assertTrue(self.service.capability_allowed(order.tenant_id, order.company_id, "ai_workforce.execute"))
        self.clock.now += timedelta(days=2)
        self.service.advance_time(order.tenant_id, order.company_id)
        self.assertFalse(self.service.capability_allowed(order.tenant_id, order.company_id, "ai_workforce.execute"))
        recovered = self.event(
            "billing.subscription.updated", 33,
            subscription_provider_ref=subscription.provider_ref,
            subscription_status=SubscriptionStatus.ACTIVE,
            current_period=BillingPeriod(self.clock.now, self.clock.now + timedelta(days=30)),
            reason="payment recovered",
        )
        self.service.handle_billing_event(recovered)
        self.assertTrue(self.service.capability_allowed(order.tenant_id, order.company_id, "ai_workforce.execute"))

    def test_grace_expiry_suspends_subscription_without_deleting_history(self):
        order, subscription = self.create_subscription(40)
        self.service.handle_billing_event(self.event("billing.payment.failed", 42, subscription_provider_ref=subscription.provider_ref))
        self.clock.now += timedelta(days=8)
        self.service.advance_time(order.tenant_id, order.company_id)
        current = self.repo.get_subscription(order.tenant_id, order.company_id, subscription.subscription_id)
        self.assertEqual(SubscriptionStatus.SUSPENDED, current.status)
        self.assertGreaterEqual(len(self.repo.subscription_history(order.tenant_id, order.company_id, subscription.subscription_id)), 3)
        self.assertTrue(self.service.capability_allowed(order.tenant_id, order.company_id, "company.read_export"))

    def test_immediate_security_suspension_is_reasoned_and_audited(self):
        order, subscription = self.create_subscription(50)
        self.service.suspend_for_security(self.context, subscription.subscription_id, "Confirmed account abuse")
        self.assertFalse(self.service.capability_allowed(order.tenant_id, order.company_id, "ai_workforce.execute"))
        self.assertTrue(any(getattr(item, "reason", "") == "Confirmed account abuse" for item in self.repo.list_audit(order.tenant_id, order.company_id)))


class RefundTests(CommercialTestCase):
    def test_partial_then_full_refund_preserves_records_and_does_not_invent_policy(self):
        order, _ = self.pay_order(ProductCode.BUILD_BUSINESS, 60)
        self.service.handle_billing_event(self.event("billing.refund.created", 61, order_id=order.order_id, payment_provider_ref="opaque_payment_60", amount=Amount("USD", 50000), reason="Configured partial refund"))
        self.assertEqual(OrderStatus.PARTIALLY_REFUNDED, self.repo.get_order(order.tenant_id, order.company_id, order.order_id).status)
        self.service.handle_billing_event(self.event("billing.refund.created", 62, order_id=order.order_id, payment_provider_ref="opaque_payment_60", amount=Amount("USD", 200000), reason="Configured remaining refund"))
        self.assertEqual(OrderStatus.REFUNDED, self.repo.get_order(order.tenant_id, order.company_id, order.order_id).status)
        self.assertEqual(2, len(self.repo.list_refunds(order.tenant_id, order.company_id, order.order_id)))
        self.assertTrue(self.service.capability_allowed(order.tenant_id, order.company_id, "company.read_export"))

    def test_refund_cannot_exceed_order_total(self):
        order, _ = self.pay_order(ProductCode.BUILD_BUSINESS, 70)
        with self.assertRaises(CommercialConflict):
            self.service.handle_billing_event(self.event("billing.refund.created", 71, order_id=order.order_id, payment_provider_ref="opaque_payment_70", amount=Amount("USD", 300000)))


class SQLiteCommercialTests(CommercialTestCase):
    def test_commercial_ledger_and_processed_event_survive_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "commercial.sqlite")
            self.repo = SQLiteCommercialRepository(path)
            seed_default_catalog(self.repo, effective_at=NOW)
            self.service = CommercialService(self.repo, self.identity.authorization, self.events, id_factory=self.ids, clock=self.clock)
            order, event = self.pay_order(ProductCode.BUILD_BUSINESS, 80)
            self.repo.close()
            reopened = SQLiteCommercialRepository(path)
            self.assertEqual(OrderStatus.FULFILLMENT_PENDING, reopened.get_order(order.tenant_id, order.company_id, order.order_id).status)
            self.assertTrue(reopened.billing_event_processed(event.provider, event.provider_event_ref))
            self.assertTrue(reopened.get_current_entitlement_grants(order.tenant_id, order.company_id))
            with self.assertRaises(sqlite3.IntegrityError):
                reopened.connection.execute("DELETE FROM commercial_audit_events")
            reopened.close()


if __name__ == "__main__":
    unittest.main()
