from __future__ import annotations

import unittest

from businessbuilder.commercial import EntitlementStatus, OrderStatus, SubscriptionStatus
from businessbuilder.fixtures.billy_bob_commercial import run_billy_bob_commercial


class BillyBobCommercialIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = run_billy_bob_commercial()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.result["brain_repository"].close()
        cls.result["runtime_repository"].close()

    def test_account_order_payment_and_runtime_fulfillment_event(self):
        self.assertEqual(self.result["tenant_id"], self.result["organization"].tenant_id)
        self.assertEqual(OrderStatus.FULFILLMENT_PENDING, self.result["build_order"].status)
        self.assertTrue(self.result["fulfillment_events"])
        self.assertEqual("commercial.fulfillment.eligible", self.result["fulfillment_events"][0].type)

    def test_purchase_does_not_make_company_ready_or_fully_set(self):
        self.assertEqual("not_ready", self.result["readiness_before"])
        self.assertEqual(self.result["readiness_before"], self.result["readiness_after_purchase"])

    def test_run_subscription_ends_without_deleting_customer_owned_access(self):
        self.assertEqual(SubscriptionStatus.CANCELED, self.result["subscription"].status)
        self.assertTrue(self.result["managed_run_grants"])
        self.assertTrue(all(item.status is EntitlementStatus.EXPIRED for item in self.result["managed_run_grants"]))
        self.assertTrue(self.result["customer_owned_grants"])
        self.assertTrue(all(item.status is EntitlementStatus.ACTIVE for item in self.result["customer_owned_grants"]))
        self.assertTrue(self.result["commercial"].capability_allowed(self.result["tenant_id"], self.result["scope"].company_id, "company.read_export"))
        self.assertFalse(self.result["commercial"].capability_allowed(self.result["tenant_id"], self.result["scope"].company_id, "ai_workforce.execute"))

    def test_every_material_layer_has_audit_evidence(self):
        self.assertTrue(self.result["identity_audit"])
        self.assertTrue(self.result["commercial_audit"])
        self.assertTrue(self.result["runtime_events"])


if __name__ == "__main__":
    unittest.main()
