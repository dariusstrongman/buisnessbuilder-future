from __future__ import annotations

import os
import unittest

from businessbuilder.postgres.cloud_proof import run_cloud_proof


@unittest.skipUnless(
    os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"),
    "requires an isolated PostgreSQL test database",
)
class PostgresCloudProofTests(unittest.TestCase):
    def test_commercial_flow_is_idempotent_persistent_and_tenant_safe(self) -> None:
        dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        schema = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        proof_id = "automated-postgres-cloud-proof-v1"

        first = run_cloud_proof(dsn, schema=schema, proof_id=proof_id)
        restarted = run_cloud_proof(dsn, schema=schema, proof_id=proof_id)

        self.assertEqual("passed", first["status"])
        self.assertEqual("postgresql", first["backend"])
        self.assertFalse(first["live_stripe"])
        self.assertFalse(first["loaded_from_persistence"])
        self.assertTrue(restarted["loaded_from_persistence"])
        self.assertTrue(all(first["billing_idempotency"].values()))
        self.assertTrue(all(first["restart_persistence"].values()))
        self.assertTrue(all(first["tenant_isolation"].values()))
        self.assertEqual("verification", first["readiness"]["authority"])
        self.assertFalse(first["readiness"]["ready_after_purchase"])
        self.assertFalse(first["readiness"]["fully_set_after_purchase"])
        self.assertEqual("canceled", first["build_and_run"]["subscription_status"])
        self.assertTrue(first["build_and_run"]["managed_operations_expired"])
        self.assertTrue(first["build_and_run"]["customer_owned_state_active"])


if __name__ == "__main__":
    unittest.main()
