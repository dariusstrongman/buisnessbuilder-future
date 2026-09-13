from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from businessbuilder.company_brain import (
    CompanyBrainService,
    EntityRef,
    KnowledgeClass,
    Provenance,
    RecordKind,
    SQLiteCompanyBrainRepository,
    load_billy_bob,
)
from businessbuilder.integration.adapters import (
    ADMIN_DEFINITIONS,
    CUSTOMER_PATH_DEFINITIONS,
    CompanyBrainRuntimeAdapter,
    CompanyBrainVerificationAdapter,
    RuntimeVerificationAdapter,
    VerificationInvalidationAdapter,
)
from businessbuilder.runtime.capabilities import CapabilityRegistry
from businessbuilder.runtime.fakes import FakeWebsiteCapability
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.runtime.models import ApprovalMode, Budget, Event, JobStatus, Money
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.runtime.storage import SQLiteRuntimeRepository
from businessbuilder.verification import (
    EvidenceRef,
    EvidenceType,
    ReadinessEvaluator,
    VerificationEventHandler,
    VerificationService,
    VerificationState,
    billy_bob_policy,
    default_registry,
)
from businessbuilder.verification.repository import InMemoryVerificationRepository


FIXED_NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
TENANT_ID = "tenant_billy"
COMPANY_ID = "co_billy_bob_lawn"
JOB_ID = "job_bb_website_001"
CORRELATION_ID = "correlation_billy_launch"


