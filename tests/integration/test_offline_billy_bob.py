from __future__ import annotations

import unittest

from businessbuilder.company_brain import (
    ConflictError,
    EntityRef,
    KnowledgeClass,
    NotFoundError,
    Provenance,
    RecordKind,
    Scope,
)
from businessbuilder.fixtures.billy_bob import COMPANY_ID, FIXED_NOW, JOB_ID, TENANT_ID, run_billy_bob
from businessbuilder.integration import CompanyBrainRuntimeAdapter, CompanyBrainVerificationAdapter
from businessbuilder.runtime.fakes import FakeCapability
from businessbuilder.runtime.models import ApprovalMode, Budget, JobStatus, Money
from businessbuilder.runtime.models import Event
from businessbuilder.verification import IllegalTransitionError, VerificationState


class OfflineBillyBobIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proof = run_billy_bob()

    def tearDown(self) -> None:
        self.proof["runtime_repository"].close()
        self.proof["brain_repository"].close()

    def test_real_subsystems_complete_the_end_to_end_flow(self) -> None:
        self.assertIsInstance(self.proof["brain_runtime_adapter"], CompanyBrainRuntimeAdapter)
        self.assertIsInstance(self.proof["brain_verification_adapter"], CompanyBrainVerificationAdapter)
        self.assertEqual(JobStatus.SUCCEEDED, self.proof["job"].status)
        self.assertEqual(1, self.proof["website_execute_count"])
        self.assertEqual(10, len(self.proof["proposed_verifications"]))
        self.assertTrue(all(item.state is VerificationState.PROPOSED for item in self.proof["proposed_verifications"]))
        self.assertEqual(271, self.proof["budget"].settled_minor)
        self.assertEqual(0, self.proof["budget"].reserved_minor)
        snapshot = self.proof["brain_verification_adapter"].get_snapshot(TENANT_ID, COMPANY_ID)
        self.assertIn("policy:policy_no_regulated_work", snapshot.facts)

    def test_ready_and_fully_set_transitions_are_owned_by_verification(self) -> None:
        self.assertFalse(self.proof["initial"].ready)
        self.assertFalse(self.proof["initial"].fully_set)
        self.assertTrue(self.proof["ready_only"].ready)
        self.assertFalse(self.proof["ready_only"].fully_set)
        self.assertTrue(self.proof["fully_set"].ready)
        self.assertTrue(self.proof["fully_set"].fully_set)

    def test_company_brain_dependency_change_invalidates_readiness(self) -> None:
        self.assertEqual(4, len(self.proof["invalidations"]))
        self.assertTrue(all(item.state is VerificationState.EXECUTED for item in self.proof["invalidations"]))
        self.assertTrue(all(item.stale_reason for item in self.proof["invalidations"]))
        self.assertFalse(self.proof["after_invalidation"].ready)
        self.assertFalse(self.proof["after_invalidation"].fully_set)

    def test_adapters_fail_closed_across_tenants(self) -> None:
        runtime_adapter = self.proof["brain_runtime_adapter"]
        verification_adapter = self.proof["brain_verification_adapter"]
        self.assertFalse(runtime_adapter.company_exists("tenant_other", COMPANY_ID))
        with self.assertRaises(NotFoundError):
            verification_adapter.get_snapshot("tenant_other", COMPANY_ID)
        with self.assertRaises(KeyError):
            self.proof["verification"].get("tenant_other", COMPANY_ID, self.proof["verification_request_ids"][0])

    def test_duplicate_runtime_verification_request_has_no_duplicate_side_effect(self) -> None:
        port = self.proof["verification_port"]
        count = len(self.proof["verification_records"])
        request_count = len(port.requests)
        event = Event(
            "event_duplicate_verification",
            TENANT_ID,
            COMPANY_ID,
            self.proof["job"].correlation_id,
            None,
            "verification.requested",
            FIXED_NOW,
            {
                "job_id": JOB_ID,
                "artifact_refs": [item.to_contract() for item in self.proof["job"].artifacts],
            },
            "integration-test",
        )
        self.proof["runtime"].events.subscribe(
            "verification.requested",
            lambda item: port.request_verification(
                tenant_id=item.tenant_id,
                company_id=item.company_id,
                job_id=item.payload["job_id"],
                artifact_refs=item.payload["artifact_refs"],
                correlation_id=item.correlation_id,
            ),
        )
        self.assertTrue(self.proof["runtime"].events.publish(event))
        self.assertFalse(self.proof["runtime"].events.publish(event))
        self.assertEqual(count, len(self.proof["verification"].list_for_company(TENANT_ID, COMPANY_ID)))
        self.assertEqual(request_count, len(port.requests))

    def test_stale_company_brain_record_write_is_rejected(self) -> None:
        brain = self.proof["brain"]
        scope = Scope(TENANT_ID, COMPANY_ID)
        record = next(
            item for item in brain.query_current_state(scope, kinds=(RecordKind.MARKET,))
            if item.record_id == "market_denton_12mi"
        )
        provenance = Provenance(
            "system", FIXED_NOW.isoformat().replace("+00:00", "Z"), EntityRef("agent", "integration_test")
        )
        with self.assertRaises(ConflictError):
            brain.update_approved_state(
                scope,
                record_id=record.record_id,
                kind=record.kind,
                data=dict(record.data),
                knowledge_class=KnowledgeClass.FACT,
                provenance=(provenance,),
                confidence=None,
                owner_ref=record.owner_ref,
                expected_version=1,
            )

    def test_invalid_verification_transition_is_rejected(self) -> None:
        record = self.proof["invalidations"][0]
        with self.assertRaises(IllegalTransitionError):
            self.proof["verification"].transition(
                TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.VERIFIED, at=FIXED_NOW
            )

    def test_budget_and_founder_only_approval_remain_enforced(self) -> None:
        runtime = self.proof["runtime"]
        runtime.registry.register(FakeCapability("fake.integration.budget", estimate_minor=200))
        runtime.budgets.create(
            Budget("budget_integration_tiny", TENANT_ID, COMPANY_ID, Money("USD", 100)),
            "correlation_integration_guard",
        )
        budget_job = runtime.create_job(
            tenant_id=TENANT_ID,
            company_id=COMPANY_ID,
            capability="fake.integration.budget",
            inputs={"objective": "Prove integrated budget enforcement"},
            budget_ref="budget_integration_tiny",
            per_job_ceiling=Money("USD", 200),
            idempotency_key="integration-budget-guard-0001",
            correlation_id="correlation_integration_guard",
        )
        self.assertEqual(JobStatus.FAILED, runtime.run(TENANT_ID, COMPANY_ID, budget_job.job_id).status)

        runtime.registry.register(FakeCapability("fake.integration.approval", estimate_minor=1))
        approval_job = runtime.create_job(
            tenant_id=TENANT_ID,
            company_id=COMPANY_ID,
            capability="fake.integration.approval",
            inputs={"objective": "Prove integrated founder approval enforcement"},
            budget_ref="budget_bb_company",
            per_job_ceiling=Money("USD", 1),
            idempotency_key="integration-approval-guard-0001",
            correlation_id="correlation_integration_guard",
            approval_mode=ApprovalMode.FOUNDER_ONLY,
        )
        with self.assertRaises(PermissionError):
            runtime.approve_job(
                tenant_id=TENANT_ID,
                company_id=COMPANY_ID,
                job_id=approval_job.job_id,
                approval_id=approval_job.approval_ids[0],
                actor_id="operator_not_founder",
                actor_role="authorized_human",
            )


if __name__ == "__main__":
    unittest.main()
