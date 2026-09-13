from __future__ import annotations

from dataclasses import asdict, dataclass, field
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


class JobStatus(StrEnum):
    QUEUED = "queued"
    WAITING_DEPENDENCIES = "waiting_dependencies"
    WAITING_APPROVAL = "waiting_approval"
    RUNNABLE = "runnable"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalMode(StrEnum):
    AUTONOMOUS = "autonomous"
    APPROVAL_REQUIRED = "approval_required"
    FOUNDER_ONLY = "founder_only"
    PROHIBITED = "prohibited"


class ApprovalState(StrEnum):
    REQUESTED = "requested"
    GRANTED = "granted"
    DENIED = "denied"
    REVOKED = "revoked"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class FailureKind(StrEnum):
    RETRYABLE = "retryable_failure"
    PERMANENT = "permanent_failure"
    APPROVAL_BLOCKED = "approval_blocked"
    BUDGET_BLOCKED = "budget_blocked"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    CANCELLED = "cancelled"
    PROHIBITED = "prohibited"


@dataclass(frozen=True)
class Money:
    currency: str
    minor_units: int

    def __post_init__(self) -> None:
        if len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError("currency must be a three-letter uppercase code")
        if not isinstance(self.minor_units, int) or isinstance(self.minor_units, bool):
            raise TypeError("minor_units must be an integer")
        if self.minor_units < 0:
            raise ValueError("money cannot be negative")

    def to_contract(self) -> dict[str, Any]:
        return {"currency": self.currency, "minor_units": self.minor_units}


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")


@dataclass(frozen=True)
class CapabilityFailure:
    code: str
    safe_message: str
    retryable: bool
    kind: FailureKind


@dataclass(frozen=True)
class ArtifactRef:
    type: str
    id: str
    version: int = 1

    def to_contract(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CapabilityRequest:
    request_id: str
    tenant_id: str
    company_id: str
    job_id: str
    capability: str
    capability_version: str
    idempotency_key: str
    correlation_id: str
    inputs: dict[str, Any]
    budget_ref: str


@dataclass(frozen=True)
class CapabilityResult:
    provider_ref: str
    status: str
    artifacts: tuple[ArtifactRef, ...]
    spend: Money
    progress: tuple[str, ...] = ()
    failure: CapabilityFailure | None = None


@dataclass
class Event:
    event_id: str
    tenant_id: str
    company_id: str
    correlation_id: str
    causation_id: str | None
    type: str
    occurred_at: datetime
    payload: dict[str, Any]
    source: str
    version: int = 1
    sequence: int = 1
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        self.recorded_at = self.recorded_at or self.occurred_at

    @property
    def payload_digest(self) -> str:
        return digest(self.payload)

    def to_contract(self) -> dict[str, Any]:
        """Project richer runtime event onto released event.v1 without changing it."""
        return {
            "schema_version": "event.v1",
            "event_id": self.event_id,
            "company_id": self.company_id,
            "event_type": self.type,
            "occurred_at": iso(self.occurred_at),
            "recorded_at": iso(self.recorded_at or self.occurred_at),
            "producer": self.source,
            "sequence": self.sequence,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "payload": self.payload,
            "payload_digest": self.payload_digest,
            "visibility": "internal",
        }


@dataclass
class ApprovalRecord:
    approval_id: str
    tenant_id: str
    company_id: str
    job_id: str
    mode: ApprovalMode
    subject_digest: str
    required_role: str
    state: ApprovalState = ApprovalState.REQUESTED
    decided_by: str | None = None
    decided_by_role: str | None = None
    expires_at: datetime | None = None
    version: int = 1

    def is_effective(self, now: datetime, expected_digest: str) -> bool:
        return (
            self.state == ApprovalState.GRANTED
            and self.subject_digest == expected_digest
            and (self.expires_at is None or self.expires_at > now)
            and (self.mode != ApprovalMode.FOUNDER_ONLY or self.decided_by_role == "founder")
        )


@dataclass
class Budget:
    budget_id: str
    tenant_id: str
    company_id: str
    ceiling: Money
    reserved_minor: int = 0
    settled_minor: int = 0
    state: str = "active"
    version: int = 1

    @property
    def remaining_minor(self) -> int:
        return self.ceiling.minor_units - self.reserved_minor - self.settled_minor


@dataclass
class Job:
    job_id: str
    tenant_id: str
    company_id: str
    capability: str
    capability_version: str
    inputs: dict[str, Any]
    dependency_ids: tuple[str, ...]
    status: JobStatus
    attempts: int
    retry_policy: RetryPolicy
    budget_ref: str
    per_job_ceiling: Money
    approval_mode: ApprovalMode
    approval_ids: tuple[str, ...]
    artifacts: tuple[ArtifactRef, ...]
    failure: CapabilityFailure | None
    idempotency_key: str
    correlation_id: str
    causation_id: str | None
    provenance: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    provider_ref: str | None = None
    reserved_minor: int = 0
    settled_minor: int = 0
    version: int = 1

    @property
    def terminal(self) -> bool:
        return self.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}

    @property
    def subject_digest(self) -> str:
        return digest({"job_id": self.job_id, "capability": self.capability, "inputs": self.inputs})

    def contract_status(self) -> str:
        return {
            JobStatus.WAITING_DEPENDENCIES: "blocked",
            JobStatus.RUNNABLE: "queued",
            JobStatus.WAITING_APPROVAL: "waiting_approval",
        }.get(self.status, self.status.value)

    def to_contract(self) -> dict[str, Any]:
        return {
            "schema_version": "job.v1",
            "job_id": self.job_id,
            "company_id": self.company_id,
            # Capability v1 separates opaque capability_id from dotted `kind`.
            # The runtime uses the dotted kind; this is its stable v1 ID projection.
            "capability_id": self.capability.replace(".", "_"),
            "objective": str(self.inputs.get("objective", f"Execute capability {self.capability}")),
            "status": self.contract_status(),
            "idempotency_key": self.idempotency_key,
            "input_artifact_refs": list(self.inputs.get("input_artifact_refs", [])),
            "output_artifact_refs": [a.to_contract() for a in self.artifacts],
            "required_approval_refs": [
                {"type": "approval", "id": value, "version": 1} for value in self.approval_ids
            ],
            "budget_ref": {"type": "budget", "id": self.budget_ref, "version": 1},
            "provider_ref": self.provider_ref,
            **(
                {
                    "failure": {
                        "code": self.failure.code,
                        "retryable": self.failure.retryable,
                        "safe_message": self.failure.safe_message,
                    }
                }
                if self.failure
                else {}
            ),
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "version": self.version,
        }
