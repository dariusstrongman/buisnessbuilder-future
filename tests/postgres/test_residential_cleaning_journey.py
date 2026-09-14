from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
import unittest
from uuid import uuid4

from businessbuilder.customer_api.bootstrap import create_postgres_customer_api
from businessbuilder.access_broker import (
    ArtifactClassification,
    ArtifactRecord,
    ArtifactStatus,
    InMemoryArtifactStore,
)
from businessbuilder.identity import (
    AuthorizationContext,
    FakeDevAuthenticationProvider,
    IdentityService,
    Permission,
    Role,
    SessionService,
)
from businessbuilder.postgres import PostgresIdentityRepository
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.residential_cleaning import (
    DeterministicMalwareScanner,
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
                    "starting_point": "started",
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
        self.assertEqual("started", journey["intake"]["data"]["starting_point"])
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
        self.assertEqual("started", status.body["journey"]["intake"]["data"]["starting_point"])
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

    def test_evidence_review_and_build_room_survive_restart(self) -> None:
        dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        schema = f"{prefix}_cleaning_review_{uuid4().hex[:10]}"
        ids = DeterministicIds()
        artifact_store = InMemoryArtifactStore(clock=lambda: NOW)

        identity_repository = PostgresIdentityRepository(dsn, schema=schema)
        identity = IdentityService(identity_repository, id_factory=ids, clock=lambda: NOW)
        founder = identity.register_user("postgres-evidence-founder@example.test")
        provider = FakeDevAuthenticationProvider()
        provider.register(founder.user_id, founder.email, "postgres-evidence-founder-proof")
        sessions = SessionService(identity_repository, provider, id_factory=ids, clock=lambda: NOW)
        founder_token = sessions.sign_in(founder.email, "postgres-evidence-founder-proof")[1]
        identity_repository.close()

        application = create_postgres_customer_api(
            signing_key=SIGNING_KEY,
            dsn=dsn,
            schema=schema,
            clock=lambda: NOW,
            enable_residential_cleaning_test_checkout=True,
            residential_cleaning_evidence_store=artifact_store,
            residential_cleaning_malware_scanner=DeterministicMalwareScanner(),
        )
        founder_headers = {"Authorization": f"Bearer {founder_token}"}
        started = application.handle(
            method="POST", path="/api/v1/pilots/residential-cleaning/intakes",
            headers=founder_headers, query={},
            body={
                "idempotency_key": "postgres-evidence-pilot-0001",
                "intake": {
                    "idea": "A residential cleaning pilot with safe evidence review.",
                    "founder_display_name": "Persistence Founder",
                    "organization_name": "Persistence Cleaning Organization",
                    "company_name": "Persistence Cleaning",
                    "country": "US", "region": "TX", "locality": "Denton",
                    "service_radius_miles": 10, "weekly_hours": 25,
                    "startup_budget_minor": 180000,
                    "working_preferences": {"owner_operated_at_launch": True},
                },
            },
            request_id="request_pg_evidence_start", correlation_id="correlation_pg_evidence",
        )
        self.assertEqual(201, started.status)
        journey = started.body["journey"]
        company_id = journey["company"]["company_id"]
        approved = application.handle(
            method="POST",
            path=f"/api/v1/companies/{company_id}/residential-cleaning-pilot/approve",
            headers=founder_headers, query={},
            body={"approval_id": journey["scope_commit"]["approval_id"]},
            request_id="request_pg_evidence_approve", correlation_id="correlation_pg_evidence",
        )
        self.assertEqual(200, approved.status)
        tenant_id = application.identity_repository.list_user_memberships(founder.user_id)[0].tenant_id
        action_id = "founder_action_cleaning_entity_admin"
        action_path = (
            f"/api/v1/companies/{company_id}/residential-cleaning-pilot/"
            f"founder-actions/{action_id}"
        )
        for operation in ("explain", "launch", "complete"):
            response = application.handle(
                method="POST", path=f"{action_path}/{operation}", headers=founder_headers,
                query={}, body={"idempotency_key": f"postgres-review-{operation}-0001"},
                request_id=f"request_pg_review_{operation}", correlation_id="correlation_pg_review",
            )
            self.assertEqual(200, response.status)

        operator = IdentityService(
            application.identity_repository, id_factory=ids, clock=lambda: NOW
        ).register_user("postgres-pilot-operator@example.test")
        provider.register(operator.user_id, operator.email, "postgres-operator-proof")
        operator_sessions = SessionService(
            application.identity_repository, provider, id_factory=ids, clock=lambda: NOW
        )
        operator_token = operator_sessions.sign_in(operator.email, "postgres-operator-proof")[1]
        identity = IdentityService(application.identity_repository, id_factory=ids, clock=lambda: NOW)
        context = AuthorizationContext(founder.user_id, tenant_id, company_id)
        membership = identity.invite_member(context, operator.user_id, Role.SUPPORT, reason="Pilot review")
        identity.accept_membership(membership.membership_id, operator.user_id)
        grant = identity.grant_support_access(
            context, operator.user_id, frozenset({Permission.ACCESS_ARTIFACTS}),
            timedelta(hours=1), "Pilot evidence review", company_id=company_id,
        )
        support = identity.start_support_session(operator.user_id, grant.grant_id, "Review evidence")
        operator_headers = {
            "Authorization": f"Bearer {operator_token}",
            "X-Support-Impersonation-Session": support.impersonation_session_id,
        }

        screenshot_content = b"\x89PNG\r\n\x1a\nSynthetic screenshot"
        screenshot = application.handle(
            method="POST", path=f"{action_path}/evidence-submissions",
            headers=founder_headers, query={},
            body={
                "idempotency_key": "postgres-screenshot-submission-0001",
                "source": "file_upload",
                "evidence_type": "supporting_screenshot",
                "filename": "authority-screen.png",
                "content_type": "image/png",
                "content_base64": base64.b64encode(screenshot_content).decode(),
            },
            request_id="request_pg_screenshot_submit", correlation_id="correlation_pg_review",
        )
        self.assertEqual(201, screenshot.status)
        screenshot_id = screenshot.body["evidence_submission"]["submission_id"]
        self.assertEqual("clean", screenshot.body["evidence_submission"]["scan_state"])
        more_required = application.handle(
            method="POST", path=f"{action_path}/evidence-reviews",
            headers=operator_headers, query={},
            body={
                "idempotency_key": "postgres-screenshot-review-0001",
                "submission_ids": [screenshot_id],
                "decision": "more_evidence_required",
                "reason_code": "authority_confirmation_missing",
                "requested_additional_evidence": ["Official authority confirmation"],
            },
            request_id="request_pg_screenshot_review", correlation_id="correlation_pg_review",
        )
        self.assertEqual(200, more_required.status)
        self.assertEqual("founder_completed", more_required.body["founder_action"]["state"])

        content = b"%PDF-1.7\nSynthetic persisted authority result\n%%EOF"
        content_hash = sha256(content).hexdigest()
        artifact_id = "artifact_postgres_review_authority"
        object_key = f"tenant/{tenant_id}/company/{company_id}/artifacts/{artifact_id}/{content_hash}"
        artifact_store.put(object_key, content, content_type="application/pdf", content_sha256=content_hash)
        application.runtime_repository.save_broker_record(
            "artifact", artifact_id, tenant_id, company_id,
            ArtifactRecord(
                artifact_id, tenant_id, company_id, object_key, content_hash,
                "application/pdf", len(content), ArtifactClassification.VERIFICATION_EVIDENCE,
                ArtifactStatus.AVAILABLE, "verified_authority:texas_secretary_of_state", NOW,
            ),
        )
        submitted = application.handle(
            method="POST", path=f"{action_path}/evidence-submissions",
            headers=founder_headers, query={},
            body={
                "idempotency_key": "postgres-authority-submission-0001",
                "source": "authority_reference",
                "evidence_type": "authority_confirmation",
                "reference": {"artifact_id": artifact_id},
                "supersedes_submission_id": screenshot_id,
            },
            request_id="request_pg_evidence_submit", correlation_id="correlation_pg_review",
        )
        self.assertEqual(201, submitted.status)
        submission_id = submitted.body["evidence_submission"]["submission_id"]
        reviewed = application.handle(
            method="POST", path=f"{action_path}/evidence-reviews",
            headers=operator_headers,
            query={},
            body={
                "idempotency_key": "postgres-operator-review-0001",
                "submission_ids": [submission_id],
                "decision": "accepted",
                "reason_code": "authority_evidence_matches",
            },
            request_id="request_pg_evidence_review", correlation_id="correlation_pg_review",
        )
        self.assertEqual(200, reviewed.status)
        self.assertEqual("verified", reviewed.body["founder_action"]["state"])
        application.close()

        restarted = create_postgres_customer_api(
            signing_key=SIGNING_KEY, dsn=dsn, schema=schema, clock=lambda: NOW,
            residential_cleaning_evidence_store=artifact_store,
            residential_cleaning_malware_scanner=DeterministicMalwareScanner(),
        )
        projection = restarted.handle(
            method="GET", path=f"/api/v1/companies/{company_id}/build-room",
            headers=founder_headers, query={}, body=None,
            request_id="request_pg_review_restart", correlation_id="correlation_pg_review_restart",
        )
        self.assertEqual(200, projection.status)
        action = next(
            item for item in projection.body["build_room"]["founder_actions"]
            if item["founder_action_id"] == action_id
        )
        self.assertEqual("verified", action["state"])
        self.assertEqual("accepted", action["evidence_review"]["review_state"])
        self.assertEqual(2, len(action["evidence_review"]["submissions"]))
        self.assertEqual(2, len(action["evidence_review"]["reviews"]))
        historical = {
            item["submission_id"]: item for item in action["evidence_review"]["submissions"]
        }
        self.assertEqual("clean", historical[screenshot_id]["scan_state"])
        self.assertEqual("more_evidence_required", historical[screenshot_id]["review_state"])
        self.assertEqual(screenshot_id, historical[submission_id]["supersedes_submission_id"])
        scan = restarted.runtime_repository.get_broker_record(
            "cleaning_evidence_scan", tenant_id, company_id, "scan_" + screenshot_id
        )
        self.assertEqual("clean", scan["state"])
        self.assertGreaterEqual(
            len(restarted.runtime_repository.list_audit(tenant_id, company_id)), 2
        )
        verification = restarted.verification.get(
            tenant_id, company_id, f"verification_{action_id}"
        )
        self.assertEqual("verified", verification.state.value)
        self.assertIn("human_review", {item.evidence_type.value for item in verification.evidence})
        restarted.close()


if __name__ == "__main__":
    unittest.main()
