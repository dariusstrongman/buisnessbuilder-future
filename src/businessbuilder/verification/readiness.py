from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .models import Blocker, BlockerSeverity, FounderActionStatus, VerificationState, utc_now
from .ports import CompanySnapshot
from .repository import VerificationRepository


class RequirementKind(StrEnum):
    FACT = "fact"
    VERIFICATION = "verification"
    ANY_VERIFICATION = "any_verification"


@dataclass(frozen=True)
class Requirement:
    requirement_id: str
    label: str
    kind: RequirementKind
    values: tuple[str, ...]
    conditional_fact: str | None = None


@dataclass(frozen=True)
class ReadinessPolicy:
    policy_id: str
    ready_requirements: tuple[Requirement, ...]
    fully_set_base_requirements: tuple[Requirement, ...]
    allow_noncritical_blockers_for_ready: bool = True


@dataclass(frozen=True)
class EvaluationResult:
    ready: bool
    fully_set: bool
    unmet_ready: tuple[str, ...]
    unmet_fully_set: tuple[str, ...]
    blocking_ids: tuple[str, ...]
    evaluated_at: datetime


def billy_bob_policy() -> ReadinessPolicy:
    fact = RequirementKind.FACT
    verify = RequirementKind.VERIFICATION
    any_verify = RequirementKind.ANY_VERIFICATION
    return ReadinessPolicy(
        policy_id="mobile_service.v1",
        ready_requirements=(
            Requirement("approved_offer", "Approved offer exists", fact, ("approved_offer",)),
            Requirement("approved_service_area", "Approved service area exists", fact, ("approved_service_area",)),
            Requirement("website_deployed", "Website deployment is independently verified", verify, ("website.deployed",)),
            Requirement("website_https", "Website HTTPS is independently verified", verify, ("website.https",)),
            Requirement("website_links", "Website links are independently verified", verify, ("website.links",)),
            Requirement("website_mobile", "Website mobile behavior is independently verified", verify, ("website.mobile",)),
            Requirement("customer_contact", "Customer contact path is verified", any_verify, ("website.forms", "email.inbound")),
            Requirement("lead_intake", "Lead intake is tested and verified", verify, ("crm.lead_capture",)),
            Requirement("quote_path", "Quote path is tested and verified", verify, ("workflow.quote",)),
            Requirement("scheduling", "Scheduling path is tested and verified", verify, ("scheduling.booking",), "scheduling_required"),
            Requirement("support_path", "Support/contact path is verified", verify, ("email.inbound",)),
            Requirement("ownership", "Ownership is known", fact, ("ownership_known",)),
            Requirement("rollback", "Rollback/failure path is verified", verify, ("workflow.rollback",)),
        ),
        fully_set_base_requirements=(
            Requirement("monitoring", "Monitoring and named ownership are verified", verify, ("monitoring.active",)),
            Requirement("handoff", "Customer handoff is verified", verify, ("handoff.complete",)),
        ),
    )


class ReadinessEvaluator:
    def __init__(self, repository: VerificationRepository, policy: ReadinessPolicy) -> None:
        self.repository = repository
        self.policy = policy

    def evaluate(
        self,
        snapshot: CompanySnapshot,
        blockers: tuple[Blocker, ...] = (),
        *,
        at: datetime | None = None,
    ) -> EvaluationResult:
        now = at or utc_now()
        records = self.repository.list_for_company(snapshot.tenant_id, snapshot.company_id)
        verified = {
            record.definition_id
            for record in records
            if record.state is VerificationState.VERIFIED and record.is_current(now)
        }
        facts = set(snapshot.facts)
        if snapshot.approved_offer_ids:
            facts.add("approved_offer")
        if snapshot.approved_service_area_ids:
            facts.add("approved_service_area")
        if snapshot.owner_ids:
            facts.add("ownership_known")
        if snapshot.scheduling_required:
            facts.add("scheduling_required")

        unmet_ready = self._unmet(self.policy.ready_requirements, facts, verified)
        incomplete_critical_actions = tuple(
            item.action_id
            for item in snapshot.founder_actions
            if item.selected and item.critical and item.status is not FounderActionStatus.VERIFIED
        )
        open_blockers = tuple(
            item for item in blockers
            if item.open and item.tenant_id == snapshot.tenant_id and item.company_id == snapshot.company_id
        )
        critical_blockers = tuple(item.blocker_id for item in open_blockers if item.severity is BlockerSeverity.CRITICAL)
        noncritical_blockers = tuple(item.blocker_id for item in open_blockers if item.severity is BlockerSeverity.NONCRITICAL)
        policy_blockers = () if self.policy.allow_noncritical_blockers_for_ready else noncritical_blockers
        ready = not (unmet_ready or incomplete_critical_actions or critical_blockers or policy_blockers)

        unmet_fully = list(self._unmet(self.policy.fully_set_base_requirements, facts, verified))
        unmet_fully.extend(
            definition_id
            for definition_id in snapshot.selected_fully_set_requirements
            if definition_id not in verified
        )
        unmet_fully.extend(
            item.action_id
            for item in snapshot.founder_actions
            if item.selected and item.status is not FounderActionStatus.VERIFIED
        )
        if snapshot.material_waivers:
            unmet_fully.extend(f"material-waiver:{item}" for item in snapshot.material_waivers)
        if snapshot.critical_unresolved_obligations:
            unmet_fully.extend(f"critical-obligation:{item}" for item in snapshot.critical_unresolved_obligations)
        fully_set = ready and not unmet_fully and not critical_blockers
        return EvaluationResult(
            ready=ready,
            fully_set=fully_set,
            unmet_ready=tuple(dict.fromkeys((*unmet_ready, *incomplete_critical_actions))),
            unmet_fully_set=tuple(dict.fromkeys(unmet_fully)),
            blocking_ids=tuple(dict.fromkeys((*critical_blockers, *policy_blockers))),
            evaluated_at=now,
        )

    @staticmethod
    def _unmet(
        requirements: tuple[Requirement, ...], facts: set[str], verified: set[str]
    ) -> tuple[str, ...]:
        missing: list[str] = []
        for requirement in requirements:
            if requirement.conditional_fact and requirement.conditional_fact not in facts:
                continue
            if requirement.kind is RequirementKind.FACT:
                satisfied = all(value in facts for value in requirement.values)
            elif requirement.kind is RequirementKind.VERIFICATION:
                satisfied = all(value in verified for value in requirement.values)
            else:
                satisfied = any(value in verified for value in requirement.values)
            if not satisfied:
                missing.append(requirement.requirement_id)
        return tuple(missing)
