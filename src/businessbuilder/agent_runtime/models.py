from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any

from businessbuilder.runtime.models import ArtifactRef, Money
from businessbuilder.access_broker.models import JobSecretRef


class TriggerClass(StrEnum):
    CUSTOMER_REQUEST = "authenticated_customer_request"
    CANONICAL_EVENT = "canonical_business_event"
    SCHEDULED_JOB = "scheduled_runtime_job"
    RECURRING_TASK = "recurring_operational_task"
    INBOUND_EVENT = "inbound_integration_event"


class ExecutionState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RETRYABLE = "retryable"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeliveryState(StrEnum):
    PENDING = "pending"
    DISPATCHING = "dispatching"
    ACKNOWLEDGED = "acknowledged"


@dataclass(frozen=True, slots=True)
class ModelPolicy:
    quality_floor: int
    requires_tools: bool = False
    requires_vision: bool = False
    allowed_providers: tuple[str, ...] = ()
    maximum_latency_ms: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.quality_floor <= 100:
            raise ValueError("quality_floor must be between 1 and 100")
        if self.maximum_latency_ms is not None and self.maximum_latency_ms < 1:
            raise ValueError("maximum_latency_ms must be positive")


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    provider: str
    model: str
    capabilities: frozenset[str]
    quality: int
    available: bool
    healthy: bool
    quota_available: bool
    latency_ms: int
    estimated_cost: Money


@dataclass(frozen=True, slots=True)
class ModelSelection:
    provider: str
    model: str
    estimated_cost: Money
    reason: str


