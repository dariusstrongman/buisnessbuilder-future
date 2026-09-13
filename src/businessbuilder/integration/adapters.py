from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from hashlib import sha256
from typing import Any

from businessbuilder.company_brain import (
    CompanyBrainService,
    EntityRef,
    InvalidationNotice,
    KnowledgeClass,
    NotFoundError,
    RecordKind,
    Scope,
)
from businessbuilder.verification import (
    CompanySnapshot,
    DependencyRef,
    FounderActionSnapshot,
    FounderActionStatus,
    VerificationEventHandler,
    VerificationRecord,
    VerificationService,
    VerificationState,
)
from businessbuilder.verification.events import CanonicalEvent


CUSTOMER_PATH_DEFINITIONS = (
    "website.deployed",
    "website.https",
    "website.forms",
    "website.mobile",
    "website.links",
    "email.inbound",
    "crm.lead_capture",
    "workflow.quote",
    "scheduling.booking",
    "workflow.rollback",
)
ADMIN_DEFINITIONS = (
    "domain.ownership",
    "founder.action_complete",
    "monitoring.active",
    "handoff.complete",
)
GEOGRAPHY_BOUND_DEFINITIONS = frozenset(
    {"website.forms", "crm.lead_capture", "workflow.quote", "scheduling.booking"}
)


class CompanyBrainRuntimeAdapter:
    """Implements Runtime's read ports strictly through CompanyBrainService."""

    def __init__(self, service: CompanyBrainService) -> None:
        self.service = service

    def company_exists(self, tenant_id: str, company_id: str) -> bool:
        try:
            self.service.get_company(Scope(tenant_id, company_id))
        except NotFoundError:
            return False
        return True

    def get_snapshot(self, tenant_id: str, company_id: str) -> dict[str, Any]:
        return self.service.compact_snapshot(Scope(tenant_id, company_id))


class CompanyBrainVerificationAdapter:
    """Explicit readiness projection, including records omitted from compact snapshots."""

    def __init__(self, service: CompanyBrainService) -> None:
        self.service = service

    def get_snapshot(self, tenant_id: str, company_id: str) -> CompanySnapshot:
        scope = Scope(tenant_id, company_id)
        company = self.service.get_company(scope)
        records = self.service.query_current_state(scope)

        active_offers = {
            item.record_id for item in records if item.kind is RecordKind.OFFER and item.lifecycle == "active"
        }
        active_service_areas = {
            item.record_id for item in records if item.kind is RecordKind.MARKET and item.lifecycle == "active"
        }
        decisions = tuple(
            item
            for item in records
            if item.kind is RecordKind.DECISION
            and item.knowledge_class is KnowledgeClass.FOUNDER_DECISION
            and item.lifecycle == "active"
            and item.data.get("choice") == "approved"
        )
        approved_offer_refs = {
            str(record_id)
            for decision in decisions
            for record_id in decision.data.get("approved_offer_ids", ())
        }
        approved_service_area_refs = {
            str(record_id)
            for decision in decisions
            for record_id in decision.data.get("approved_service_area_ids", ())
        }
        offers = tuple(sorted(active_offers & approved_offer_refs))
        service_areas = tuple(sorted(active_service_areas & approved_service_area_refs))
        accounts = tuple(item for item in records if item.kind is RecordKind.ACCOUNT)
        actions = tuple(item for item in records if item.kind is RecordKind.FOUNDER_ACTION)
        obligations = tuple(item for item in records if item.kind is RecordKind.OBLIGATION)
        policies = tuple(item for item in records if item.kind is RecordKind.POLICY)

        founder_actions: list[FounderActionSnapshot] = []
        material_waivers: list[str] = []
        for item in actions:
            raw_state = str(item.data.get("state", FounderActionStatus.REQUIRED.value))
            try:
                status = FounderActionStatus(raw_state)
            except ValueError:
                status = FounderActionStatus.REQUIRED
            selected = bool(item.data.get("selected", True))
            critical = bool(item.data.get("critical", False))
            founder_actions.append(FounderActionSnapshot(item.record_id, status, critical, selected))
            if status is FounderActionStatus.WAIVED_NONCRITICAL:
                material_waivers.append(item.record_id)

        selected_requirements: list[str] = []
        if any(item.data.get("type") == "domain_registrar" for item in accounts):
            selected_requirements.append("domain.ownership")
        if founder_actions:
            selected_requirements.append("founder.action_complete")

        critical_obligations = tuple(
            item.record_id
            for item in obligations
            if item.data.get("critical") is True and item.data.get("status") not in {"resolved", "verified"}
        )
        facts = {f"policy:{item.record_id}" for item in policies}
        facts.update(
            str(item.data["requirement_id"])
            for item in policies
            if isinstance(item.data.get("requirement_id"), str)
        )
        projection_version = company.version + sum(item.version for item in records)
        return CompanySnapshot(
            tenant_id=tenant_id,
            company_id=company_id,
            version=projection_version,
            approved_offer_ids=offers,
            approved_service_area_ids=service_areas,
            owner_ids=tuple(item.id for item in company.owner_refs),
            founder_actions=tuple(founder_actions),
            scheduling_required=company.archetype in {"mobile_service", "appointment_business"},
            facts=frozenset(facts),
            selected_fully_set_requirements=tuple(selected_requirements),
            material_waivers=tuple(material_waivers),
            critical_unresolved_obligations=critical_obligations,
        )

    def dependency(self, tenant_id: str, company_id: str, record_id: str) -> DependencyRef:
        scope = Scope(tenant_id, company_id)
        matches = [item for item in self.service.query_current_state(scope) if item.record_id == record_id]
        if len(matches) != 1:
            raise KeyError(f"Company Brain dependency not found: {record_id}")
        record = matches[0]
        return DependencyRef(record.record_id, record.version, record.kind.value)


