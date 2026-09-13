from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"sha256:{sha256(encoded).hexdigest()}"


class RoleState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    ESCALATE = "escalate"
    DENY = "deny"


@dataclass(frozen=True)
class ManagementAuthorityProof:
    """Opaque proof whose authenticity is checked by the service boundary."""

    proof_ref: str
    tenant_id: str
    company_id: str
    actor_id: str
    actor_role: str

    def __post_init__(self) -> None:
        if not all((self.proof_ref, self.tenant_id, self.company_id, self.actor_id)):
            raise ValueError("management authority proof identity and scope are required")
        if self.actor_role not in {"founder", "authorized_manager"}:
            raise ValueError("management authority must belong to a founder or authorized manager")


@dataclass(frozen=True)
class BudgetCeiling:
    currency: str
    per_action_minor: int
    period_minor: int
    period: str = "day"

    def __post_init__(self) -> None:
        if len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError("currency must be a three-letter uppercase code")
        if self.period not in {"day", "month"}:
            raise ValueError("budget period must be day or month")
        if self.per_action_minor < 0 or self.period_minor < 0:
            raise ValueError("budget ceilings cannot be negative")
        if self.per_action_minor > self.period_minor:
            raise ValueError("per-action ceiling cannot exceed period ceiling")


@dataclass(frozen=True)
class CapabilityGrant:
    capability: str
    actions: frozenset[str]

    def __post_init__(self) -> None:
        if not self.capability or not self.actions or any(not action for action in self.actions):
            raise ValueError("capability grants require a capability and explicit actions")


@dataclass(frozen=True)
class EscalationRule:
    trigger: str
    reason: str
    required_role: str = "founder"


@dataclass(frozen=True)
class RoleDefinition:
    role_id: str
    tenant_id: str
    company_id: str
    name: str
    objective: str
    grants: tuple[CapabilityGrant, ...]
    denied_actions: frozenset[str]
    approval_actions: frozenset[str]
    escalation_rules: tuple[EscalationRule, ...]
    budget: BudgetCeiling
    version: int = 1
    authority_epoch: int = 1
    state: RoleState = RoleState.ACTIVE
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not all((self.role_id, self.tenant_id, self.company_id, self.name, self.objective)):
            raise ValueError("role identity, scope, name, and objective are required")
        if self.version < 1:
            raise ValueError("role version must be positive")
        if self.authority_epoch < 1:
            raise ValueError("role authority epoch must be positive")
        granted = {action for grant in self.grants for action in grant.actions}
        overlap = granted & self.denied_actions
        if overlap:
            raise ValueError(f"actions cannot be both granted and denied: {sorted(overlap)}")
        if not self.approval_actions <= granted:
            raise ValueError("approval actions must also be explicitly granted")

    @property
    def definition_digest(self) -> str:
        return digest(self.to_projection())

    def to_projection(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "tenant_id": self.tenant_id,
            "company_id": self.company_id,
            "name": self.name,
            "objective": self.objective,
            "grants": [
                {"capability": grant.capability, "actions": sorted(grant.actions)}
                for grant in self.grants
            ],
            "denied_actions": sorted(self.denied_actions),
            "approval_actions": sorted(self.approval_actions),
            "escalation_rules": [
                {"trigger": rule.trigger, "reason": rule.reason, "required_role": rule.required_role}
                for rule in self.escalation_rules
            ],
            "budget": {
                "currency": self.budget.currency,
                "per_action_minor": self.budget.per_action_minor,
                "period_minor": self.budget.period_minor,
                "period": self.budget.period,
            },
            "version": self.version,
            "authority_epoch": self.authority_epoch,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class ActionRequest:
    request_id: str
    idempotency_key: str
    tenant_id: str
    company_id: str
    role_id: str
    role_version: int
    capability: str
    action: str
    estimated_minor: int = 0
    currency: str = "USD"
    period_spend_minor: int = 0
    context_flags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not all(
            (self.request_id, self.idempotency_key, self.tenant_id, self.company_id,
             self.role_id, self.capability, self.action)
        ):
            raise ValueError("request identity, scope, role, capability, and action are required")
        if self.role_version < 1:
            raise ValueError("role_version must be positive")
        if self.estimated_minor < 0 or self.period_spend_minor < 0:
            raise ValueError("spend values cannot be negative")

    @property
    def request_digest(self) -> str:
        return digest({
            "request_id": self.request_id,
            "tenant_id": self.tenant_id,
            "company_id": self.company_id,
            "role_id": self.role_id,
            "role_version": self.role_version,
            "capability": self.capability,
            "action": self.action,
            "estimated_minor": self.estimated_minor,
            "currency": self.currency,
            "period_spend_minor": self.period_spend_minor,
            "context_flags": sorted(self.context_flags),
        })


@dataclass(frozen=True)
class PolicyEvaluation:
    evaluation_id: str
    tenant_id: str
    company_id: str
    role_id: str
    role_version: int
    authority_epoch: int | None
    request_id: str
    request_digest: str
    decision: PolicyDecision
    reason_code: str
    reason: str
    required_role: str | None
    estimated_minor: int
    definition_digest: str | None
    evaluated_at: datetime

    @property
    def permits_execution(self) -> bool:
        return self.decision is PolicyDecision.ALLOW


@dataclass(frozen=True)
class AuditRecord:
    audit_id: str
    tenant_id: str
    company_id: str
    action: str
    target_id: str
    actor_id: str
    reason: str
    before_digest: str | None
    after_digest: str | None
    occurred_at: datetime
    append_only: bool = True
