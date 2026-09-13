from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import unittest

from businessbuilder.runtime.approvals import ApprovalService
from businessbuilder.runtime.audit import to_audit_contract
from businessbuilder.runtime.capabilities import CapabilityRegistry
from businessbuilder.runtime.contracts import ContractValidator
from businessbuilder.runtime.fakes import (
    FakeCapability,
    FakeCrmCapability,
    FakeEmailCapability,
    FakeWebsiteCapability,
)
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.runtime.models import (
    ApprovalMode,
    Budget,
    CapabilityRequest,
    Event,
    FailureKind,
    JobStatus,
    Money,
    RetryPolicy,
)
from businessbuilder.runtime.orchestrator import IllegalTransition, JobOrchestrator
from businessbuilder.runtime.ports import FakeCompanyStateReader, RecordingVerificationPort
from businessbuilder.runtime.storage import SQLiteRuntimeRepository
from fixtures.runtime.billy_bob import FIXED_NOW, run_billy_bob, website_request


ROOT = Path(__file__).resolve().parents[2]


class RuntimeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = SQLiteRuntimeRepository()
        self.ids = DeterministicIds()
        self.companies = FakeCompanyStateReader({("tenant_one", "company_one")})
        self.verification = RecordingVerificationPort()
        self.registry = CapabilityRegistry()
        self.crm = FakeCrmCapability()
        self.email = FakeEmailCapability()
        self.website = FakeWebsiteCapability(ROOT / "contracts")
        for capability in (self.crm, self.email, self.website):
            self.registry.register(capability)
        self.runtime = JobOrchestrator(
            repository=self.repo,
            registry=self.registry,
            company_reader=self.companies,
            verification=self.verification,
            id_factory=self.ids,
            clock=lambda: FIXED_NOW,
        )
        self.runtime.budgets.create(
            Budget("budget_company_one", "tenant_one", "company_one", Money("USD", 1000)),
            "correlation_test",
        )

    def tearDown(self) -> None:
        self.repo.close()

    def make_job(self, **overrides):
        values = {
            "tenant_id": "tenant_one",
            "company_id": "company_one",
            "capability": "fake.crm.setup",
            "inputs": {"objective": "Configure a fake CRM capability"},
            "budget_ref": "budget_company_one",
            "per_job_ceiling": Money("USD", 200),
            "idempotency_key": "idempotency-test-0001",
            "correlation_id": "correlation_test",
        }
        values.update(overrides)
        return self.runtime.create_job(**values)


class EventsTests(RuntimeTestCase):
    def test_dispatch_is_synchronous_and_registration_ordered(self):
        calls = []
        self.runtime.events.subscribe("company.created", lambda event: calls.append((1, event.event_id)))
        self.runtime.events.subscribe("company.created", lambda event: calls.append((2, event.event_id)))
        event = Event(
            "event_created_001", "tenant_one", "company_one", "correlation_test", None,
            "company.created", FIXED_NOW, {"name": "Test"}, "company-brain.fake",
        )
        self.assertTrue(self.runtime.events.publish(event))
        self.assertEqual([(1, event.event_id), (2, event.event_id)], calls)
        ContractValidator(ROOT / "contracts").validate("event.schema.json", event.to_contract())

    def test_duplicate_event_is_stored_and_dispatched_once(self):
        calls = []
        self.runtime.events.subscribe("company.created", calls.append)
        event = Event(
            "event_duplicate_001", "tenant_one", "company_one", "correlation_test", None,
            "company.created", FIXED_NOW, {}, "test",
        )
        self.assertTrue(self.runtime.events.publish(event))
        self.assertFalse(self.runtime.events.publish(event))
        self.assertEqual(1, len(calls))


class RegistryAndLifecycleTests(RuntimeTestCase):
    def test_registry_registers_and_refuses_duplicates(self):
        self.assertIs(self.crm, self.registry.get("fake.crm.setup", "v1"))
        with self.assertRaises(ValueError):
            self.registry.register(FakeCrmCapability())
        with self.assertRaises(LookupError):
            self.registry.get("unknown.capability", "v1")

    def test_autonomous_job_lifecycle_and_contract_projection(self):
        job = self.make_job()
        self.assertEqual(JobStatus.RUNNABLE, job.status)
        job = self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        self.assertEqual(JobStatus.SUCCEEDED, job.status)
        self.assertEqual(1, job.attempts)
        ContractValidator(ROOT / "contracts").validate("job.schema.json", job.to_contract())

    def test_illegal_transition_never_silently_skips(self):
        job = self.make_job()
        job = self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        with self.assertRaises(IllegalTransition):
            self.runtime.transition_for_test(job, JobStatus.RUNNING)

    def test_duplicate_job_and_capability_request_has_one_side_effect(self):
        first = self.make_job()
        duplicate = self.make_job(job_id="job_should_not_be_created")
        self.assertEqual(first.job_id, duplicate.job_id)
        self.runtime.run(first.tenant_id, first.company_id, first.job_id)
        self.runtime.run(first.tenant_id, first.company_id, first.job_id)
        self.assertEqual(1, self.crm.execute_count)


