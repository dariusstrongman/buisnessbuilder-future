from __future__ import annotations

import unittest
from pathlib import Path

from businessbuilder.build_room.adapters import adapt_approval, adapt_blocker, adapt_event, adapt_founder_action, adapt_job
from businessbuilder.build_room.fixtures import canonical_billy_bob_records
from businessbuilder.runtime.contracts import ContractValidator
from businessbuilder.runtime.models import Event, Job, JobStatus, Money, RetryPolicy, ApprovalMode
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[2]


class BuildRoomAdapterTests(unittest.TestCase):
    def test_fixture_inputs_validate_against_released_v2_contracts(self) -> None:
        records = canonical_billy_bob_records()
        validator = ContractValidator(ROOT / "contracts")
        for kind, schema in (
            ("jobs", "job.v2.schema.json"), ("verifications", "verification.v2.schema.json"),
            ("approvals", "approval.v2.schema.json"), ("founder_actions", "founder-action.v2.schema.json"),
            ("events", "event.v2.schema.json"),
        ):
            for record in records[kind]:
                with self.subTest(kind=kind, record=record):
                    validator.validate(schema, record)
        validator.validate("budget-spend.v2.schema.json", records["budget"])

    def test_requested_approval_preserves_null_decision_time(self) -> None:
        requested = canonical_billy_bob_records()["approvals"][-1]
        projected = adapt_approval(requested, title="Publish", summary="Waiting")
        self.assertEqual("requested", projected["state"])
        self.assertIsNone(projected["decided_at"])

    def test_expired_and_superseded_approvals_preserve_null_decision_time(self) -> None:
        validator = ContractValidator(ROOT / "contracts")
        for state in ("expired", "superseded"):
            approval = dict(canonical_billy_bob_records()["approvals"][-1])
            approval["state"] = state
            validator.validate("approval.v2.schema.json", approval)
            with self.subTest(state=state):
                self.assertIsNone(adapt_approval(approval, title="Inactive", summary="No decision was made")["decided_at"])

    def test_actual_decision_states_require_decision_time(self) -> None:
        approval = dict(canonical_billy_bob_records()["approvals"][0])
        approval["decided_at"] = None
        with self.assertRaisesRegex(ValueError, "decisions require"):
            adapt_approval(approval, title="Decision", summary="Missing timestamp")

    def test_invalid_founder_action_state_is_rejected(self) -> None:
        action = dict(canonical_billy_bob_records()["founder_actions"][0])
        action["state"] = "blocked"
        with self.assertRaisesRegex(ValueError, "state/instructions"):
            adapt_founder_action(action)

    def test_incomplete_or_extended_v2_record_is_rejected(self) -> None:
        job = dict(canonical_billy_bob_records()["jobs"][0])
        del job["idempotency_key"]
        with self.assertRaisesRegex(ValueError, "missing required fields"):
            adapt_job(job, title="Build", owner="Runtime")
        job = dict(canonical_billy_bob_records()["jobs"][0])
        job["presentation_title"] = "not canonical"
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            adapt_job(job, title="Build", owner="Runtime")

    def test_invalid_job_status_and_event_visibility_fail_closed(self) -> None:
        records = canonical_billy_bob_records()

        job = dict(records["jobs"][0])
        job["status"] = "invented"
        with self.assertRaisesRegex(ValueError, "job status is invalid"):
            adapt_job(job, title="Build", owner="Runtime")

        event = dict(records["events"][0])
        event["visibility"] = "public"
        with self.assertRaisesRegex(ValueError, "event visibility is invalid"):
            adapt_event(event)

    def test_runtime_domain_job_and_event_are_accepted_by_adapters(self) -> None:
        now = datetime(2026, 9, 13, tzinfo=timezone.utc)
        job = Job(
            "job_domain", "tenant_domain", "company_domain", "fake.website.build", "v1",
            {"objective": "Build a bounded offline website package."}, (), JobStatus.QUEUED, 0,
            RetryPolicy(), "budget_domain", Money("USD", 100), ApprovalMode.AUTONOMOUS, (), (), None,
            "domain-job-idempotency", "correlation_domain", None, {}, now, now,
        )
        event = Event("event_domain", "tenant_domain", "company_domain", "correlation_domain", None, "job.started", now, {"customer_label": "Started"}, "runtime")
        self.assertEqual("job_domain", adapt_job(job, title="Build", owner="Runtime")["id"])
        self.assertEqual("event_domain", adapt_event(event)["event_id"])

    def test_blocker_requires_complete_typed_display_fields(self) -> None:
        blocker = {
            "blocker_id": "blocker_domain", "tenant_id": "tenant_domain", "company_id": "company_domain",
            "severity": "critical", "affected_target": "website.deployed", "reason": "Ownership missing",
            "remediation": "Confirm the owned account", "owner": "Founder", "open": True,
        }
        self.assertEqual("blocker_domain", adapt_blocker(blocker)["blocker_id"])
        for field, invalid in (("owner", ""), ("reason", 123)):
            candidate = dict(blocker)
            candidate[field] = invalid
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "nonempty strings"):
                adapt_blocker(candidate)
        incomplete = dict(blocker)
        del incomplete["remediation"]
        with self.assertRaisesRegex(ValueError, "missing required fields"):
            adapt_blocker(incomplete)


if __name__ == "__main__":
    unittest.main()
