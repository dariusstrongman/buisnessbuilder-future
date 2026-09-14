from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import os
import unittest
from uuid import uuid4

from businessbuilder.customer_api.bootstrap import create_postgres_customer_api
from businessbuilder.access_broker import (
    ArtifactClassification,
    ArtifactRecord,
    ArtifactStatus,
)
from businessbuilder.identity import (
    FakeDevAuthenticationProvider,
    IdentityService,
    SessionService,
)
from businessbuilder.postgres import PostgresIdentityRepository
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.residential_cleaning import (
    DeterministicResidentialCleaningEvidenceVerifier,
)


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
            residential_cleaning_evidence_verifier=DeterministicResidentialCleaningEvidenceVerifier(),
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
        tenant_id = application.identity_repository.list_user_memberships(
            founder.user_id
        )[0].tenant_id
        action_id = "founder_action_cleaning_entity_admin"
        for operation in ("explain", "launch", "complete"):
            advanced = application.handle(
                method="POST",
                path=(
                    f"/api/v1/companies/{company_id}/residential-cleaning-pilot/"
                    f"founder-actions/{action_id}/{operation}"
                ),
                headers=headers,
                query={},
                body={"idempotency_key": f"postgres-entity-{operation}-0001"},
                request_id=f"request_postgres_entity_{operation}",
                correlation_id="correlation_postgres_entity",
            )
            self.assertEqual(200, advanced.status)
        artifact_id = "artifact_postgres_entity_authority"
        content_hash = sha256(artifact_id.encode()).hexdigest()
        application.runtime_repository.save_broker_record(
            "artifact",
            artifact_id,
            tenant_id,
            company_id,
            ArtifactRecord(
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                company_id=company_id,
                object_key=f"tenant/{tenant_id}/company/{company_id}/artifacts/{artifact_id}/{content_hash}",
                content_sha256=content_hash,
                content_type="application/pdf",
                size_bytes=128,
                classification=ArtifactClassification.VERIFICATION_EVIDENCE,
                status=ArtifactStatus.AVAILABLE,
                provenance_ref="verified_authority:texas_secretary_of_state",
                created_at=NOW,
            ),
        )
        captured = application.handle(
            method="POST",
            path=(
                f"/api/v1/companies/{company_id}/residential-cleaning-pilot/"
                f"founder-actions/{action_id}/evidence"
            ),
            headers=headers,
            query={},
            body={
                "idempotency_key": "postgres-entity-evidence-0001",
                "evidence_refs": [{"kind": "artifact", "reference": artifact_id}],
            },
            request_id="request_postgres_entity_evidence",
            correlation_id="correlation_postgres_entity",
        )
        self.assertEqual(200, captured.status)
        verified = application.residential_cleaning.verify_founder_action_test_only(
            tenant_id=tenant_id,
            company_id=company_id,
            action_id=action_id,
            idempotency_key="postgres-entity-verification-0001",
        )
        self.assertEqual("verified", verified["state"])
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
            len([item for item in recovered["founder_actions"] if item["state"] == "prepared"]),
            10,
        )
        self.assertFalse(recovered["verification"]["ready"])
        self.assertFalse(recovered["verification"]["fully_set"])
        recovered_action = next(
            item for item in recovered["founder_actions"]
            if item["founder_action_id"] == action_id
        )
        self.assertEqual("verified", recovered_action["state"])
        self.assertEqual("accepted", recovered_action["verification"]["result"])
        self.assertGreaterEqual(len(recovered_action["history"]), 6)
        self.assertEqual(2, len(recovered_action["evidence"] if recovered_action.get("evidence") else []))
        restarted.close()


if __name__ == "__main__":
    unittest.main()