class ApprovalPermissionTests(RuntimeTestCase):
    def test_approval_required_blocks_then_resumes(self):
        job = self.make_job(approval_mode=ApprovalMode.APPROVAL_REQUIRED)
        self.assertEqual(JobStatus.WAITING_APPROVAL, job.status)
        blocked = self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        self.assertEqual(0, self.crm.execute_count)
        self.assertEqual(FailureKind.APPROVAL_BLOCKED, blocked.failure.kind)
        job = self.runtime.approve_job(
            tenant_id=job.tenant_id, company_id=job.company_id, job_id=job.job_id,
            approval_id=job.approval_ids[0], actor_id="human_operator", actor_role="authorized_human",
        )
        self.assertEqual(JobStatus.RUNNABLE, job.status)
        self.assertEqual(JobStatus.SUCCEEDED, self.runtime.run(job.tenant_id, job.company_id, job.job_id).status)

    def test_founder_only_cannot_be_approved_by_nonfounder(self):
        job = self.make_job(approval_mode=ApprovalMode.FOUNDER_ONLY)
        with self.assertRaises(PermissionError):
            self.runtime.approve_job(
                tenant_id=job.tenant_id, company_id=job.company_id, job_id=job.job_id,
                approval_id=job.approval_ids[0], actor_id="human_operator", actor_role="authorized_human",
            )
        self.assertEqual(0, self.crm.execute_count)

    def test_expired_approval_does_not_resume_job(self):
        job = self.make_job(approval_mode=ApprovalMode.FOUNDER_ONLY)
        approval = self.repo.get_approval(job.tenant_id, job.company_id, job.approval_ids[0])
        approval.expires_at = FIXED_NOW - timedelta(seconds=1)
        self.repo.save_approval(approval)
        job = self.runtime.approve_job(
            tenant_id=job.tenant_id, company_id=job.company_id, job_id=job.job_id,
            approval_id=job.approval_ids[0], actor_id="founder_one", actor_role="founder",
        )
        self.assertEqual(JobStatus.WAITING_APPROVAL, job.status)

    def test_prohibited_action_fails_closed(self):
        job = self.make_job(approval_mode=ApprovalMode.PROHIBITED)
        self.assertEqual(JobStatus.FAILED, job.status)
        self.assertEqual(FailureKind.PROHIBITED, job.failure.kind)
        self.assertEqual(0, self.crm.execute_count)


class BudgetTests(RuntimeTestCase):
    def test_per_job_budget_refusal(self):
        job = self.make_job(per_job_ceiling=Money("USD", 100))
        job = self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        self.assertEqual(JobStatus.FAILED, job.status)
        self.assertEqual(FailureKind.BUDGET_BLOCKED, job.failure.kind)
        self.assertEqual(0, self.crm.execute_count)

    def test_company_budget_refusal_is_scope_bound(self):
        self.runtime.budgets.create(
            Budget("budget_tiny", "tenant_one", "company_one", Money("USD", 50)), "correlation_test"
        )
        job = self.make_job(budget_ref="budget_tiny", per_job_ceiling=Money("USD", 200))
        job = self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        self.assertEqual(FailureKind.BUDGET_BLOCKED, job.failure.kind)

    def test_reservation_settlement_and_unused_release_are_exact(self):
        job = self.make_job()
        job = self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        budget = self.repo.get_budget("tenant_one", "company_one", "budget_company_one")
        self.assertEqual(0, budget.reserved_minor)
        self.assertEqual(100, budget.settled_minor)
        self.assertEqual(900, budget.remaining_minor)
        self.assertEqual(100, job.settled_minor)

    def test_money_rejects_float_and_negative_paths(self):
        with self.assertRaises(ValueError):
            Money("usd", 1)
        with self.assertRaises(ValueError):
            Money("USD", -1)
        with self.assertRaises(TypeError):
            Money("USD", 1.5)  # type: ignore[arg-type]


