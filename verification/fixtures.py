from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .catalog import VerificationDefinitionRegistry
from .models import (
    DependencyRef,
    EvidenceRef,
    EvidenceType,
    FounderActionStatus,
    VerificationMethod,
    VerificationRecord,
    VerificationState,
)
from .ports import CompanySnapshot, FounderActionSnapshot
from .service import VerificationService


FIXTURE_NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
TENANT_ID = "tenant_billy_demo"
COMPANY_ID = "company_billy_bob_lawn"


def billy_snapshot(
    *,
    offer: bool = False,
    service_area: bool = False,
    admin_complete: bool = False,
    material_waivers: tuple[str, ...] = (),
) -> CompanySnapshot:
    status = FounderActionStatus.VERIFIED if admin_complete else FounderActionStatus.REQUIRED
    return CompanySnapshot(
        tenant_id=TENANT_ID,
        company_id=COMPANY_ID,
        version=3,
        approved_offer_ids=("offer_mow_edge_blow",) if offer else (),
        approved_service_area_ids=("market_denton_12_mile",) if service_area else (),
        owner_ids=("party_billy_bob",),
        founder_actions=(
            FounderActionSnapshot("founder_action_domain", FounderActionStatus.VERIFIED, critical=True),
            FounderActionSnapshot("founder_action_admin", status, critical=False),
        ),
        scheduling_required=True,
        selected_fully_set_requirements=("domain.ownership", "founder.action_complete"),
        material_waivers=material_waivers,
    )


def proposed_record(
    verification_id: str,
    definition_id: str,
    *,
    company_id: str = COMPANY_ID,
    tenant_id: str = TENANT_ID,
    owner: str | None = "party_billy_bob",
    method: VerificationMethod = VerificationMethod.AUTOMATED,
    dependencies: tuple[DependencyRef, ...] = (),
) -> VerificationRecord:
    return VerificationRecord(
        verification_id=verification_id,
        tenant_id=tenant_id,
        company_id=company_id,
        definition_id=definition_id,
        target_type="workflow" if definition_id.startswith("workflow.") else "asset",
        target_id=f"target_{verification_id}",
        state=VerificationState.PROPOSED,
        owner=owner,
        scope=f"Billy Bob fixture scope for {definition_id}",
        method=method,
        dependencies=dependencies,
        created_at=FIXTURE_NOW,
        updated_at=FIXTURE_NOW,
        provenance={"source_type": "system", "fixture": True, "criticality": "material"},
    )


def evidence(
    evidence_id: str,
    evidence_type: EvidenceType,
    *,
    company_id: str = COMPANY_ID,
    test_name: str | None = None,
    passed: bool | None = None,
    expires_at: datetime | None = None,
    issuer: str = "fixture-provider",
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        artifact_ref=f"artifact_{evidence_id}",
        company_id=company_id,
        captured_at=FIXTURE_NOW,
        expires_at=expires_at,
        test_name=test_name,
        test_passed=passed,
        issuer=issuer,
        provenance={"fixture": True},
    )


def verify_definition(
    service: VerificationService,
    registry: VerificationDefinitionRegistry,
    definition_id: str,
    sequence: int,
    *,
    dependencies: tuple[DependencyRef, ...] = (),
) -> VerificationRecord:
    definition = registry.get(definition_id)
    method = sorted(definition.allowed_methods, key=lambda item: item.value)[0]
    record = proposed_record(
        f"verification_{sequence:03d}",
        definition_id,
        method=method,
        dependencies=dependencies,
    )
    service.create(record)
    service.transition(TENANT_ID, COMPANY_ID, record.verification_id, VerificationState.EXECUTED, at=FIXTURE_NOW)
    evidence_items: list[EvidenceRef] = []
    defined_test = sorted(definition.defined_tests)[0]
    for offset, evidence_type in enumerate(sorted(definition.required_evidence_types, key=lambda item: item.value)):
        evidence_items.append(
            evidence(
                f"evidence_{sequence:03d}_{offset:02d}",
                evidence_type,
                test_name=defined_test if evidence_type is EvidenceType.TEST_RESULT else None,
                passed=True if evidence_type is EvidenceType.TEST_RESULT else None,
                expires_at=FIXTURE_NOW + timedelta(days=365),
            )
        )
    if EvidenceType.TEST_RESULT not in definition.required_evidence_types:
        evidence_items.append(
            evidence(
                f"evidence_{sequence:03d}_test",
                EvidenceType.TEST_RESULT,
                test_name=defined_test,
                passed=True,
                expires_at=FIXTURE_NOW + timedelta(days=365),
            )
        )
    service.transition(
        TENANT_ID,
        COMPANY_ID,
        record.verification_id,
        VerificationState.TESTED,
        evidence=tuple(evidence_items),
        at=FIXTURE_NOW,
    )
    return service.transition(
        TENANT_ID,
        COMPANY_ID,
        record.verification_id,
        VerificationState.VERIFIED,
        at=FIXTURE_NOW,
    )


READY_DEFINITIONS = (
    "website.forms",
    "email.inbound",
    "crm.lead_capture",
    "workflow.quote",
    "scheduling.booking",
    "workflow.rollback",
)

FULLY_SET_DEFINITIONS = (
    "domain.ownership",
    "founder.action_complete",
    "monitoring.active",
    "handoff.complete",
)


def populate_ready(service: VerificationService, registry: VerificationDefinitionRegistry) -> None:
    for sequence, definition_id in enumerate(READY_DEFINITIONS, 1):
        dependency = (
            DependencyRef("website_deployment", 1, "website_deployment"),
        ) if definition_id in {"website.forms", "crm.lead_capture"} else ()
        verify_definition(service, registry, definition_id, sequence, dependencies=dependency)


def populate_fully_set(service: VerificationService, registry: VerificationDefinitionRegistry) -> None:
    populate_ready(service, registry)
    for sequence, definition_id in enumerate(FULLY_SET_DEFINITIONS, 100):
        verify_definition(service, registry, definition_id, sequence)
