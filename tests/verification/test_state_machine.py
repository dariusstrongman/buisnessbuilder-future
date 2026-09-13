from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
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

    def test_every_defined_scenario_must_pass(self) -> None:
        record = proposed_record("verification_quote_scenarios", "workflow.quote")
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED)
        standard_only = evidence(
            "evidence_quote_standard",
            EvidenceType.TEST_RESULT,
            test_name="quote-standard",
            passed=True,
        )
        with self.assertRaisesRegex(MissingEvidenceError, "quote-exception"):
            self.service.transition(
                TENANT_ID,
                COMPANY_ID,
                record.verification_id,
                VerificationState.TESTED,
                evidence=(standard_only,),
            )
        exception = evidence(
            "evidence_quote_exception",
            EvidenceType.TEST_RESULT,
            test_name="quote-exception",
            passed=True,
        )
        tested = self.service.transition(
            TENANT_ID,
            COMPANY_ID,
            record.verification_id,
            VerificationState.TESTED,
            evidence=(standard_only, exception),
        )
        self.assertEqual(VerificationState.TESTED, tested.state)

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

    def test_failure_retest_requires_every_defined_scenario_to_be_new(self) -> None:
        record = proposed_record("verification_quote_retest", "workflow.quote")
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED, at=FIXTURE_NOW)
        old_standard = evidence(
            "evidence_old_standard", EvidenceType.TEST_RESULT, test_name="quote-standard", passed=True
        )
        old_exception = evidence(
            "evidence_old_exception", EvidenceType.TEST_RESULT, test_name="quote-exception", passed=True
        )
        audit = evidence("evidence_quote_audit", EvidenceType.AUDIT_RECORD)
        self.service.transition(
            TENANT_ID,
            COMPANY_ID,
            record.verification_id,
            VerificationState.TESTED,
            evidence=(old_standard, old_exception, audit),
            at=FIXTURE_NOW,
        )
        self.service.transition(
            TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED, at=FIXTURE_NOW
        )
        self.service.record_failure(
            TENANT_ID, COMPANY_ID, record.verification_id, "quote regression", at=FIXTURE_NOW
        )
        new_standard = evidence(
            "evidence_new_standard", EvidenceType.TEST_RESULT, test_name="quote-standard", passed=True
        )
        with self.assertRaisesRegex(MissingEvidenceError, "quote-exception"):
            self.service.transition(
                TENANT_ID,
                COMPANY_ID,
                record.verification_id,
                VerificationState.VERIFIED,
                evidence=(new_standard,),
                at=FIXTURE_NOW,
            )
        new_exception = evidence(
            "evidence_new_exception", EvidenceType.TEST_RESULT, test_name="quote-exception", passed=True
        )
        verified = self.service.transition(
            TENANT_ID,
            COMPANY_ID,
            record.verification_id,
            VerificationState.VERIFIED,
            evidence=(new_standard, new_exception),
            at=FIXTURE_NOW,
        )
        self.assertEqual(VerificationState.VERIFIED, verified.state)

    def test_failure_retest_rejects_resubmitted_pre_failure_evidence(self) -> None:
        record = proposed_record("verification_failure_old_evidence", "website.links")
        self.service.create(record)
        self.service.transition(
            TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED, at=FIXTURE_NOW
        )
        old = evidence(
            "evidence_before_failure",
            EvidenceType.TEST_RESULT,
            test_name="link-crawl",
            passed=True,
            expires_at=FIXTURE_NOW + timedelta(days=1),
        )
        self.service.transition(
            TENANT_ID,
            COMPANY_ID,
            record.verification_id,
            VerificationState.TESTED,
            evidence=(old,),
            at=FIXTURE_NOW,
        )
        self.service.transition(
            TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED, at=FIXTURE_NOW
        )
        failed_at = FIXTURE_NOW + timedelta(hours=1)
        self.service.record_failure(
            TENANT_ID, COMPANY_ID, record.verification_id, "later regression", at=failed_at
        )
        with self.assertRaisesRegex(MissingEvidenceError, "new passing test evidence"):
            self.service.transition(
                TENANT_ID,
                COMPANY_ID,
                record.verification_id,
                VerificationState.VERIFIED,
                evidence=(old,),
                at=failed_at,
            )

        fresh = replace(old, evidence_id="evidence_after_failure", captured_at=failed_at)
        verified = self.service.transition(
            TENANT_ID,
            COMPANY_ID,
            record.verification_id,
            VerificationState.VERIFIED,
            evidence=(fresh,),
            at=failed_at,
        )
        self.assertEqual(VerificationState.VERIFIED, verified.state)

    def test_invalidation_retest_rejects_resubmitted_pre_invalidation_evidence(self) -> None:
        record = proposed_record(
            "verification_invalidation_old_evidence",
            "website.links",
            dependencies=(DependencyRef("website_deployment", 4, "website_deployment"),),
        )
        self.service.create(record)
        self.service.transition(
            TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED, at=FIXTURE_NOW
        )
        old = evidence(
            "evidence_before_invalidation",
            EvidenceType.TEST_RESULT,
            test_name="link-crawl",
            passed=True,
            expires_at=FIXTURE_NOW + timedelta(days=1),
        )
        self.service.transition(
            TENANT_ID,
            COMPANY_ID,
            record.verification_id,
            VerificationState.TESTED,
            evidence=(old,),
            at=FIXTURE_NOW,
        )
        self.service.transition(
            TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED, at=FIXTURE_NOW
        )
        invalidated_at = FIXTURE_NOW + timedelta(hours=1)
        self.service.invalidate_dependency(
            TENANT_ID,
            COMPANY_ID,
            "website_deployment",
            5,
            at=invalidated_at,
        )
        with self.assertRaisesRegex(MissingEvidenceError, "new passing test evidence"):
            self.service.transition(
                TENANT_ID,
                COMPANY_ID,
                record.verification_id,
                VerificationState.TESTED,
                evidence=(old,),
                at=invalidated_at,
            )

        fresh = replace(old, evidence_id="evidence_after_invalidation", captured_at=invalidated_at)
        tested = self.service.transition(
            TENANT_ID,
            COMPANY_ID,
            record.verification_id,
            VerificationState.TESTED,
            evidence=(fresh,),
            at=invalidated_at,
        )
        self.assertEqual(VerificationState.TESTED, tested.state)

    def test_cross_company_evidence_is_rejected(self) -> None:
        record = proposed_record("verification_cross_company", "website.links")
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED)
        foreign = evidence("evidence_foreign", EvidenceType.TEST_RESULT, company_id="company_other", test_name="link-crawl", passed=True)
        with self.assertRaises(VerificationError):
            self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.TESTED, evidence=(foreign,))

    def test_same_company_id_cross_tenant_evidence_is_rejected(self) -> None:
        record = proposed_record("verification_cross_tenant", "website.links")
        self.service.create(record)
        self.service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED)
        foreign = evidence(
            "evidence_foreign_tenant",
            EvidenceType.TEST_RESULT,
            tenant_id="tenant_other",
            company_id=COMPANY_ID,
            test_name="link-crawl",
            passed=True,
        )
        with self.assertRaisesRegex(VerificationError, "cross-tenant"):
            self.service.transition(
                TENANT_ID,
                COMPANY_ID,
                record.verification_id,
                VerificationState.TESTED,
                evidence=(foreign,),
            )

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
