from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from businessbuilder.verification.catalog import default_registry
from businessbuilder.verification.fixtures import COMPANY_ID, FIXTURE_NOW, TENANT_ID, evidence, proposed_record
from businessbuilder.verification.models import DependencyRef, EvidenceType, VerificationMethod, VerificationState
from businessbuilder.verification.repository import InMemoryVerificationRepository, JsonVerificationRepository
from businessbuilder.verification.service import IllegalTransitionError, MissingEvidenceError, VerificationError, VerificationService


class VerificationStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = InMemoryVerificationRepository()
        self.registry = default_registry()
        self.service = VerificationService(self.repo, self.registry)

    def test_legal_state_progression(self) -> None:
        record = proposed_record("verification_legal", "website.links")
        self.service.create(record)
        executed = self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED, at=FIXTURE_NOW)
        tested = self.service.transition(
            TENANT_ID,
            COMPANY_ID,
            record.verification_id,
            VerificationState.TESTED,
            evidence=(evidence("evidence_link_test", EvidenceType.TEST_RESULT, test_name="link-crawl", passed=True),),
            at=FIXTURE_NOW,
        )
        verified = self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED, at=FIXTURE_NOW)
        self.assertEqual((executed.state, tested.state, verified.state), (VerificationState.EXECUTED, VerificationState.TESTED, VerificationState.VERIFIED))

    def test_cannot_jump_proposed_to_verified(self) -> None:
        record = proposed_record("verification_jump", "website.links")
        self.service.create(record)
        with self.assertRaisesRegex(IllegalTransitionError, "proposed -> verified"):
            self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED)

    def test_tested_requires_named_passing_defined_test(self) -> None:
        record = proposed_record("verification_no_test", "website.links")
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED)
        wrong = evidence("evidence_wrong_test", EvidenceType.TEST_RESULT, test_name="unregistered-test", passed=True)
        with self.assertRaises(MissingEvidenceError):
            self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.TESTED, evidence=(wrong,))

    def test_verified_requires_owner_scope_and_all_evidence(self) -> None:
        record = proposed_record("verification_form", "website.forms", owner=None)
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED)
        test = evidence("evidence_form_test", EvidenceType.TEST_RESULT, test_name="form-roundtrip", passed=True)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.TESTED, evidence=(test,))
        with self.assertRaisesRegex(MissingEvidenceError, "owner and scope"):
            self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED)

    def test_verified_requires_every_definition_evidence_type(self) -> None:
        record = proposed_record("verification_missing_api", "website.forms")
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED)
        test = evidence("evidence_form_only", EvidenceType.TEST_RESULT, test_name="form-roundtrip", passed=True)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.TESTED, evidence=(test,))
        with self.assertRaisesRegex(MissingEvidenceError, "api_response_reference"):
            self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED)

    def test_screenshot_is_not_functional_proof(self) -> None:
        record = proposed_record("verification_screenshot", "website.links")
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED)
        screenshot = evidence("evidence_screenshot", EvidenceType.SCREENSHOT)
        with self.assertRaises(MissingEvidenceError):
            self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.TESTED, evidence=(screenshot,))

    def test_expiry_removes_verified(self) -> None:
        record = proposed_record("verification_expiry", "website.links")
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED, at=FIXTURE_NOW)
        test = evidence(
            "evidence_expiring",
            EvidenceType.TEST_RESULT,
            test_name="link-crawl",
            passed=True,
            expires_at=FIXTURE_NOW + timedelta(hours=1),
        )
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.TESTED, evidence=(test,), at=FIXTURE_NOW)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED, at=FIXTURE_NOW)
        changed = self.service.expire_due(TENANT_ID, COMPANY_ID, at=FIXTURE_NOW + timedelta(hours=2))
        self.assertEqual(changed[0].state, VerificationState.EXPIRED)

    def test_expired_verification_can_reenter_execution_for_retest(self) -> None:
        record = proposed_record("verification_retest", "website.links")
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED, at=FIXTURE_NOW)
        test = evidence("evidence_retest_old", EvidenceType.TEST_RESULT, test_name="link-crawl", passed=True, expires_at=FIXTURE_NOW + timedelta(hours=1))
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.TESTED, evidence=(test,), at=FIXTURE_NOW)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED, at=FIXTURE_NOW)
        self.service.expire_due(TENANT_ID, COMPANY_ID, at=FIXTURE_NOW + timedelta(hours=2))
        reopened = self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED, at=FIXTURE_NOW + timedelta(hours=2))
        self.assertEqual(reopened.state, VerificationState.EXECUTED)

    def test_dependency_change_invalidates_test_and_verification(self) -> None:
        record = proposed_record(
            "verification_deploy_bound",
            "website.links",
            dependencies=(DependencyRef("website_deployment", 4, "website_deployment"),),
        )
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED, at=FIXTURE_NOW)
        test = evidence("evidence_deploy_bound", EvidenceType.TEST_RESULT, test_name="link-crawl", passed=True)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.TESTED, evidence=(test,), at=FIXTURE_NOW)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED, at=FIXTURE_NOW)
        changed = self.service.invalidate_dependency(TENANT_ID, COMPANY_ID, "website_deployment", 5, at=FIXTURE_NOW)
        self.assertEqual(changed[0].state, VerificationState.EXECUTED)
        self.assertIn("changed to version 5", changed[0].stale_reason or "")

    def test_failure_reverts_to_highest_supported_prior_state(self) -> None:
        record = proposed_record("verification_failure", "website.links")
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED, at=FIXTURE_NOW)
        test = evidence("evidence_failure_test", EvidenceType.TEST_RESULT, test_name="link-crawl", passed=True)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.TESTED, evidence=(test,), at=FIXTURE_NOW)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED, at=FIXTURE_NOW)
        failed = self.service.record_failure(TENANT_ID, COMPANY_ID, record.verification_id, "production check failed", at=FIXTURE_NOW)
        self.assertEqual(failed.state, VerificationState.TESTED)
        self.assertEqual(failed.failure_reason, "production check failed")
        with self.assertRaisesRegex(MissingEvidenceError, "new passing test evidence"):
            self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED, at=FIXTURE_NOW)

    def test_cross_company_evidence_is_rejected(self) -> None:
        record = proposed_record("verification_cross_company", "website.links")
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED)
        foreign = evidence("evidence_foreign", EvidenceType.TEST_RESULT, company_id="company_other", test_name="link-crawl", passed=True)
        with self.assertRaises(VerificationError):
            self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.TESTED, evidence=(foreign,))

    def test_tenant_and_company_isolation(self) -> None:
        self.service.create(proposed_record("verification_isolated", "website.links"))
        with self.assertRaises(KeyError):
            self.repo.get("tenant_other", COMPANY_ID, "verification_isolated")
        with self.assertRaises(KeyError):
            self.repo.get(TENANT_ID, "company_other", "verification_isolated")

    def test_provider_method_cannot_claim_human_review(self) -> None:
        record = proposed_record("verification_mobile", "website.mobile", method=VerificationMethod.AUTOMATED)
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED, at=FIXTURE_NOW)
        evidence_items = (
            evidence("evidence_mobile_test", EvidenceType.TEST_RESULT, test_name="mobile-viewport", passed=True),
            evidence("evidence_mobile_review", EvidenceType.HUMAN_REVIEW),
        )
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.TESTED, evidence=evidence_items, at=FIXTURE_NOW)
        with self.assertRaisesRegex(MissingEvidenceError, "method"):
            self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED, at=FIXTURE_NOW)

    def test_json_repository_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "verification.json"
            repo = JsonVerificationRepository(path)
            repo.save(proposed_record("verification_persisted", "website.links"))
            loaded = JsonVerificationRepository(path).get(TENANT_ID, COMPANY_ID, "verification_persisted")
            self.assertEqual(loaded.definition_id, "website.links")
            self.assertEqual(loaded.tenant_id, TENANT_ID)


if __name__ == "__main__":
    unittest.main()
