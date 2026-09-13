from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"sha256:{sha256(encoded).hexdigest()}"


class SetupMode(StrEnum):
    DO_IT = "do_it"
    GUIDE_ME = "guide_me"
    SKIP = "skip"


class SetupState(StrEnum):
    UNDECIDED = "undecided"
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    WAITING_FOUNDER = "waiting_founder"
    WAITING_EXTERNAL = "waiting_external"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    INVALIDATED = "invalidated"


class AuthorityGate(StrEnum):
    NONE = "none"
    FOUNDER = "founder"
    EXTERNAL = "external"
    FOUNDER_AND_EXTERNAL = "founder_and_external"


class Criticality(StrEnum):
    CRITICAL = "critical"
    MATERIAL = "material"
    ADVISORY = "advisory"


@dataclass(frozen=True)
class ActorBinding:
    """A tenant-scoped identity assertion supplied by the identity boundary."""

    tenant_id: str
    company_id: str
    actor_type: str
    actor_id: str
    verified: bool
    verification_ref: str

    def snapshot(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "company_id": self.company_id,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "verified": self.verified,
            "verification_ref": self.verification_ref,
        }


@dataclass(frozen=True)
class EvidenceReference:
    """Typed evidence metadata; content remains in the segregated evidence store."""

    tenant_id: str
    company_id: str
    evidence_ref: str
    kind: str
    source_type: str
    subject_id: str
    verified: bool
    captured_at: datetime
    source_id: str
    verification_ref: str
    authorized_actor: ActorBinding | None = None
    founder_action_id: str | None = None
    approval_ref: str | None = None
    approval_subject_digest: str | None = None
    exact_target: str | None = None
    amount_minor_units: int | None = None
    currency: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "company_id": self.company_id,
            "evidence_ref": self.evidence_ref,
            "kind": self.kind,
            "source_type": self.source_type,
            "subject_id": self.subject_id,
            "verified": self.verified,
            "captured_at": iso(self.captured_at),
            "source_id": self.source_id,
            "verification_ref": self.verification_ref,
            "authorized_actor": self.authorized_actor.snapshot() if self.authorized_actor else None,
            "founder_action_id": self.founder_action_id,
            "approval_ref": self.approval_ref,
            "approval_subject_digest": self.approval_subject_digest,
            "exact_target": self.exact_target,
            "amount_minor_units": self.amount_minor_units,
            "currency": self.currency,
        }


@dataclass(frozen=True)
class ApprovalBinding:
    """Verified projection of an effective public Approval v2 record."""

    tenant_id: str
    company_id: str
    approval_ref: str
    subject_digest: str
    approver_id: str
    approver_role: str
    state: str
    target: str
    expires_at: datetime
    amount_minor_units: int | None = None
    currency: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "company_id": self.company_id,
            "approval_ref": self.approval_ref,
            "subject_digest": self.subject_digest,
            "approver_id": self.approver_id,
            "approver_role": self.approver_role,
            "state": self.state,
            "target": self.target,
            "expires_at": iso(self.expires_at),
            "amount_minor_units": self.amount_minor_units,
            "currency": self.currency,
        }


@dataclass(frozen=True)
class RequirementDefinition:
    requirement_id: str
    title: str
    description: str
    archetypes: frozenset[str]
    categories: frozenset[str]
    authority_gate: AuthorityGate
    criticality: Criticality
    founder_action_type: str | None = None
    required_evidence_kinds: tuple[str, ...] = ()
    dependency_ids: tuple[str, ...] = ()
    blocks: tuple[str, ...] = ()
    skip_consequence: str = ""
    blocks_fully_set_when_skipped: bool = False
    locality_required: bool = False


@dataclass(frozen=True)
class Provenance:
    source_type: str
    source_ref: str
    captured_at: datetime
    actor_type: str
    actor_id: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "captured_at": iso(self.captured_at),
            "actor_ref": {"type": self.actor_type, "id": self.actor_id},
            "notes": self.notes,
        }


