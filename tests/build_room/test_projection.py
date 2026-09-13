from __future__ import annotations

import json
import unittest

from businessbuilder.build_room.adapters import scoped
from businessbuilder.build_room.fixtures import COMPANY_ID, TENANT_ID, billy_bob_build_room
from businessbuilder.build_room.projection import ProjectionError, project_build_room


def empty_projection(**changes):
    values = {
        "generated_at": "2026-09-13T18:00:00Z",
        "source": scoped(TENANT_ID, COMPANY_ID),
        "company": {"tenant_id": TENANT_ID, "company_id": COMPANY_ID},
        "readiness": scoped(TENANT_ID, COMPANY_ID, ready=False, fully_set=False),
        "budget": scoped(TENANT_ID, COMPANY_ID, currency="USD", ceiling_minor=0, settled_minor=0, reserved_minor=0),
        "jobs": [], "verifications": [], "approvals": [], "evidence": [], "founder_actions": [],
        "blockers": [], "events": [], "handoff": scoped(TENANT_ID, COMPANY_ID),
    }
    values.update(changes)
    return project_build_room(**values)


class BuildRoomProjectionTests(unittest.TestCase):
    def test_billy_bob_projection_is_deterministic_and_customer_safe(self) -> None:
        first = billy_bob_build_room().to_dict()
        second = billy_bob_build_room().to_dict()
        self.assertEqual(first, second)
        self.assertEqual("build-room.projection.v1", first["schema_version"])
        self.assertEqual(TENANT_ID, first["company"]["tenant_id"])
        self.assertEqual(COMPANY_ID, first["company"]["company_id"])
        self.assertEqual(271, first["budget"]["settled_minor"])
        self.assertEqual(0, first["budget"]["reserved_minor"])
        self.assertEqual(729, first["budget"]["available_minor"])
        self.assertFalse(first["readiness"]["ready"])
        self.assertFalse(first["readiness"]["fully_set"])
        self.assertTrue(first["source"]["canonical"])
        self.assertFalse(first["source"]["mutable"])
        json.dumps(first, sort_keys=True)

    def test_package_ready_is_not_presented_as_deployment_or_ready(self) -> None:
        projection = billy_bob_build_room().to_dict()
        package = next(item for item in projection["work_items"] if item["id"] == "job_bb_website_001")
        deployment = next(item for item in projection["work_items"] if item["id"] == "verify_website_deployed")
        self.assertEqual("succeeded", package["status"])
        self.assertIn("does not mean deployed", package["detail"])
        self.assertNotEqual("verified", deployment["status"])
        self.assertFalse(projection["readiness"]["ready"])

    def test_projection_exposes_discrete_work_and_dependencies(self) -> None:
        projection = billy_bob_build_room().to_dict()
        self.assertEqual(15, projection["summary"]["total"])
        self.assertEqual(10, projection["summary"]["complete"])
        self.assertEqual(67, projection["summary"]["progress_percent"])
        self.assertTrue(all(item["id"] and item["owner"] and item["status"] for item in projection["work_items"]))
        self.assertEqual(
            ["job_bb_brain_001"],
            next(item for item in projection["work_items"] if item["id"] == "job_bb_website_001")["dependency_ids"],
        )
        sequences = [item["sequence"] for item in projection["timeline"]]
        self.assertEqual(sorted(sequences), sequences)
        self.assertEqual(len(sequences), len(set(sequences)))

    def test_cross_tenant_input_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProjectionError, "escaped tenant/company scope"):
            empty_projection(jobs=[{"tenant_id": "tenant_other", "company_id": COMPANY_ID}])

    def test_budget_overrun_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProjectionError, "exceeds its ceiling"):
            empty_projection(budget=scoped(TENANT_ID, COMPANY_ID, currency="USD", ceiling_minor=100, settled_minor=90, reserved_minor=20))

    def test_open_critical_blocker_forces_readiness_false(self) -> None:
        projection = empty_projection(
            readiness=scoped(TENANT_ID, COMPANY_ID, ready=True, fully_set=True),
            blockers=[{"tenant_id": TENANT_ID, "company_id": COMPANY_ID, "blocker_id": "critical", "severity": "critical", "open": True}],
        ).to_dict()
        self.assertFalse(projection["readiness"]["ready"])
        self.assertFalse(projection["readiness"]["fully_set"])

    def test_all_aggregate_envelopes_fail_closed_across_tenants(self) -> None:
        for field in ("source", "readiness", "budget", "handoff"):
            value = scoped("tenant_other", COMPANY_ID, ready=False, fully_set=False, currency="USD", ceiling_minor=0, settled_minor=0, reserved_minor=0)
            with self.subTest(field=field), self.assertRaisesRegex(ProjectionError, "escaped tenant/company scope"):
                empty_projection(**{field: value})

    def test_budget_minor_units_reject_coercible_values_and_booleans(self) -> None:
        for invalid in ("100", 1.5, True):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ProjectionError, "exact integer"):
                empty_projection(budget=scoped(TENANT_ID, COMPANY_ID, currency="USD", ceiling_minor=invalid, settled_minor=0, reserved_minor=0))

    def test_invalid_blockers_and_inconsistent_readiness_fail_closed(self) -> None:
        with self.assertRaisesRegex(ProjectionError, "blocker severity"):
            empty_projection(blockers=[{"tenant_id": TENANT_ID, "company_id": COMPANY_ID, "blocker_id": "unknown", "severity": "mystery", "open": True}])
        with self.assertRaisesRegex(ProjectionError, "Fully Set"):
            empty_projection(readiness=scoped(TENANT_ID, COMPANY_ID, ready=False, fully_set=True))

    def test_work_item_cannot_reference_absent_scoped_evidence(self) -> None:
        check = {"tenant_id": TENANT_ID, "company_id": COMPANY_ID, "id": "verify_x", "kind": "verification", "title": "Check", "owner": "QA", "status": "verified", "evidence_refs": ["artifact:missing"], "order": 1}
        with self.assertRaisesRegex(ProjectionError, "missing scoped evidence"):
            empty_projection(verifications=[check])
        job = {"tenant_id": TENANT_ID, "company_id": COMPANY_ID, "id": "job_x", "kind": "job", "title": "Build", "owner": "Runtime", "status": "succeeded", "evidence_refs": ["artifact:missing"], "order": 1}
        with self.assertRaisesRegex(ProjectionError, "missing scoped evidence"):
            empty_projection(jobs=[job])

    def test_every_billy_bob_work_evidence_reference_resolves(self) -> None:
        projection = billy_bob_build_room().to_dict()
        artifacts = {item["artifact_ref"] for item in projection["evidence"]}
        for item in projection["work_items"]:
            with self.subTest(item=item["id"]):
                self.assertTrue(all(reference.partition(":")[2] in artifacts for reference in item["evidence_refs"]))

    def test_true_and_false_readiness_states_remain_distinct(self) -> None:
        false_state = empty_projection().to_dict()["readiness"]
        true_state = empty_projection(readiness=scoped(TENANT_ID, COMPANY_ID, ready=True, fully_set=True)).to_dict()["readiness"]
        self.assertEqual((False, False), (false_state["ready"], false_state["fully_set"]))
        self.assertEqual((True, True), (true_state["ready"], true_state["fully_set"]))


if __name__ == "__main__":
    unittest.main()
