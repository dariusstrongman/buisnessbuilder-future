from __future__ import annotations

import unittest
from dataclasses import replace

from businessbuilder.verification.catalog import default_registry
from businessbuilder.verification.fixtures import (
    COMPANY_ID,
    FIXTURE_NOW,
    TENANT_ID,
    billy_snapshot,
    populate_fully_set,
    populate_ready,
    READY_DEFINITIONS,
    verify_definition,
)
from businessbuilder.verification.models import Blocker, BlockerSeverity, FounderActionStatus
from businessbuilder.verification.ports import FounderActionSnapshot
from businessbuilder.verification.readiness import ReadinessEvaluator, billy_bob_policy
from businessbuilder.verification.repository import InMemoryVerificationRepository
from businessbuilder.verification.service import VerificationService


def blocker(severity: BlockerSeverity, blocker_id: str = "blocker_fixture") -> Blocker:
    return Blocker(
        blocker_id=blocker_id,
        tenant_id=TENANT_ID,
        company_id=COMPANY_ID,
        severity=severity,
        source="verification-test",
        affected_target="launch",
        reason="fixture reason",
        remediation="fixture remediation",
        owner="party_billy",
    )


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = InMemoryVerificationRepository()
        self.registry = default_registry()
        self.service = VerificationService(self.repo, self.registry)
        self.evaluator = ReadinessEvaluator(self.repo, billy_bob_policy())

    def test_initial_state_is_neither_ready_nor_fully_set(self) -> None:
        result = self.evaluator.evaluate(billy_snapshot(), at=FIXTURE_NOW)
        self.assertFalse(result.ready)
        self.assertFalse(result.fully_set)

    def test_offer_and_service_area_are_not_enough(self) -> None:
        result = self.evaluator.evaluate(billy_snapshot(offer=True, service_area=True), at=FIXTURE_NOW)
        self.assertFalse(result.ready)
        self.assertIn("lead_intake", result.unmet_ready)

    def test_verified_customer_path_makes_ready_but_not_fully_set(self) -> None:
        populate_ready(self.service, self.registry)
        result = self.evaluator.evaluate(billy_snapshot(offer=True, service_area=True), at=FIXTURE_NOW)
        self.assertTrue(result.ready)
        self.assertFalse(result.fully_set)
        self.assertIn("monitoring", result.unmet_fully_set)

    def test_each_deployment_and_qa_requirement_blocks_ready_when_omitted(self) -> None:
        required = {"website.deployed", "website.https", "website.links", "website.mobile"}
        for omitted in sorted(required):
            with self.subTest(omitted=omitted):
                repo = InMemoryVerificationRepository()
                service = VerificationService(repo, self.registry)
                evaluator = ReadinessEvaluator(repo, billy_bob_policy())
                for sequence, definition_id in enumerate(
                    (item for item in READY_DEFINITIONS if item != omitted), 1
                ):
                    verify_definition(service, self.registry, definition_id, sequence)
                result = evaluator.evaluate(
                    billy_snapshot(offer=True, service_area=True), at=FIXTURE_NOW
                )
                self.assertFalse(result.ready)
                self.assertIn(
                    {
                        "website.deployed": "website_deployed",
                        "website.https": "website_https",
                        "website.links": "website_links",
                        "website.mobile": "website_mobile",
                    }[omitted],
                    result.unmet_ready,
                )

    def test_failed_deployment_verification_blocks_ready(self) -> None:
        populate_ready(self.service, self.registry)
        deployed = next(
            item
            for item in self.repo.list_for_company(TENANT_ID, COMPANY_ID)
            if item.definition_id == "website.deployed"
        )
        self.service.record_failure(
            TENANT_ID,
            COMPANY_ID,
            deployed.verification_id,
            "offline deployment smoke failed",
            at=FIXTURE_NOW,
        )
        result = self.evaluator.evaluate(
            billy_snapshot(offer=True, service_area=True), at=FIXTURE_NOW
        )
        self.assertFalse(result.ready)
        self.assertIn("website_deployed", result.unmet_ready)

    def test_all_selected_requirements_make_fully_set(self) -> None:
        populate_fully_set(self.service, self.registry)
        result = self.evaluator.evaluate(
            billy_snapshot(offer=True, service_area=True, admin_complete=True), at=FIXTURE_NOW
        )
        self.assertTrue(result.ready)
        self.assertTrue(result.fully_set)

    def test_critical_blocker_prevents_both(self) -> None:
        populate_fully_set(self.service, self.registry)
        result = self.evaluator.evaluate(
            billy_snapshot(offer=True, service_area=True, admin_complete=True),
            (blocker(BlockerSeverity.CRITICAL),),
            at=FIXTURE_NOW,
        )
        self.assertFalse(result.ready)
        self.assertFalse(result.fully_set)

    def test_noncritical_blocker_can_allow_ready(self) -> None:
        populate_ready(self.service, self.registry)
        result = self.evaluator.evaluate(
            billy_snapshot(offer=True, service_area=True),
            (blocker(BlockerSeverity.NONCRITICAL),),
            at=FIXTURE_NOW,
        )
        self.assertTrue(result.ready)

    def test_required_critical_founder_action_prevents_ready(self) -> None:
        populate_ready(self.service, self.registry)
        snapshot = billy_snapshot(offer=True, service_area=True)
        snapshot = replace(
            snapshot,
            founder_actions=snapshot.founder_actions
            + (FounderActionSnapshot("founder_action_payment", FounderActionStatus.SUBMITTED, critical=True),),
        )
        result = self.evaluator.evaluate(snapshot, at=FIXTURE_NOW)
        self.assertFalse(result.ready)
        self.assertIn("founder_action_payment", result.unmet_ready)

    def test_material_waiver_prevents_fully_set(self) -> None:
        populate_fully_set(self.service, self.registry)
        result = self.evaluator.evaluate(
            billy_snapshot(
                offer=True,
                service_area=True,
                admin_complete=True,
                material_waivers=("insurance-deferred",),
            ),
            at=FIXTURE_NOW,
        )
        self.assertTrue(result.ready)
        self.assertFalse(result.fully_set)

    def test_changed_website_dependency_recalculates_readiness(self) -> None:
        populate_ready(self.service, self.registry)
        snapshot = billy_snapshot(offer=True, service_area=True)
        self.assertTrue(self.evaluator.evaluate(snapshot, at=FIXTURE_NOW).ready)
        self.service.invalidate_dependency(TENANT_ID, COMPANY_ID, "website_deployment", 2, at=FIXTURE_NOW)
        result = self.evaluator.evaluate(snapshot, at=FIXTURE_NOW)
        self.assertFalse(result.ready)
        self.assertIn("lead_intake", result.unmet_ready)

    def test_foreign_company_blocker_does_not_leak(self) -> None:
        populate_ready(self.service, self.registry)
        foreign = replace(blocker(BlockerSeverity.CRITICAL), company_id="company_other")
        result = self.evaluator.evaluate(
            billy_snapshot(offer=True, service_area=True), (foreign,), at=FIXTURE_NOW
        )
        self.assertTrue(result.ready)


if __name__ == "__main__":
    unittest.main()
