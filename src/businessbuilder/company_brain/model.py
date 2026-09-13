from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from .errors import InvalidTransitionError, ValidationError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class LifecycleState(StrEnum):
    DRAFT = "draft"
    CHALLENGED = "challenged"
    APPROVED = "approved"
    ASSEMBLY = "assembly"
    READY = "ready"
    FULLY_SET = "fully_set"
    OPERATING = "operating"
    PAUSED = "paused"
    ARCHIVED = "archived"


LEGAL_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.DRAFT: frozenset({LifecycleState.CHALLENGED}),
    LifecycleState.CHALLENGED: frozenset({LifecycleState.DRAFT, LifecycleState.APPROVED}),
    LifecycleState.APPROVED: frozenset({LifecycleState.CHALLENGED, LifecycleState.ASSEMBLY}),
    LifecycleState.ASSEMBLY: frozenset({LifecycleState.READY, LifecycleState.PAUSED}),
    LifecycleState.READY: frozenset({LifecycleState.FULLY_SET, LifecycleState.OPERATING, LifecycleState.PAUSED}),
    LifecycleState.FULLY_SET: frozenset({LifecycleState.OPERATING, LifecycleState.PAUSED}),
    LifecycleState.OPERATING: frozenset({LifecycleState.PAUSED, LifecycleState.ARCHIVED}),
    LifecycleState.PAUSED: frozenset({LifecycleState.OPERATING, LifecycleState.ARCHIVED}),
    LifecycleState.ARCHIVED: frozenset(),
}


class RecordKind(StrEnum):
    PARTY = "party"
    GOAL = "goal"
    STRATEGY = "strategy"
    DECISION = "decision"
    OFFER = "offer"
    SERVICE = "service"
    MARKET = "market"
    POLICY = "policy"
    CAPACITY = "capacity"
    ASSET = "asset"
    ACCOUNT = "account"
    VENDOR = "vendor"
    CUSTOMER_REF = "customer_ref"
    LEAD_REF = "lead_ref"
    WORKFLOW = "workflow"
    AGENT = "agent"
    APPROVAL_REF = "approval_ref"
    VERIFICATION_REF = "verification_ref"
    EVIDENCE_REF = "evidence_ref"
    RISK = "risk"
    OBLIGATION = "obligation"
    METRIC = "metric"
    FOUNDER_ACTION = "founder_action"
    AUDIT_EVENT = "audit_event"
    ARTIFACT_REF = "artifact_ref"


class KnowledgeClass(StrEnum):
    FACT = "fact"
    ESTIMATE = "estimate"
    INFERENCE = "inference"
    FOUNDER_DECISION = "founder_decision"
    EXTERNAL_VERIFICATION = "external_verification"


MATERIAL_INVALIDATION_KINDS = frozenset({
    RecordKind.OFFER,
    RecordKind.SERVICE,
    RecordKind.MARKET,
    RecordKind.POLICY,
    RecordKind.ACCOUNT,
    RecordKind.WORKFLOW,
    RecordKind.PARTY,
})


@dataclass(frozen=True, slots=True)
class Scope:
    tenant_id: str
    company_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.company_id:
            raise ValidationError("tenant_id and company_id are required")


@dataclass(frozen=True, slots=True)
class EntityRef:
    type: str
    id: str
    version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True, slots=True)
class Provenance:
    source_type: str
    captured_at: str
    actor_ref: EntityRef
    source_ref: str | None = None
    content_digest: str | None = None
    confidence: float | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValidationError("provenance confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["actor_ref"] = self.actor_ref.to_dict()
        return {k: v for k, v in result.items() if v is not None}


@dataclass(frozen=True, slots=True)
class Company:
    scope: Scope
    display_name: str
    archetype: str
    jurisdiction: Mapping[str, Any]
    owner_refs: tuple[EntityRef, ...]
    lifecycle: LifecycleState = LifecycleState.DRAFT
    readiness: str = "not_ready"
    legal_name: str | None = None
    permissions: tuple[str, ...] = ()
    provenance: tuple[Provenance, ...] = ()
    version: int = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.display_name.strip() or not self.owner_refs:
            raise ValidationError("company needs display_name and at least one owner")
        if not self.provenance:
            raise ValidationError("company requires provenance")
        if self.readiness not in {"not_ready", "ready", "fully_set"}:
            raise ValidationError("invalid readiness")

    def to_contract(self) -> dict[str, Any]:
        return {
            "schema_version": "company.v1",
            "company_id": self.scope.company_id,
            "display_name": self.display_name,
            "legal_name": self.legal_name,
            "archetype": self.archetype,
            "jurisdiction": dict(self.jurisdiction),
            "owner_refs": [ref.to_dict() for ref in self.owner_refs],
            "lifecycle": self.lifecycle.value,
            "readiness": self.readiness,
            "permissions": list(self.permissions),
            "provenance": [item.to_dict() for item in self.provenance],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class BrainRecord:
    scope: Scope
    record_id: str
    kind: RecordKind
    data: Mapping[str, Any]
    knowledge_class: KnowledgeClass
    owner_ref: EntityRef
    provenance: tuple[Provenance, ...]
    confidence: float | None
    lifecycle: str
    version: int
    created_at: str
    updated_at: str
    supersedes_version: int | None = None
    superseded_by_version: int | None = None
    invalidated_at: str | None = None
    invalidation_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.record_id or self.version < 1:
            raise ValidationError("record_id and positive version are required")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValidationError("confidence must be between 0 and 1")
        if not self.provenance:
            raise ValidationError("every record requires provenance")

    @property
    def is_current(self) -> bool:
        return self.superseded_by_version is None


@dataclass(frozen=True, slots=True)
class Dependency:
    scope: Scope
    source_record_id: str
    dependent_ref: EntityRef
    trigger: str
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class InvalidationNotice:
    scope: Scope
    notice_id: str
    source_record_id: str
    source_version: int
    dependent_ref: EntityRef
    trigger: str
    reason: str
    occurred_at: str


def require_transition(current: LifecycleState, target: LifecycleState) -> None:
    if target == current:
        raise InvalidTransitionError(f"lifecycle already {current.value}")
    if target not in LEGAL_TRANSITIONS[current]:
        raise InvalidTransitionError(f"illegal lifecycle transition: {current.value} -> {target.value}")