@dataclass(frozen=True, slots=True)
class AgentJobEnvelope:
    tenant_id: str
    company_id: str
    job_id: str
    correlation_id: str
    causation_id: str | None
    capability: str
    capability_version: str
    action: str
    agent_role: str
    agent_role_version: int
    entitlement_refs: tuple[str, ...]
    permission_scope: tuple[str, ...]
    approval_refs: tuple[str, ...]
    reserved_budget: Money
    maximum_job_spend: Money
    model_policy: ModelPolicy
    model_selection: ModelSelection
    input_artifact_refs: tuple[ArtifactRef, ...]
    attempt: int
    max_attempts: int
    idempotency_key: str
    trigger_class: TriggerClass
    trigger_ref: str
    triggered_by: str
    policy_evaluation_id: str
    policy_definition_digest: str
    created_at: datetime
    expires_at: datetime
    secret_refs: tuple[JobSecretRef, ...] = ()

    def __post_init__(self) -> None:
        required = (
            self.tenant_id,
            self.company_id,
            self.job_id,
            self.correlation_id,
            self.capability,
            self.action,
            self.agent_role,
            self.entitlement_refs,
            self.permission_scope,
            self.idempotency_key,
            self.trigger_ref,
            self.triggered_by,
            self.policy_evaluation_id,
            self.policy_definition_digest,
        )
        if not all(required):
            raise ValueError("agent job envelope is missing required authority fields")
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("agent job timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("agent job must expire after creation")
        if not 1 <= self.attempt <= self.max_attempts <= 20:
            raise ValueError("agent job retry metadata is invalid")
        if self.reserved_budget.currency != self.maximum_job_spend.currency:
            raise ValueError("agent job budget currency mismatch")
        if self.reserved_budget.minor_units > self.maximum_job_spend.minor_units:
            raise ValueError("reserved budget exceeds maximum job spend")

    @property
    def envelope_digest(self) -> str:
        return "sha256:" + sha256(self.to_json().encode()).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": "agent-job.v1",
            "tenant_id": self.tenant_id,
            "company_id": self.company_id,
            "job_id": self.job_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "capability": self.capability,
            "capability_version": self.capability_version,
            "action": self.action,
            "agent_role": self.agent_role,
            "agent_role_version": self.agent_role_version,
            "entitlement_refs": list(self.entitlement_refs),
            "permission_scope": list(self.permission_scope),
            "approval_refs": list(self.approval_refs),
            "reserved_budget": self.reserved_budget.to_contract(),
            "maximum_job_spend": self.maximum_job_spend.to_contract(),
            "model_policy": asdict(self.model_policy),
            "model_selection": {
                "provider": self.model_selection.provider,
                "model": self.model_selection.model,
                "estimated_cost": self.model_selection.estimated_cost.to_contract(),
                "reason": self.model_selection.reason,
            },
            "input_artifact_refs": [item.to_contract() for item in self.input_artifact_refs],
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "idempotency_key": self.idempotency_key,
            "trigger_class": self.trigger_class.value,
            "trigger_ref": self.trigger_ref,
            "triggered_by": self.triggered_by,
            "policy_evaluation_id": self.policy_evaluation_id,
            "policy_definition_digest": self.policy_definition_digest,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
        }
        if self.secret_refs:
            payload["secret_refs"] = [item.to_contract() for item in self.secret_refs]
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> AgentJobEnvelope:
        if value.get("schema_version") != "agent-job.v1":
            raise ValueError("unsupported agent job envelope")
        policy = dict(value["model_policy"])
        policy["allowed_providers"] = tuple(policy.get("allowed_providers", ()))
        return cls(
            tenant_id=value["tenant_id"],
            company_id=value["company_id"],
            job_id=value["job_id"],
            correlation_id=value["correlation_id"],
            causation_id=value.get("causation_id"),
            capability=value["capability"],
            capability_version=value["capability_version"],
            action=value["action"],
            agent_role=value["agent_role"],
            agent_role_version=int(value["agent_role_version"]),
            entitlement_refs=tuple(value["entitlement_refs"]),
            permission_scope=tuple(value["permission_scope"]),
            approval_refs=tuple(value["approval_refs"]),
            reserved_budget=Money(**value["reserved_budget"]),
            maximum_job_spend=Money(**value["maximum_job_spend"]),
            model_policy=ModelPolicy(**policy),
            model_selection=ModelSelection(
                value["model_selection"]["provider"],
                value["model_selection"]["model"],
                Money(**value["model_selection"]["estimated_cost"]),
                value["model_selection"]["reason"],
            ),
            input_artifact_refs=tuple(ArtifactRef(**item) for item in value["input_artifact_refs"]),
            attempt=int(value["attempt"]),
            max_attempts=int(value["max_attempts"]),
            idempotency_key=value["idempotency_key"],
            trigger_class=TriggerClass(value["trigger_class"]),
            trigger_ref=value["trigger_ref"],
            triggered_by=value["triggered_by"],
            policy_evaluation_id=value["policy_evaluation_id"],
            policy_definition_digest=value["policy_definition_digest"],
            created_at=datetime.fromisoformat(value["created_at"].replace("Z", "+00:00")),
            expires_at=datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00")),
            secret_refs=tuple(JobSecretRef(**item) for item in value.get("secret_refs", ())),
        )


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    tenant_id: str
    company_id: str
    job_id: str
    envelope_digest: str
    state: ExecutionState
    attempts: int
    created_at: datetime
    updated_at: datetime
    lease_owner: str | None = None
    lease_until: datetime | None = None
    provider: str | None = None
    model: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    actual_cost: Money | None = None
    outcome: str | None = None
    failure_classification: str | None = None
    emitted_event_ids: tuple[str, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()


@dataclass(frozen=True, slots=True)
class QueueOutboxRecord:
    message_id: str
    idempotency_key: str
    tenant_id: str
    company_id: str
    job_id: str
    envelope_digest: str
    state: DeliveryState
    attempts: int
    available_at: datetime
    created_at: datetime
    claimed_by: str | None = None
    claimed_until: datetime | None = None
    acknowledged_at: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class QueueDelivery:
    message_id: str
    receipt_handle: str
    payload: dict[str, Any]
    receive_count: int


@dataclass(frozen=True, slots=True)
class RuntimeSchedule:
    schedule_id: str
    tenant_id: str
    company_id: str
    role_id: str
    capability: str
    action: str
    interval_seconds: int
    next_due_at: datetime
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.interval_seconds < 60:
            raise ValueError("schedule interval must be at least 60 seconds")
        if self.next_due_at.tzinfo is None:
            raise ValueError("schedule timestamp must be timezone-aware")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


SENSITIVE_KEY_PARTS = frozenset(
    {"api_key", "password", "secret", "credential", "authorization", "access_key", "private_key", "token"}
)


def assert_queue_payload_safe(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized == "secret_refs":
                if not isinstance(item, list):
                    raise ValueError("queue secret_refs must be a list of opaque references")
                for reference in item:
                    if not isinstance(reference, dict) or set(reference) != {
                        "secret_ref", "provider", "capability", "tenant_id", "company_id"
                    }:
                        raise ValueError("queue secret reference contains unexpected fields")
                    JobSecretRef(**reference)
                continue
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                raise ValueError("queue payload contains a prohibited sensitive field")
            assert_queue_payload_safe(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_queue_payload_safe(item)