class RuntimeVerificationAdapter:
    """Idempotently translates a completed runtime artifact into real verification proposals."""

    def __init__(
        self,
        verification: VerificationService,
        company_snapshots: CompanyBrainVerificationAdapter,
        company_brain: CompanyBrainService,
        *,
        dependency_id: str = "market_denton_12mi",
        definition_ids: Iterable[str] = (*CUSTOMER_PATH_DEFINITIONS, *ADMIN_DEFINITIONS),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.verification = verification
        self.company_snapshots = company_snapshots
        self.company_brain = company_brain
        self.dependency_id = dependency_id
        self.definition_ids = tuple(definition_ids)
        self.clock = clock
        self.requested_ids: list[str] = []
        self.requests: list[dict[str, Any]] = []

    @staticmethod
    def verification_id(job_id: str, definition_id: str) -> str:
        suffix = sha256(f"{job_id}:{definition_id}".encode()).hexdigest()[:16]
        return f"verification_{suffix}"

    def request_verification(
        self,
        *,
        tenant_id: str,
        company_id: str,
        job_id: str,
        artifact_refs: list[dict[str, Any]],
        correlation_id: str,
    ) -> None:
        if not self.definition_ids:
            return
        snapshot = self.company_snapshots.get_snapshot(tenant_id, company_id)
        dependency = self.company_snapshots.dependency(tenant_id, company_id, self.dependency_id)
        target_id = artifact_refs[0]["id"] if artifact_refs else job_id
        scope = Scope(tenant_id, company_id)
        created_any = False
        for definition_id in self.definition_ids:
            verification_id = self.verification_id(job_id, definition_id)
            try:
                self.verification.get(tenant_id, company_id, verification_id)
            except KeyError:
                definition = self.verification.registry.get(definition_id)
                method = sorted(definition.allowed_methods, key=lambda item: item.value)[0]
                now = self.clock() if self.clock else None
                record = VerificationRecord(
                    verification_id=verification_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    definition_id=definition_id,
                    target_type="artifact",
                    target_id=target_id,
                    state=VerificationState.PROPOSED,
                    owner=snapshot.owner_ids[0] if snapshot.owner_ids else None,
                    scope=f"Offline Billy Bob proof for {definition_id}",
                    method=method,
                    dependencies=(dependency,) if definition_id in GEOGRAPHY_BOUND_DEFINITIONS else (),
                    provenance={
                        "source_type": "runtime",
                        "job_id": job_id,
                        "correlation_id": correlation_id,
                    },
                    **({"created_at": now, "updated_at": now} if now else {}),
                )
                self.verification.create(record)
                self.requested_ids.append(verification_id)
                created_any = True
            # Dependency registration is a distinct idempotent side effect. Always
            # reconcile it, including when a prior attempt created Verification but
            # failed before Company Brain recorded the dependency.
            if definition_id in GEOGRAPHY_BOUND_DEFINITIONS:
                self.company_brain.add_dependency(
                    scope,
                    source_record_id=dependency.dependency_id,
                    dependent_ref=EntityRef("verification", verification_id),
                    trigger="service_area.changed",
                )
        if created_any:
            self.requests.append(
                {
                    "tenant_id": tenant_id,
                    "company_id": company_id,
                    "job_id": job_id,
                    "artifact_refs": artifact_refs,
                    "correlation_id": correlation_id,
                }
            )


class VerificationInvalidationAdapter:
    """Converts Company Brain notices to Verification's canonical event boundary."""

    def __init__(self, handler: VerificationEventHandler) -> None:
        self.handler = handler

    def handle(self, notices: Iterable[InvalidationNotice]) -> tuple[VerificationRecord, ...]:
        changed: list[VerificationRecord] = []
        for notice in notices:
            occurred_at = datetime.fromisoformat(notice.occurred_at.replace("Z", "+00:00"))
            changed.extend(
                self.handler.handle(
                    CanonicalEvent(
                        event_id=notice.notice_id,
                        tenant_id=notice.scope.tenant_id,
                        company_id=notice.scope.company_id,
                        event_type=notice.trigger,
                        occurred_at=occurred_at,
                        payload={
                            "dependency_id": notice.source_record_id,
                            "new_version": notice.source_version,
                        },
                    )
                )
            )
        return tuple(changed)