def website_request() -> dict[str, Any]:
    return {
        "schema_version": "website.capability.request.v1",
        "request_id": "request_bb_website_001",
        "company_id": COMPANY_ID,
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
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


def _verify_definition(
    service: VerificationService,
    adapter: RuntimeVerificationAdapter,
    definition_id: str,
    artifact_id: str,
) -> None:
    verification_id = adapter.verification_id(JOB_ID, definition_id)
    definition = service.registry.get(definition_id)
    service.transition(TENANT_ID, COMPANY_ID, verification_id, VerificationState.EXECUTED, at=FIXED_NOW)
    evidence: list[EvidenceRef] = []
    defined_test = sorted(definition.defined_tests)[0]
    for index, evidence_type in enumerate(sorted(definition.required_evidence_types, key=lambda item: item.value)):
        evidence.append(
            EvidenceRef(
                evidence_id=f"evidence_{verification_id[-8:]}_{index}",
                evidence_type=evidence_type,
                artifact_ref=artifact_id,
                company_id=COMPANY_ID,
                captured_at=FIXED_NOW,
                expires_at=FIXED_NOW + timedelta(days=365),
                test_name=defined_test if evidence_type is EvidenceType.TEST_RESULT else None,
                test_passed=True if evidence_type is EvidenceType.TEST_RESULT else None,
                issuer="fixture-provider",
                provenance={"source_type": "deterministic_offline_fixture"},
            )
        )
    if EvidenceType.TEST_RESULT not in definition.required_evidence_types:
        evidence.append(
            EvidenceRef(
                evidence_id=f"evidence_{verification_id[-8:]}_test",
                evidence_type=EvidenceType.TEST_RESULT,
                artifact_ref=artifact_id,
                company_id=COMPANY_ID,
                captured_at=FIXED_NOW,
                expires_at=FIXED_NOW + timedelta(days=365),
                test_name=defined_test,
                test_passed=True,
                issuer="fixture-provider",
                provenance={"source_type": "deterministic_offline_fixture"},
            )
        )
    service.transition(
        TENANT_ID,
        COMPANY_ID,
        verification_id,
        VerificationState.TESTED,
        evidence=tuple(evidence),
        at=FIXED_NOW,
    )
    service.transition(TENANT_ID, COMPANY_ID, verification_id, VerificationState.VERIFIED, at=FIXED_NOW)


def run_billy_bob() -> dict[str, Any]:
    """Run the canonical, entirely offline integration proof using all real subsystems."""
    root = Path(__file__).resolve().parents[3]
    brain_repository = SQLiteCompanyBrainRepository()
    brain_repository.migrate()
    brain = CompanyBrainService(brain_repository)
    scope = load_billy_bob(brain, TENANT_ID)
    founder = EntityRef("party", "party_billy")
    provenance = Provenance(
        "system",
        FIXED_NOW.isoformat().replace("+00:00", "Z"),
        EntityRef("agent", "integration_worker"),
        source_ref="fixture://offline-billy-bob-v1",
    )

    verification_repository = InMemoryVerificationRepository()
    registry = default_registry()
    verification = VerificationService(verification_repository, registry)
    readiness = ReadinessEvaluator(verification_repository, billy_bob_policy())
    brain_runtime = CompanyBrainRuntimeAdapter(brain)
    brain_verification = CompanyBrainVerificationAdapter(brain)
    verification_port = RuntimeVerificationAdapter(
        verification, brain_verification, brain, clock=lambda: FIXED_NOW
    )

    runtime_repository = SQLiteRuntimeRepository()
    ids = DeterministicIds()
    capability_registry = CapabilityRegistry()
    website = FakeWebsiteCapability(root / "contracts")
    capability_registry.register(website)
    runtime = JobOrchestrator(
        repository=runtime_repository,
        registry=capability_registry,
        company_reader=brain_runtime,
        verification=verification_port,
        id_factory=ids,
        clock=lambda: FIXED_NOW,
    )
    runtime.events.publish(
        Event(
            event_id="event_company_created",
            tenant_id=TENANT_ID,
            company_id=COMPANY_ID,
            correlation_id=CORRELATION_ID,
            causation_id=None,
            type="company.created",
            occurred_at=FIXED_NOW,
            payload={"name": "Billy Bob Lawn Care", "company_version": 1},
            source="businessbuilder.company_brain",
        )
    )
    runtime.budgets.create(
        Budget("budget_bb_company", TENANT_ID, COMPANY_ID, Money("USD", 1000)),
        CORRELATION_ID,
    )
    initial = readiness.evaluate(brain_verification.get_snapshot(TENANT_ID, COMPANY_ID), at=FIXED_NOW)
    job = runtime.create_job(
        tenant_id=TENANT_ID,
        company_id=COMPANY_ID,
        capability="fake.website.build",
        inputs={"objective": "Build Billy Bob's approved lawn-care website", "website_request": website_request()},
        budget_ref="budget_bb_company",
        per_job_ceiling=Money("USD", 400),
        idempotency_key="billy-bob-website-0001",
        correlation_id=CORRELATION_ID,
        approval_mode=ApprovalMode.FOUNDER_ONLY,
        job_id=JOB_ID,
    )
    assert job.status is JobStatus.WAITING_APPROVAL
    job = runtime.approve_job(
        tenant_id=TENANT_ID,
        company_id=COMPANY_ID,
        job_id=JOB_ID,
        approval_id=job.approval_ids[0],
        actor_id=founder.id,
        actor_role="founder",
    )
    brain.append_approval(
        scope,
        approval_id="approval_website_direction",
        approval_ref=EntityRef("approval", job.approval_ids[0], 2),
        provenance=(provenance,),
        owner_ref=founder,
    )
    job = runtime.run(TENANT_ID, COMPANY_ID, JOB_ID)
    assert job.status is JobStatus.SUCCEEDED
    artifact = job.artifacts[0]
    brain.attach_artifact(
        scope,
        record_id="artifact_website_package",
        artifact_ref=EntityRef(artifact.type, artifact.id, artifact.version),
        provenance=(provenance,),
        owner_ref=founder,
    )

    proposed = tuple(
        verification.get(TENANT_ID, COMPANY_ID, verification_port.verification_id(JOB_ID, definition_id))
        for definition_id in (*CUSTOMER_PATH_DEFINITIONS, *ADMIN_DEFINITIONS)
    )
    for definition_id in CUSTOMER_PATH_DEFINITIONS:
        _verify_definition(verification, verification_port, definition_id, artifact.id)
    ready_only = readiness.evaluate(brain_verification.get_snapshot(TENANT_ID, COMPANY_ID), at=FIXED_NOW)

    action = next(
        item
        for item in brain.query_current_state(scope, kinds=(RecordKind.FOUNDER_ACTION,))
        if item.record_id == "founder_buy_domain"
    )
    brain.update_approved_state(
        scope,
        record_id=action.record_id,
        kind=RecordKind.FOUNDER_ACTION,
        data={**dict(action.data), "state": "verified"},
        knowledge_class=KnowledgeClass.FACT,
        provenance=(provenance,),
        confidence=None,
        owner_ref=founder,
        expected_version=action.version,
    )
    for definition_id in ADMIN_DEFINITIONS:
        _verify_definition(verification, verification_port, definition_id, artifact.id)
    fully_set = readiness.evaluate(brain_verification.get_snapshot(TENANT_ID, COMPANY_ID), at=FIXED_NOW)

    service_area = next(
        item
        for item in brain.query_current_state(scope, kinds=(RecordKind.MARKET,))
        if item.record_id == "market_denton_12mi"
    )
    _, notices = brain.update_approved_state(
        scope,
        record_id=service_area.record_id,
        kind=RecordKind.MARKET,
        data={**dict(service_area.data), "radius_miles": 10},
        knowledge_class=KnowledgeClass.FOUNDER_DECISION,
        provenance=(provenance,),
        confidence=None,
        owner_ref=founder,
        expected_version=service_area.version,
        invalidation_reason="approved service area changed from 12 miles to 10 miles",
    )
    invalidations = VerificationInvalidationAdapter(VerificationEventHandler(verification)).handle(notices)
    after_invalidation = readiness.evaluate(brain_verification.get_snapshot(TENANT_ID, COMPANY_ID), at=FIXED_NOW)
    budget = runtime_repository.get_budget(TENANT_ID, COMPANY_ID, "budget_bb_company")
    return {
        "scope": scope,
        "job": job,
        "budget": budget,
        "events": runtime_repository.list_events(TENANT_ID, COMPANY_ID),
        "audit": runtime_repository.list_audit(TENANT_ID, COMPANY_ID),
        "proposed_verifications": proposed,
        "verification_records": verification_repository.list_for_company(TENANT_ID, COMPANY_ID),
        "verification_request_ids": tuple(verification_port.requested_ids),
        "verification_requests": verification_port.requests,
        "initial": initial,
        "ready_only": ready_only,
        "fully_set": fully_set,
        "invalidations": invalidations,
        "after_invalidation": after_invalidation,
        "website_execute_count": website.execute_count,
        "brain": brain,
        "brain_repository": brain_repository,
        "runtime": runtime,
        "runtime_repository": runtime_repository,
        "repository": runtime_repository,
        "verification": verification,
        "verification_port": verification_port,
        "brain_runtime_adapter": brain_runtime,
        "brain_verification_adapter": brain_verification,
    }