@dataclass(frozen=True)
class SetupHistoryEvent:
    event_id: str
    tenant_id: str
    company_id: str
    setup_item_id: str
    action: str
    occurred_at: datetime
    actor_type: str
    actor_id: str
    before_digest: str | None
    after_digest: str
    reason: str
    correlation_id: str
    sequence: int
    founder_action_digest: str | None = None


@dataclass(frozen=True)
class SetupItem:
    setup_item_id: str
    tenant_id: str
    company_id: str
    requirement_id: str
    mode: SetupMode | None = None
    state: SetupState = SetupState.UNDECIDED
    evidence_refs: tuple[str, ...] = ()
    dependency_versions: tuple[tuple[str, int], ...] = ()
    stale_reason: str | None = None
    current_founder_action_id: str | None = None
    provenance: tuple[Provenance, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    version: int = 1

    def with_update(self, *, at: datetime | None = None, **changes: Any) -> "SetupItem":
        return replace(
            self,
            updated_at=at or utc_now(),
            version=self.version + 1,
            **changes,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "setup_item_id": self.setup_item_id,
            "tenant_id": self.tenant_id,
            "company_id": self.company_id,
            "requirement_id": self.requirement_id,
            "mode": self.mode.value if self.mode else None,
            "state": self.state.value,
            "evidence_refs": list(self.evidence_refs),
            "dependency_versions": dict(self.dependency_versions),
            "stale_reason": self.stale_reason,
            "current_founder_action_id": self.current_founder_action_id,
            "provenance": [item.to_dict() for item in self.provenance],
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "version": self.version,
        }


@dataclass(frozen=True)
class FounderAction:
    founder_action_id: str
    tenant_id: str
    company_id: str
    setup_item_id: str
    action_type: str
    title: str
    reason: str
    instructions: tuple[str, ...]
    risk: str
    irreversible: bool
    required_evidence_kinds: tuple[str, ...]
    blocks: tuple[str, ...]
    state: str
    created_at: datetime
    named_approver_id: str
    named_approver_role: str
    exact_target: str
    approval_ref: str
    approval_subject_digest: str
    expires_at: datetime
    amount_minor_units: int | None = None
    currency: str | None = None
    evidence_refs: tuple[str, ...] = ()
    version: int = 1

    def with_update(self, **changes: Any) -> "FounderAction":
        return replace(self, version=self.version + 1, **changes)

    def snapshot(self) -> dict[str, Any]:
        return {
            "founder_action_id": self.founder_action_id,
            "tenant_id": self.tenant_id,
            "company_id": self.company_id,
            "setup_item_id": self.setup_item_id,
            "action_type": self.action_type,
            "state": self.state,
            "named_approver_id": self.named_approver_id,
            "named_approver_role": self.named_approver_role,
            "exact_target": self.exact_target,
            "approval_ref": self.approval_ref,
            "approval_subject_digest": self.approval_subject_digest,
            "expires_at": iso(self.expires_at),
            "amount_minor_units": self.amount_minor_units,
            "currency": self.currency,
            "evidence_refs": list(self.evidence_refs),
            "version": self.version,
        }

    def to_contract(self) -> dict[str, Any]:
        return {
            "schema_version": "founder-action.v2",
            "tenant_id": self.tenant_id,
            "founder_action_id": self.founder_action_id,
            "company_id": self.company_id,
            "action_type": self.action_type,
            "title": self.title,
            "reason": self.reason,
            "instructions": list(self.instructions),
            "risk": self.risk,
            "irreversible": self.irreversible,
            "state": self.state,
            "required_evidence_kinds": list(self.required_evidence_kinds),
            "evidence_refs": [{"type": "evidence", "id": item} for item in self.evidence_refs],
            "blocks": [
                *({"type": "readiness", "id": item} for item in self.blocks),
                {"type": "approval", "id": self.approval_ref},
            ],
            "due_at": iso(self.expires_at),
            "created_at": iso(self.created_at),
            "version": self.version,
        }


@dataclass(frozen=True)
class SetupProjection:
    tenant_id: str
    company_id: str
    fully_set_eligible: bool
    blockers: tuple[str, ...]
    consequences: tuple[str, ...]
    pending_founder_actions: tuple[FounderAction, ...]
