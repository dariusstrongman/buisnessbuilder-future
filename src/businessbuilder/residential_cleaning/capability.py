from __future__ import annotations

from hashlib import sha256
import json

from businessbuilder.company_brain import (
    CompanyBrainService,
    EntityRef,
    KnowledgeClass,
    LifecycleState,
    Provenance,
    RecordKind,
    Scope,
)
from businessbuilder.company_brain.errors import NotFoundError
from businessbuilder.runtime import ArtifactRef, Capability, CapabilityRequest, CapabilityResult, Money

from .founder_actions import FOUNDER_ACTION_DEFINITIONS, prepared_action_data


def canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + sha256(encoded).hexdigest()


class ResidentialCleaningVerificationRouter:
    """Prevents a scope decision from masquerading as launch verification."""

    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def request_verification(self, **request) -> None:
        artifacts = request.get("artifact_refs", ())
        if any(
            (
                item.get("type") == "company_brain_record"
                and item.get("id") == "decision_cleaning_build_scope"
            )
            or item.get("type") in {
                "pilot_founder_action_state",
                "pilot_founder_action_verification",
            }
            for item in artifacts
        ):
            return
        self.delegate.request_verification(**request)


class ResidentialCleaningScopeCommitCapability(Capability):
    """Runtime-owned, zero-spend commit of one founder-approved pilot scope."""

    name = "pilot.residential_cleaning.commit_scope"
    version = "v1"

    def __init__(self, company_brain: CompanyBrainService) -> None:
        self.company_brain = company_brain

    def validate_request(self, request: CapabilityRequest) -> None:
        if request.capability != self.name or request.capability_version != self.version:
            raise ValueError("scope-commit capability mismatch")
        if request.inputs.get("vertical") != "residential_cleaning":
            raise PermissionError("only the residential-cleaning pilot is supported")
        for key in ("recommendation_id", "recommendation_digest", "research_record_id"):
            if not isinstance(request.inputs.get(key), str) or not request.inputs[key]:
                raise ValueError(f"{key} is required")

    def estimate(self, request: CapabilityRequest) -> Money:
        self.validate_request(request)
        return Money("USD", 0)

    def execute(self, request: CapabilityRequest) -> CapabilityResult:
        self.validate_request(request)
        scope = Scope(request.tenant_id, request.company_id)
        recommendation = self.company_brain.repository.get_record(
            scope, request.inputs["recommendation_id"]
        )
        if recommendation.kind is not RecordKind.STRATEGY:
            raise PermissionError("recommendation record has the wrong kind")
        if canonical_digest(dict(recommendation.data)) != request.inputs["recommendation_digest"]:
            raise PermissionError("recommendation changed after approval was requested")
        research = self.company_brain.repository.get_record(
            scope, request.inputs["research_record_id"]
        )
        if research.kind is not RecordKind.MARKET:
            raise PermissionError("research packet has the wrong kind")

        owner = self.company_brain.get_company(scope).owner_refs[0]
        provenance = (
            Provenance(
                "runtime_approved_capability",
                self.company_brain.get_company(scope).updated_at,
                EntityRef("runtime_job", request.job_id),
                source_ref=f"runtime://job/{request.job_id}",
                content_digest=request.inputs["recommendation_digest"],
            ),
        )
        approved = dict(recommendation.data)
        approved["state"] = "approved"
        approved["approved_by_runtime_job"] = request.job_id
        self._ensure_record(
            scope,
            "decision_cleaning_build_scope",
            RecordKind.DECISION,
            {
                "choice": "approved",
                "recommendation_id": recommendation.record_id,
                "recommendation_digest": request.inputs["recommendation_digest"],
                "research_record_id": research.record_id,
                "approved_offer_ids": [
                    f"offer_{item['offer_id']}" for item in approved["offers"]
                ],
                "approved_service_area_ids": ["market_cleaning_service_area"],
            },
            KnowledgeClass.FOUNDER_DECISION,
            provenance,
            owner,
        )
        self._ensure_record(
            scope, "strategy_cleaning_approved", RecordKind.STRATEGY, approved,
            KnowledgeClass.FOUNDER_DECISION, provenance, owner,
        )
        service_area = approved["service_area"]
        self._ensure_record(
            scope, "market_cleaning_service_area", RecordKind.MARKET,
            {**service_area, "approved": True}, KnowledgeClass.FOUNDER_DECISION,
            provenance, owner,
        )
        for offer in approved["offers"]:
            self._ensure_record(
                scope, f"offer_{offer['offer_id']}", RecordKind.OFFER,
                {**offer, "approved": True}, KnowledgeClass.FOUNDER_DECISION,
                provenance, owner,
            )
        self._ensure_record(
            scope, "policy_cleaning_price_logic", RecordKind.POLICY,
            {**approved["starting_price_logic"], "approved": True},
            KnowledgeClass.FOUNDER_DECISION, provenance, owner,
        )
        for action in FOUNDER_ACTION_DEFINITIONS:
            self._ensure_record(
                scope,
                action.action_id,
                RecordKind.FOUNDER_ACTION,
                prepared_action_data(
                    action,
                    company_id=scope.company_id,
                    prepared_at=self.company_brain.get_company(scope).updated_at,
                    actor_id=request.job_id,
                ),
                KnowledgeClass.FACT,
                provenance,
                owner,
            )
        company = self.company_brain.get_company(scope)
        if company.lifecycle is LifecycleState.CHALLENGED:
            company = self.company_brain.transition_company(
                scope, LifecycleState.APPROVED, expected_version=company.version
            )
        if company.lifecycle is LifecycleState.APPROVED:
            self.company_brain.transition_company(
                scope, LifecycleState.ASSEMBLY, expected_version=company.version
            )
        elif company.lifecycle is not LifecycleState.ASSEMBLY:
            raise PermissionError("company is not in an approvable pilot lifecycle")

        provider_ref = f"company-brain:{request.company_id}:cleaning-scope-v1"
        return CapabilityResult(
            provider_ref=provider_ref,
            status="completed",
            artifacts=(ArtifactRef("company_brain_record", "decision_cleaning_build_scope"),),
            spend=Money("USD", 0),
            progress=("recommendation_validated", "scope_committed"),
        )

    def status(self, provider_ref: str) -> str:
        return "completed" if provider_ref.startswith("company-brain:") else "unknown"

    def cancel(self, provider_ref: str) -> bool:
        return False

    def collect_result(self, provider_ref: str) -> CapabilityResult | None:
        return None

    def _ensure_record(
        self,
        scope: Scope,
        record_id: str,
        kind: RecordKind,
        data: dict,
        knowledge_class: KnowledgeClass,
        provenance: tuple[Provenance, ...],
        owner: EntityRef,
    ) -> None:
        try:
            existing = self.company_brain.repository.get_record(scope, record_id)
        except NotFoundError:
            self.company_brain.update_approved_state(
                scope,
                record_id=record_id,
                kind=kind,
                data=data,
                knowledge_class=knowledge_class,
                provenance=provenance,
                confidence=None,
                owner_ref=owner,
            )
            return
        if existing.kind is not kind or dict(existing.data) != data or existing.knowledge_class is not knowledge_class:
            raise PermissionError("existing Company Brain scope record conflicts with approved recommendation")