class DependenciesRetriesCancellationTests(RuntimeTestCase):
    def test_dependency_blocks_until_dependency_succeeds(self):
        dependency = self.make_job(idempotency_key="dependency-test-0001")
        dependent = self.make_job(
            idempotency_key="dependent-test-0001", dependencies=(dependency.job_id,), capability="fake.email.setup"
        )
        self.assertEqual(JobStatus.WAITING_DEPENDENCIES, dependent.status)
        self.runtime.run(dependency.tenant_id, dependency.company_id, dependency.job_id)
        dependent = self.runtime.refresh(dependent)
        self.assertEqual(JobStatus.RUNNABLE, dependent.status)
        self.assertEqual(JobStatus.SUCCEEDED, self.runtime.run(dependent.tenant_id, dependent.company_id, dependent.job_id).status)

    def test_retryable_failure_is_bounded_then_succeeds(self):
        flaky = FakeCapability("fake.flaky.run", estimate_minor=100, failures_before_success=1)
        self.registry.register(flaky)
        job = self.make_job(capability="fake.flaky.run", retry_policy=RetryPolicy(2))
        first = self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        self.assertEqual(JobStatus.RUNNABLE, first.status)
        self.assertEqual(FailureKind.RETRYABLE, first.failure.kind)
        second = self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        self.assertEqual(JobStatus.SUCCEEDED, second.status)
        self.assertEqual(2, flaky.execute_count)

    def test_retries_stop_at_max_attempts(self):
        broken = FakeCapability("fake.broken.run", estimate_minor=100, failures_before_success=10)
        self.registry.register(broken)
        job = self.make_job(capability="fake.broken.run", retry_policy=RetryPolicy(2))
        self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        job = self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        self.assertEqual(JobStatus.FAILED, job.status)
        self.assertEqual(2, broken.execute_count)

    def test_permanent_failure_has_no_retry(self):
        broken = FakeCapability(
            "fake.permanent.run", estimate_minor=100, failures_before_success=1, retryable_failure=False
        )
        self.registry.register(broken)
        job = self.make_job(capability="fake.permanent.run")
        job = self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        self.assertEqual(JobStatus.FAILED, job.status)
        self.assertEqual(FailureKind.PERMANENT, job.failure.kind)
        self.assertEqual(1, broken.execute_count)

    def test_cancellation_is_explicit_and_idempotent(self):
        job = self.make_job(approval_mode=ApprovalMode.FOUNDER_ONLY)
        job = self.runtime.cancel(job.tenant_id, job.company_id, job.job_id)
        self.assertEqual(JobStatus.CANCELLED, job.status)
        self.assertEqual(FailureKind.CANCELLED, job.failure.kind)
        again = self.runtime.cancel(job.tenant_id, job.company_id, job.job_id)
        self.assertEqual(job.version, again.version)


class AuditScopeAndIntegrationTests(RuntimeTestCase):
    def test_audit_is_append_only_and_contract_compatible(self):
        job = self.make_job()
        self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        records = self.repo.list_audit("tenant_one", "company_one")
        self.assertGreaterEqual(len(records), 8)
        validator = ContractValidator(ROOT / "contracts")
        for record in records:
            validator.validate("audit-event.schema.json", to_audit_contract(record))
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.connection.execute("UPDATE audit_events SET body='{}'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.connection.execute("DELETE FROM audit_events")

    def test_failure_audit_remains_contract_compatible(self):
        broken = FakeCapability(
            "fake.audit_failure", estimate_minor=100, failures_before_success=1, retryable_failure=False
        )
        self.registry.register(broken)
        job = self.make_job(capability="fake.audit_failure", idempotency_key="audit-failure-test-0001")
        self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        validator = ContractValidator(ROOT / "contracts")
        for record in self.repo.list_audit("tenant_one", "company_one"):
            validator.validate("audit-event.schema.json", to_audit_contract(record))

    def test_tenant_and_company_scoping_prevents_cross_reads(self):
        job = self.make_job()
        self.assertIsNone(self.repo.get_job("tenant_other", "company_one", job.job_id))
        self.assertIsNone(self.repo.get_job("tenant_one", "company_other", job.job_id))
        with self.assertRaises(LookupError):
            self.runtime.run("tenant_other", "company_one", job.job_id)

    def test_verification_is_only_requested_through_port(self):
        job = self.make_job()
        self.runtime.run(job.tenant_id, job.company_id, job.job_id)
        self.assertEqual(1, len(self.verification.requests))
        self.assertEqual(job.job_id, self.verification.requests[0]["job_id"])

    def test_billy_bob_flow_is_complete_orchestration_proof(self):
        proof = run_billy_bob()
        try:
            event_types = [event["type"] for event in proof["events"]]
            self.assertEqual("company.created", event_types[0])
            self.assertIn("founder.approved", event_types)
            self.assertIn("capability.completed", event_types)
            self.assertEqual("verification.requested", event_types[-1])
            self.assertEqual(1, proof["website_execute_count"])
            self.assertEqual(1, len(proof["verification_requests"]))
            self.assertEqual(271, proof["job"].settled_minor)
        finally:
            proof["repository"].close()

    def test_fake_website_request_conforms_to_released_contract(self):
        ContractValidator(ROOT / "contracts").validate("website-capability.schema.json", website_request())

    def test_fake_website_result_conforms_to_released_contract(self):
        payload = website_request()
        request = CapabilityRequest(
            request_id="request_runtime_001",
            tenant_id="tenant_one",
            company_id=payload["company_id"],
            job_id=payload["job_id"],
            capability="fake.website.build",
            capability_version="v1",
            idempotency_key=payload["idempotency_key"],
            correlation_id=payload["correlation_id"],
            inputs={"website_request": payload},
            budget_ref=payload["budget_ref"]["id"],
        )
        self.website.validate_request(request)
        result = self.website.execute(request)
        contract_result = self.website.to_contract_result(request, result, FIXED_NOW)
        ContractValidator(ROOT / "contracts").validate("website-capability.schema.json", contract_result)


if __name__ == "__main__":
    unittest.main()
