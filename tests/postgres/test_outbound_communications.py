from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import unittest
from uuid import uuid4

from businessbuilder.outbound_communications import (
    CommunicationPurpose, ConsentState, ContactRelationship, DestinationType,
    RateLimitReservation, RecipientRecord, SuppressionState,
)
from businessbuilder.postgres import PostgresRuntimeRepository


NOW = datetime(2026, 9, 13, 23, 0, tzinfo=timezone.utc)


@unittest.skipUnless(os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"),
                     "requires an isolated PostgreSQL test database")
class PostgresOutboundCommunicationTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        self.schema = f"{prefix}_communications_{uuid4().hex[:10]}"
        self.recipient = RecipientRecord(
            "recipient_pg_001", "tenant_pg", "company_pg", "pg@example.test",
            DestinationType.EMAIL, "event_pg_inbound", ContactRelationship.INBOUND_CUSTOMER,
            ConsentState.CUSTOMER_INITIATED, "canonical_inbound", NOW,
            SuppressionState.CLEAR, None, None, None, (), NOW, NOW)

    @staticmethod
    def limits():
        return {"company_minute": 10, "company_hour": 10, "company_day": 10,
                "recipient_minute": 1, "recipient_hour": 10,
                "recipient_day": 10, "burst": 10}

    def test_migration_restart_scope_and_rollback(self):
        first = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        first.save_broker_record("communication_recipient", self.recipient.recipient_id,
                                 self.recipient.tenant_id, self.recipient.company_id,
                                 self.recipient)
        with self.assertRaises(RuntimeError):
            with first.transaction():
                first.save_broker_record("communication_recipient", "recipient_rollback_001",
                    "tenant_pg", "company_pg", self.recipient.__class__(
                        "recipient_rollback_001", "tenant_pg", "company_pg",
                        "rollback@example.test", DestinationType.EMAIL, "event_pg_inbound",
                        ContactRelationship.INBOUND_CUSTOMER, ConsentState.CUSTOMER_INITIATED,
                        "canonical_inbound", NOW, SuppressionState.CLEAR, None, None, None, (), NOW, NOW))
                raise RuntimeError("rollback proof")
        first.close()
        second = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        self.assertEqual(self.recipient, second.get_broker_record(
            "communication_recipient", "tenant_pg", "company_pg", "recipient_pg_001"))
        self.assertIsNone(second.get_broker_record(
            "communication_recipient", "tenant_pg", "company_pg", "recipient_rollback_001"))
        self.assertIsNone(second.get_broker_record(
            "communication_recipient", "tenant_other", "company_pg", "recipient_pg_001"))
        second.close()

    def test_concurrent_rate_reservation_and_event_replay_are_atomic(self):
        repositories = [PostgresRuntimeRepository(self.dsn, schema=self.schema) for _ in range(2)]
        values = [RateLimitReservation(
            f"reservation_pg_{index:03d}", "tenant_pg", "company_pg", "recipient_pg_001",
            CommunicationPurpose.REPLY_TO_INBOUND, f"communication_pg_{index:03d}",
            f"idempotency_pg_{index:03d}", NOW) for index in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: pair[0].reserve_communication_send(
                pair[1], self.limits()), zip(repositories, values)))
        self.assertEqual(1, sum(item[1] for item in results))
        self.assertEqual(1, sum(item[2] == "recipient_minute rate limit exceeded" for item in results))
        with ThreadPoolExecutor(max_workers=2) as pool:
            events = list(pool.map(lambda repo: repo.claim_communication_event(
                "sandbox-email", "delivery_event_pg_001"), repositories))
        self.assertEqual(1, sum(events))
        for repository in repositories:
            repository.close()

    def test_company_rate_limit_serializes_different_recipients(self):
        repositories = [PostgresRuntimeRepository(self.dsn, schema=self.schema) for _ in range(2)]
        limits = self.limits(); limits["company_minute"] = 1; limits["recipient_minute"] = 10
        values = [RateLimitReservation(
            f"reservation_company_{index:03d}", "tenant_pg", "company_pg",
            f"recipient_pg_{index:03d}", CommunicationPurpose.QUOTE_RESPONSE,
            f"communication_company_{index:03d}", f"company_key_{index:03d}", NOW)
            for index in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: pair[0].reserve_communication_send(
                pair[1], limits), zip(repositories, values)))
        self.assertEqual(1, sum(item[1] for item in results))
        self.assertEqual(1, sum(item[2] == "company_minute rate limit exceeded" for item in results))
        for repository in repositories:
            repository.close()


if __name__ == "__main__":
    unittest.main()
