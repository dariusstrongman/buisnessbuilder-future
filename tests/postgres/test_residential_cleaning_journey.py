from __future__ import annotations

from datetime import datetime, timezone
import os
import unittest
from uuid import uuid4

from businessbuilder.customer_api.bootstrap import create_postgres_customer_api
from businessbuilder.identity import (
    FakeDevAuthenticationProvider,
    IdentityService,
    SessionService,
)
from businessbuilder.postgres import PostgresIdentityRepository
from businessbuilder.runtime.ids import DeterministicIds


NOW = datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc)
SIGNING_KEY = b"postgres-cleaning-journey-signing-key-v1"


@unittest.skipUnless(
    os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"),
    "requires an isolated PostgreSQL test database",
)
class PostgresResidentialCleaningJourneyTests(unittest.TestCase):
    def test_full_journey_and_restart_persistence(self) -> None:
        dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        schema = f"{prefix}_cleaning_{uuid4().hex[:10]}"
        ids = DeterministicIds()

        identity_repository = PostgresIdentityRepository(dsn, schema=schema)
        identity = IdentityService(
            identity_repository, id_factory=ids, clock=lambda: NOW
        )
        founder = identity.register_user("postgres-cleaning-founder@example.test")
        provider = FakeDevAuthenticationProvider()
        provider.register(founder.user_id, founder.email, "postgres-cleaning-proof")
        sessions = SessionService(
            identity_repository, provider, id_factory=ids, clock=lambda: NOW
        )
        _, token = sessions.sign_in(founder.email, "postgres-cleaning-proof")
        identity_repository.close()

        application = create_postgres_customer_api(
            signing_key=SIGNING_KEY,
            dsn=dsn,
            schema=schema,
            clock=lambda: NOW,
            enable_residential_cleaning_test_checkout=True,
        )
        headers = {"Authorization": f"Bearer {token}"}
        started = application.handle(
            method="POST",
            path="/api/v1/pilots/residential-cleaning/intakes",
            headers=headers,
            query={},
            body={
                "idempotency_key": "postgres-cleaning-pilot-0001",
                "intake": {
                    "idea": "Build a reliable residential cleaning service for busy Denton households.",
                    "founder_display_name": "PostgreSQL Founder",
                    "organization_name": "PostgreSQL Cleaning Organization",
                    "company_name": "PostgreSQL Cleaning",
                    "country": "US",
                    "region": "TX",
                    "locality": "Denton",
                    "service_radius_miles": 12,
                    "weekly_hours": 35,
                    "startup_budget_minor": 300000,
                    "working_preferences": {"owner_operated_at_launch": True},
                },
            },
            request_id="request_postgres_cleaning_start",
            correlation_id="correlation_postgres_cleaning",
        )
        self.assertEqual(201, started.status)
        journey = started.body["journey"]
        company_id = journey["company"]["company_id"]
        approved = application.handle(
            method="POST",
            path=f"/api/v1/companies/{company_id}/residential-cleaning-pilot/approve",
            headers=headers,
            query={},
            body={"approval_id": journey["scope_commit"]["approval_id"]},
            request_id="request_postgres_cleaning_approve",
            correlation_id="correlation_postgres_cleaning",
        )
        self.assertEqual(200, approved.status)
        self.assertEqual("succeeded", approved.body["journey"]["scope_commit"]["job_status"])
        self.assertEqual("fulfillment_pending", approved.body["journey"]["order"]["status"])
        self.assertFalse(approved.body["journey"]["verification"]["ready"])
        application.close()

        restarted = create_postgres_customer_api(
            signing_key=SIGNING_KEY,
            dsn=dsn,
            schema=schema,
            clock=lambda: NOW,
            enable_residential_cleaning_test_checkout=True,
        )
        status = restarted.handle(
            method="GET",
            path=f"/api/v1/companies/{company_id}/residential-cleaning-pilot",
            headers=headers,
            query={},
            body=None,
            request_id="request_postgres_cleaning_restart",
            correlation_id="correlation_postgres_cleaning_restart",
        )
        self.assertEqual(200, status.status)
        recovered = status.body["journey"]
        self.assertEqual("scope_committed", recovered["scope_commit"]["state"])
        self.assertEqual("succeeded", recovered["scope_commit"]["job_status"])
        self.assertEqual("fulfillment_pending", recovered["order"]["status"])
        self.assertGreaterEqual(
            len([item for item in recovered["founder_actions"] if item["state"] == "required"]),
            10,
        )
        self.assertFalse(recovered["verification"]["ready"])
        self.assertFalse(recovered["verification"]["fully_set"])
        restarted.close()


if __name__ == "__main__":
    unittest.main()
