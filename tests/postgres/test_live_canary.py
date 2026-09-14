from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import unittest
from uuid import uuid4

from businessbuilder.live_canary.models import CanarySendReservation, ReputationState, SenderIdentityReadiness
from businessbuilder.postgres import PostgresRuntimeRepository
from businessbuilder.postgres.migrations import MIGRATION_VERSION


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


@unittest.skipUnless(os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"),
                     "requires an isolated PostgreSQL test database")
class PostgresLiveCanaryTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        self.schema = f"{prefix}_canary_{uuid4().hex[:10]}"

    def test_migration_restart_scope_and_transaction_rollback(self):
        first = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        sender = SenderIdentityReadiness(
            "sender_pg_canary", "tenant_pg", "company_pg", "connection_pg",
            "sender@example.test", "example.test", True, True, True, True,
            True, True, ReputationState.GOOD, NOW)
        first.save_broker_record("live_canary_sender", sender.sender_id,
                                 sender.tenant_id, sender.company_id, sender)
        with self.assertRaises(RuntimeError):
            with first.transaction():
                first.save_broker_record("live_canary_sender", "sender_rollback",
                                         sender.tenant_id, sender.company_id,
                                         SenderIdentityReadiness(
                                             "sender_rollback", sender.tenant_id,
                                             sender.company_id, sender.provider_connection_id,
                                             "rollback@example.test", "example.test",
                                             True, True, True, True, True, True,
                                             ReputationState.GOOD, NOW))
                raise RuntimeError("rollback proof")
        first.close()
        second = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        self.assertEqual(sender, second.get_broker_record(
            "live_canary_sender", sender.tenant_id, sender.company_id, sender.sender_id))
        self.assertIsNone(second.get_broker_record(
            "live_canary_sender", sender.tenant_id, sender.company_id, "sender_rollback"))
        self.assertIsNone(second.get_broker_record(
            "live_canary_sender", "tenant_other", sender.company_id, sender.sender_id))
        with second.connection.cursor() as cursor:
            cursor.execute("SELECT version FROM bb_schema_migrations")
            self.assertEqual(MIGRATION_VERSION, cursor.fetchone()["version"])
        second.close()

    def test_concurrent_dispatchers_cannot_bypass_canary_cap(self):
        bootstrap = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        bootstrap.close()

        def reserve(index):
            repository = PostgresRuntimeRepository(self.dsn, schema=self.schema)
            try:
                value = CanarySendReservation(
                    f"reservation_pg_{index}", "permit_pg", "tenant_pg", "company_pg",
                    f"communication_pg_{index}", f"idempotency_pg_{index}", NOW)
                return repository.reserve_canary_send(value, 1, 1)
            finally:
                repository.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(reserve, range(6)))
        self.assertEqual(1, sum(1 for _, allowed, _ in results if allowed))
        self.assertEqual(5, sum(1 for _, allowed, reason in results
                                if not allowed and "cap exceeded" in reason))


if __name__ == "__main__":
    unittest.main()
