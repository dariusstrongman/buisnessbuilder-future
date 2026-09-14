from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http import HTTPStatus
from pathlib import Path
import tempfile
import unittest

from businessbuilder.access_broker import (
    ArtifactClassification,
    ArtifactRecord,
    ArtifactStatus,
    InMemoryArtifactStore,
)
from businessbuilder.commercial import (
    CommercialService,
    InMemoryCommercialRepository,
    RecordingCommercialEventSink,
    seed_default_catalog,
)
from businessbuilder.company_brain import CompanyBrainService, SQLiteCompanyBrainRepository
from businessbuilder.customer_api import CustomerApi
from businessbuilder.identity import (
    AuthorizationContext,
    FakeDevAuthenticationProvider,
    IdentityService,
    Permission,
    PrincipalContextAuthority,
    Role,
    SessionService,
    SQLiteIdentityRepository,
)
from businessbuilder.integration import (
    CompanyBrainRuntimeAdapter,
    CompanyBrainVerificationAdapter,
    IdentityApprovalPrincipalVerifier,
)
from businessbuilder.residential_cleaning import (
    DeterministicMalwareScanner,
    PendingMalwareScanner,
    PilotConflict,
    ResidentialCleaningJourneyService,
    ResidentialCleaningVerificationRouter,
)
from businessbuilder.runtime.capabilities import CapabilityRegistry
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.runtime.ports import RecordingVerificationPort
from businessbuilder.runtime.storage import SQLiteRuntimeRepository
from businessbuilder.verification import (
    InMemoryVerificationRepository,
    ReadinessEvaluator,
    VerificationService,
    billy_bob_policy,
    default_registry,
)


NOW = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc)
ACTION_ID = "founder_action_cleaning_entity_admin"


class ResidentialCleaningEvidenceReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.ids = DeterministicIds()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.identity_repository = SQLiteIdentityRepository(str(root / "identity.sqlite"))
        self.identity = IdentityService(
            self.identity_repository, id_factory=self.ids, clock=lambda: self.now
        )
        self.auth_provider = FakeDevAuthenticationProvider()
        self.sessions = SessionService(
            self.identity_repository, self.auth_provider,
            id_factory=self.ids, clock=lambda: self.now,
        )
        self.authority = PrincipalContextAuthority(
            self.identity_repository,
            clock=lambda: self.now,
            signing_key=b"cleaning-evidence-review-test-key-v1",
        )
        self.brain_repository = SQLiteCompanyBrainRepository(str(root / "brain.sqlite"))
        self.brain_repository.migrate()
        self.brain = CompanyBrainService(self.brain_repository)
        self.runtime_repository = SQLiteRuntimeRepository(str(root / "runtime.sqlite"))
        self.runtime = JobOrchestrator(
            repository=self.runtime_repository,
            registry=CapabilityRegistry(),
            company_reader=CompanyBrainRuntimeAdapter(self.brain),
            verification=ResidentialCleaningVerificationRouter(RecordingVerificationPort()),
            id_factory=self.ids,
            clock=lambda: self.now,
            approval_principals=IdentityApprovalPrincipalVerifier(self.authority),
        )
        self.verification_repository = InMemoryVerificationRepository()
        self.verification = VerificationService(self.verification_repository, default_registry())
        self.commercial_repository = InMemoryCommercialRepository()
        seed_default_catalog(self.commercial_repository, effective_at=self.now)
        self.commercial = CommercialService(
            self.commercial_repository,
            self.identity.authorization,
            RecordingCommercialEventSink(),
            id_factory=self.ids,
            clock=lambda: self.now,
            auto_dispatch_outbox=False,
        )
        self.artifact_store = InMemoryArtifactStore(clock=lambda: self.now)
        self.journey = ResidentialCleaningJourneyService(
            identity_repository=self.identity_repository,
            principal_authority=self.authority,
            company_brain=self.brain,
            runtime=self.runtime,
            runtime_repository=self.runtime_repository,
            commercial=self.commercial,
            commercial_repository=self.commercial_repository,
            verification=self.verification,
            id_factory=self.ids,
            clock=lambda: self.now,
            enable_test_checkout=True,
            evidence_store=self.artifact_store,
            malware_scanner=DeterministicMalwareScanner(),
        )
        self.api = CustomerApi(
            identity_repository=self.identity_repository,
            principal_authority=self.authority,
            company_brain=self.brain,
            runtime=self.runtime,
            runtime_repository=self.runtime_repository,
            verification=self.verification,
            readiness=ReadinessEvaluator(self.verification_repository, billy_bob_policy()),
            company_snapshots=CompanyBrainVerificationAdapter(self.brain),
            commercial=self.commercial,
            commercial_repository=self.commercial_repository,
            id_factory=self.ids,
            clock=lambda: self.now,
            residential_cleaning=self.journey,
        )
        self.founder, self.founder_token = self._user("evidence-founder@example.test")
        self.tenant_id, self.company_id = self._start_and_approve()
        self._advance(ACTION_ID)
        self.operator, self.operator_token, self.support_session_id = self._operator()

    def tearDown(self) -> None:
        self.runtime_repository.close()
        self.brain_repository.close()
        self.identity_repository.close()
        self.temporary.cleanup()

    def _user(self, email: str):
        user = self.identity.register_user(email)
        proof = f"proof-{user.user_id}"
        self.auth_provider.register(user.user_id, email, proof)
        token = self.sessions.sign_in(email, proof, lifetime=timedelta(hours=1))[1]
        return user, token

    def request(self, method, path, *, token=None, body=None, headers=None):
        values = dict(headers or {})
        if token:
            values["Authorization"] = f"Bearer {token}"
        return self.api.handle(
            method=method,
            path=path,
            headers=values,
            query={},
            body=body,
            request_id="request_cleaning_evidence",
            correlation_id="correlation_cleaning_evidence",
        )

    def _start_and_approve(self):
        started = self.request(
            "POST",
            "/api/v1/pilots/residential-cleaning/intakes",
            token=self.founder_token,
            body={
                "idempotency_key": "cleaning-evidence-pilot-0001",
                "intake": {
                    "idea": "Start a safe residential cleaning company.",
                    "founder_display_name": "Evidence Founder",
                    "organization_name": "Evidence Cleaning Organization",
                    "company_name": "Evidence Cleaning",
                    "country": "US", "region": "TX", "locality": "Denton",
                    "service_radius_miles": 12,
                    "weekly_hours": 30,
                    "startup_budget_minor": 200000,
                    "working_preferences": {"owner_operated_at_launch": True},
                },
            },
        )
        self.assertEqual(HTTPStatus.CREATED, started.status)
        journey = started.body["journey"]
        company_id = journey["company"]["company_id"]
        approved = self.request(
            "POST",
            f"/api/v1/companies/{company_id}/residential-cleaning-pilot/approve",
            token=self.founder_token,
            body={"approval_id": journey["scope_commit"]["approval_id"]},
        )
        self.assertEqual(HTTPStatus.OK, approved.status)
        tenant_id = self.identity_repository.list_user_memberships(self.founder.user_id)[0].tenant_id
        return tenant_id, company_id

    def _advance(self, action_id):
        for index, operation in enumerate(("explain", "launch", "complete"), 1):
            result = self.request(
                "POST",
                f"{self._action_path(action_id)}/{operation}",
                token=self.founder_token,
                body={"idempotency_key": f"advance-{action_id}-{operation}-{index:04d}"},
            )
            self.assertEqual(HTTPStatus.OK, result.status)

    def _operator(self):
        operator, token = self._user("pilot-operator@example.test")
        context = AuthorizationContext(self.founder.user_id, self.tenant_id, self.company_id)
        membership = self.identity.invite_member(
            context, operator.user_id, Role.SUPPORT, reason="Pilot evidence review"
        )
        self.identity.accept_membership(membership.membership_id, operator.user_id)
        grant = self.identity.grant_support_access(
            context,
            operator.user_id,
            frozenset({Permission.ACCESS_ARTIFACTS}),
            timedelta(hours=1),
            "Review residential-cleaning evidence",
            company_id=self.company_id,
        )
        support = self.identity.start_support_session(
            operator.user_id, grant.grant_id, "Review pilot evidence"
        )
        return operator, token, support.impersonation_session_id

    def _action_path(self, action_id=ACTION_ID):
        return (
            f"/api/v1/companies/{self.company_id}/residential-cleaning-pilot/"
            f"founder-actions/{action_id}"
        )

    def _submission_path(self, action_id=ACTION_ID):
        return self._action_path(action_id) + "/evidence-submissions"

    def _reviews_path(self, action_id=ACTION_ID):
        return self._action_path(action_id) + "/evidence-reviews"

    def _authority_artifact(self, *, company_id=None, artifact_id="artifact_authority_evidence_001"):
        company_id = company_id or self.company_id
        content = b"%PDF-1.7\nSynthetic Texas authority confirmation\n%%EOF"
        digest = sha256(content).hexdigest()
        object_key = f"tenant/{self.tenant_id}/company/{company_id}/artifacts/{artifact_id}/{digest}"
        self.artifact_store.put(
            object_key, content, content_type="application/pdf", content_sha256=digest
        )
        record = ArtifactRecord(
            artifact_id, self.tenant_id, company_id, object_key, digest,
            "application/pdf", len(content), ArtifactClassification.VERIFICATION_EVIDENCE,
            ArtifactStatus.AVAILABLE, "verified_authority:texas_secretary_of_state", self.now,
        )
        self.runtime_repository.save_broker_record(
            "artifact", artifact_id, self.tenant_id, company_id, record
        )
        return record

    def _submit_authority(self, key="authority-submission-0001", artifact=None, **extra):
        artifact = artifact or self._authority_artifact()
        return self.request(
            "POST", self._submission_path(), token=self.founder_token,
            body={
                "idempotency_key": key,
                "source": "authority_reference",
                "evidence_type": "authority_confirmation",
                "reference": {"artifact_id": artifact.artifact_id},
                **extra,
            },
        )

    def _review(self, submission_ids, *, decision="accepted", key="operator-review-0001", **extra):
        action_id = extra.pop("action_id", ACTION_ID)
        return self.request(
            "POST", self._reviews_path(action_id), token=self.operator_token,
            headers={"X-Support-Impersonation-Session": self.support_session_id},
            body={
                "idempotency_key": key,
                "submission_ids": submission_ids,
                "decision": decision,
                "reason_code": extra.pop("reason_code", "evidence_matches_authority"),
                **extra,
            },
        )

    def _upload(self, *, content=None, content_type="application/pdf", filename="proof.pdf",
                evidence_type="supporting_document", key="upload-evidence-0001", **extra):
        content = content or b"%PDF-1.7\nSynthetic supporting document\n%%EOF"
        return self.request(
            "POST", self._submission_path(), token=self.founder_token,
            body={
                "idempotency_key": key,
                "source": "file_upload",
                "evidence_type": evidence_type,
                "filename": filename,
                "content_type": content_type,
                "content_base64": base64.b64encode(content).decode(),
                **extra,
            },
        )

    def test_authority_reference_review_drives_runtime_then_verification(self):
        submitted = self._submit_authority()
        self.assertEqual(HTTPStatus.CREATED, submitted.status)
        submission = submitted.body["evidence_submission"]
        self.assertEqual("pending_review", submission["review_state"])
        reviewed = self._review([submission["submission_id"]])
        self.assertEqual(HTTPStatus.OK, reviewed.status)
        self.assertEqual("accepted", reviewed.body["review"]["decision"])
        self.assertEqual("verified", reviewed.body["founder_action"]["state"])
        self.assertEqual("accepted", reviewed.body["founder_action"]["verification"]["result"])
        replayed = self._review([submission["submission_id"]])
        self.assertEqual(reviewed.body, replayed.body)
        projected = self.request(
            "GET", f"/api/v1/companies/{self.company_id}/build-room", token=self.founder_token
        )
        action = next(
            item for item in projected.body["build_room"]["founder_actions"]
            if item["founder_action_id"] == ACTION_ID
        )
        self.assertEqual("accepted", action["evidence_review"]["review_state"])
        self.assertEqual("not_applicable", action["evidence_review"]["submissions"][0]["scan_state"])
        verification = self.verification.get(
            self.tenant_id, self.company_id, f"verification_{ACTION_ID}"
        )
        self.assertIn("human_review", {item.evidence_type.value for item in verification.evidence})
        audit_actions = {item["action"] for item in self.runtime_repository.list_audit(self.tenant_id, self.company_id)}
        self.assertIn("cleaning.evidence.reviewed", audit_actions)

    def test_direct_evidence_transition_cannot_bypass_review(self):
        artifact = self._authority_artifact()
        bypass = self.request(
            "POST", f"{self._action_path()}/evidence", token=self.founder_token,
            body={
                "idempotency_key": "direct-evidence-bypass-0001",
                "evidence_refs": [{"kind": "artifact", "reference": artifact.artifact_id}],
            },
        )
        self.assertEqual(HTTPStatus.CONFLICT, bypass.status)
        self.assertEqual("evidence_submission_required", bypass.body["error"])

    def test_upload_quarantine_integrity_access_and_file_validation(self):
        uploaded = self._upload()
        self.assertEqual(HTTPStatus.CREATED, uploaded.status)
        submission = uploaded.body["evidence_submission"]
        self.assertEqual("clean", submission["scan_state"])
        access = self.request(
            "POST",
            f"{self._submission_path()}/{submission['submission_id']}/access",
            token=self.founder_token,
            body={},
        )
        self.assertEqual(HTTPStatus.OK, access.status)
        self.assertTrue(access.body["evidence_access"]["download_url"].startswith("memory-signed://"))
        mismatch = self._upload(
            content=b"not-a-pdf", key="upload-mime-mismatch-0001"
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, mismatch.status)
        unsupported = self._upload(
            content=b"MZ executable", content_type="application/octet-stream",
            filename="proof.exe", key="upload-unsupported-0001",
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, unsupported.status)
        quarantined = self._upload(
            content=b"%PDF-1.7\nDETERMINISTIC-MALWARE-MARKER\n%%EOF",
            key="upload-quarantined-0001",
        )
        self.assertEqual(HTTPStatus.CREATED, quarantined.status)
        self.assertEqual("rejected", quarantined.body["evidence_submission"]["scan_state"])
        quarantine_review = self._review(
            [quarantined.body["evidence_submission"]["submission_id"]],
            key="operator-quarantine-review-0001",
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, quarantine_review.status)
        oversized = self._upload(
            content=b"%PDF-" + b"x" * (512 * 1024),
            key="upload-oversized-0001",
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, oversized.status)
        artifact = self.runtime_repository.get_broker_record(
            "artifact", self.tenant_id, self.company_id,
            "artifact_" + submission["submission_id"],
        )
        self.artifact_store.tamper_for_test(artifact.object_key, b"tampered")
        denied = self._review(
            [submission["submission_id"]], key="operator-tamper-review-0001"
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, denied.status)

    def test_unconfigured_scanner_stays_quarantined_and_fails_closed(self):
        self.journey.evidence_reviews.scanner = PendingMalwareScanner()
        uploaded = self._upload(key="pending-scanner-upload-0001")
        self.assertEqual(HTTPStatus.CREATED, uploaded.status)
        submission = uploaded.body["evidence_submission"]
        self.assertEqual("pending_scan", submission["scan_state"])
        denied = self._review(
            [submission["submission_id"]], key="pending-scanner-review-0001"
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, denied.status)

    def test_founder_admin_and_forged_authority_cannot_review(self):
        submission = self._submit_authority().body["evidence_submission"]
        body = {
            "idempotency_key": "founder-review-denied-0001",
            "submission_ids": [submission["submission_id"]],
            "decision": "accepted",
            "reason_code": "self_review_attempt",
        }
        founder = self.request(
            "POST", self._reviews_path(), token=self.founder_token, body=body
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, founder.status)
        admin, admin_token = self._user("evidence-admin@example.test")
        membership = self.identity.invite_member(
            AuthorizationContext(self.founder.user_id, self.tenant_id, self.company_id),
            admin.user_id, Role.ADMIN, reason="test admin",
        )
        self.identity.accept_membership(membership.membership_id, admin.user_id)
        admin_result = self.request(
            "POST", self._reviews_path(), token=admin_token,
            body={**body, "idempotency_key": "admin-review-denied-0001"},
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, admin_result.status)
        forged = self.request(
            "POST", self._reviews_path(), token=self.operator_token,
            headers={
                "X-Support-Impersonation-Session": self.support_session_id,
                "X-Actor-Role": "support",
            },
            body={**body, "idempotency_key": "forged-review-denied-0001"},
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, forged.status)

    def test_idempotency_review_immutability_and_resubmission_history(self):
        screenshot = self._upload(
            content=b"\x89PNG\r\n\x1a\nsynthetic",
            content_type="image/png",
            filename="screen.png",
            evidence_type="supporting_screenshot",
            key="screenshot-submission-0001",
        )
        self.assertEqual(HTTPStatus.CREATED, screenshot.status)
        repeated = self._upload(
            content=b"\x89PNG\r\n\x1a\nsynthetic",
            content_type="image/png",
            filename="screen.png",
            evidence_type="supporting_screenshot",
            key="screenshot-submission-0001",
        )
        self.assertEqual(screenshot.body, repeated.body)
        conflict = self._upload(
            content=b"\x89PNG\r\n\x1a\ndifferent",
            content_type="image/png",
            filename="screen.png",
            evidence_type="supporting_screenshot",
            key="screenshot-submission-0001",
        )
        self.assertEqual(HTTPStatus.CONFLICT, conflict.status)
        submission_id = screenshot.body["evidence_submission"]["submission_id"]
        rejected = self._review(
            [submission_id], decision="more_evidence_required",
            key="review-more-needed-0001",
            reason_code="authority_confirmation_missing",
            requested_additional_evidence=["Official authority confirmation"],
        )
        self.assertEqual(HTTPStatus.OK, rejected.status)
        self.assertEqual("founder_completed", rejected.body["founder_action"]["state"])
        mutation = self._review(
            [submission_id], decision="accepted", key="review-more-needed-0001"
        )
        self.assertEqual(HTTPStatus.CONFLICT, mutation.status)
        artifact = self._authority_artifact(artifact_id="artifact_resubmitted_authority")
        resubmitted = self._submit_authority(
            key="authority-resubmission-0001", artifact=artifact,
            supersedes_submission_id=submission_id,
        )
        accepted = self._review(
            [resubmitted.body["evidence_submission"]["submission_id"]],
            key="review-resubmission-0001",
        )
        self.assertEqual(HTTPStatus.OK, accepted.status)
        self.assertEqual("verified", accepted.body["founder_action"]["state"])
        history = self.request("GET", self._reviews_path(), token=self.founder_token)
        self.assertEqual(2, len(history.body["evidence_reviews"]))
        self.assertEqual(
            {"more_evidence_required", "accepted"},
            {item["decision"] for item in history.body["evidence_reviews"]},
        )

    def test_screenshot_acceptance_does_not_satisfy_authority_or_verify(self):
        screenshot = self._upload(
            content=b"\x89PNG\r\n\x1a\nsynthetic screenshot",
            content_type="image/png", filename="authority.png",
            evidence_type="supporting_screenshot", key="screenshot-only-0001",
        )
        reviewed = self._review(
            [screenshot.body["evidence_submission"]["submission_id"]],
            key="screenshot-only-review-0001",
        )
        self.assertEqual(HTTPStatus.OK, reviewed.status)
        self.assertEqual("accepted", reviewed.body["review"]["decision"])
        self.assertEqual("founder_completed", reviewed.body["founder_action"]["state"])
        self.assertEqual("not_requested", reviewed.body["founder_action"]["verification"]["state"])

    def test_structured_reference_can_support_founder_attestation_only_action(self):
        action_id = "founder_action_cleaning_legal_name_address"
        self._advance(action_id)
        payload_hash = sha256(b"founder-approved-name-and-address-snapshot").hexdigest()
        submitted = self.request(
            "POST", self._submission_path(action_id), token=self.founder_token,
            body={
                "idempotency_key": "structured-reference-submit-0001",
                "source": "structured_reference",
                "evidence_type": "other_supported_reference",
                "reference": {
                    "reference_id": "founder_profile_snapshot_001",
                    "reference_kind": "company_brain_fact_snapshot",
                    "issued_at": "2026-09-14T18:00:00Z",
                    "content_sha256": payload_hash,
                },
            },
        )
        self.assertEqual(HTTPStatus.CREATED, submitted.status)
        reviewed = self._review(
            [submitted.body["evidence_submission"]["submission_id"]],
            key="structured-reference-review-0001",
            action_id=action_id,
        )
        self.assertEqual(HTTPStatus.OK, reviewed.status)
        self.assertEqual("verified", reviewed.body["founder_action"]["state"])

    def test_cross_scope_and_fake_references_fail_closed(self):
        second_company = "company_cleaning_secondary_scope"
        self.identity.attach_company(
            AuthorizationContext(self.founder.user_id, self.tenant_id, self.company_id),
            second_company,
        )
        foreign = self._authority_artifact(
            company_id=second_company, artifact_id="artifact_other_company_authority"
        )
        cross_company = self._submit_authority(
            key="cross-company-reference-0001", artifact=foreign
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, cross_company.status)
        fake_provider = self.request(
            "POST", self._submission_path(), token=self.founder_token,
            body={
                "idempotency_key": "fake-provider-reference-0001",
                "source": "provider_receipt",
                "evidence_type": "provider_receipt",
                "reference": {"provider_receipt_id": "receipt_forged_provider"},
            },
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, fake_provider.status)
        outsider, outsider_token = self._user("evidence-outsider@example.test")
        denied = self.request(
            "GET", self._submission_path(), token=outsider_token
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, denied.status)
        self.assertNotEqual(outsider.user_id, self.founder.user_id)

    def test_post_acceptance_content_substitution_invalidates_verification(self):
        artifact = self._authority_artifact(artifact_id="artifact_post_acceptance_tamper")
        submission = self._submit_authority(
            key="post-acceptance-submit-0001", artifact=artifact
        ).body["evidence_submission"]
        reviewed = self._review(
            [submission["submission_id"]], key="post-acceptance-review-0001"
        )
        self.assertEqual("verified", reviewed.body["founder_action"]["state"])
        self.artifact_store.tamper_for_test(artifact.object_key, b"substituted-after-acceptance")
        with self.assertRaises(PilotConflict):
            self.journey.verify_founder_action_internal(
                tenant_id=self.tenant_id,
                company_id=self.company_id,
                action_id=ACTION_ID,
                idempotency_key="post-acceptance-reverify-0001",
            )
        action = next(
            item for item in self.journey.project(
                self.authority.issue(
                    self.founder_token, tenant_id=self.tenant_id, company_id=self.company_id
                )
            )["founder_actions"]
            if item["founder_action_id"] == ACTION_ID
        )
        self.assertEqual("result_captured", action["state"])
        self.assertEqual("invalidated", action["verification"]["state"])


if __name__ == "__main__":
    unittest.main()
