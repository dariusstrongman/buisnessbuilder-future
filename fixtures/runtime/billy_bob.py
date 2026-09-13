from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from businessbuilder_runtime.capabilities import CapabilityRegistry
from businessbuilder_runtime.fakes import FakeCrmCapability, FakeEmailCapability, FakeWebsiteCapability
from businessbuilder_runtime.ids import DeterministicIds
from businessbuilder_runtime.models import ApprovalMode, Budget, Event, JobStatus, Money
from businessbuilder_runtime.orchestrator import JobOrchestrator
from businessbuilder_runtime.ports import FakeCompanyStateReader, RecordingVerificationPort
from businessbuilder_runtime.storage import SQLiteRuntimeRepository


FIXED_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def website_request() -> dict[str, Any]:
    return {
        "schema_version": "website.capability.request.v1",
        "request_id": "request_bb_website_001",
        "company_id": "company_billy_bob",
        "job_id": "job_bb_website_001",
        "correlation_id": "correlation_billy_launch",
        "idempotency_key": "billy-bob-website-0001",
        "brief_artifact_ref": {"type": "artifact", "id": "artifact_bb_brief", "version": 1},
        "brief_digest": "sha256:" + "a" * 64,
        "approved_claims": ["Mow, edge, and blow service within 12 miles"],
        "prohibited_claims": ["Chemical treatment", "Irrigation", "Tree work", "Hardscape"],
        "asset_refs": [],
        "requirements": {
            "mode": "new",
            "required_routes": ["/", "/services", "/contact"],
            "responsive": True,
            "accessible": True,
            "delivery": "static_package",
        },
        "budget_ref": {"type": "budget", "id": "budget_bb_company", "version": 1},
        "approval_policy": {
            "direction": "founder_required",
            "delivery": "founder_required",
            "publish": "outside_capability",
        },
    }


def run_billy_bob() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    repository = SQLiteRuntimeRepository()
    ids = DeterministicIds()
    companies = FakeCompanyStateReader({("tenant_billy", "company_billy_bob")})
    verification = RecordingVerificationPort()
    registry = CapabilityRegistry()
    website = FakeWebsiteCapability(root / "contracts")
    registry.register(website)
    registry.register(FakeCrmCapability())
    registry.register(FakeEmailCapability())
    runtime = JobOrchestrator(
        repository=repository,
        registry=registry,
        company_reader=companies,
        verification=verification,
        id_factory=ids,
        clock=lambda: FIXED_NOW,
    )
    runtime.events.publish(
        Event(
            event_id="event_company_created",
            tenant_id="tenant_billy",
            company_id="company_billy_bob",
            correlation_id="correlation_billy_launch",
            causation_id=None,
            type="company.created",
            occurred_at=FIXED_NOW,
            payload={"name": "Billy Bob Lawn Care", "archetype": "mobile_service"},
            source="company-brain.fake",
        )
    )
    runtime.budgets.create(
        Budget(
            budget_id="budget_bb_company",
            tenant_id="tenant_billy",
            company_id="company_billy_bob",
            ceiling=Money("USD", 1000),
        ),
        "correlation_billy_launch",
    )
    job = runtime.create_job(
        tenant_id="tenant_billy",
        company_id="company_billy_bob",
        capability="fake.website.build",
        inputs={"objective": "Build Billy Bob's approved lawn-care website", "website_request": website_request()},
        budget_ref="budget_bb_company",
        per_job_ceiling=Money("USD", 400),
        idempotency_key="billy-bob-website-0001",
        correlation_id="correlation_billy_launch",
        approval_mode=ApprovalMode.FOUNDER_ONLY,
        job_id="job_bb_website_001",
    )
    assert job.status == JobStatus.WAITING_APPROVAL
    job = runtime.approve_job(
        tenant_id=job.tenant_id,
        company_id=job.company_id,
        job_id=job.job_id,
        approval_id=job.approval_ids[0],
        actor_id="founder_billy_bob",
        actor_role="founder",
    )
    job = runtime.run(job.tenant_id, job.company_id, job.job_id)
    assert job.status == JobStatus.SUCCEEDED
    return {
        "job": job,
        "events": repository.list_events(job.tenant_id, job.company_id),
        "audit": repository.list_audit(job.tenant_id, job.company_id),
        "verification_requests": verification.requests,
        "website_execute_count": website.execute_count,
        "repository": repository,
        "runtime": runtime,
    }


if __name__ == "__main__":
    proof = run_billy_bob()
    print(proof["job"].status.value)
    print([event["type"] for event in proof["events"]])
