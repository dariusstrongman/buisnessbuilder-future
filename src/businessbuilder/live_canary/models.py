from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from businessbuilder.outbound_communications.models import CommunicationPurpose


class ReadinessState(StrEnum):
    NOT_READY = "not_ready"
    CANARY_READY = "canary_ready"


class ReputationState(StrEnum):
    UNKNOWN = "unknown"
    GOOD = "good"
    WARNING = "warning"
    BLOCKED = "blocked"


class CanaryPermitStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED_SIMULATION = "approved_simulation"
    EXPIRED = "expired"
    REVOKED = "revoked"


class CanaryAlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class ReconciliationState(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    RETRY = "retry"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProviderDeliveryContract:
    provider: str
    oauth_scopes: frozenset[str]
    send_operation: str
    idempotency_mode: str
    rate_limit_model: str
    callback_event_types: tuple[str, ...]
    callback_verification_method: str
    callback_required_refs: tuple[str, ...]
    token_refresh_behavior: str
    error_classification: tuple[str, ...]
    reconciliation_fallback: str
    sender_identity_requirements: tuple[str, ...]
    network_delivery_enabled: bool = False


@dataclass(frozen=True, slots=True)
class SenderIdentityReadiness:
    sender_id: str
    tenant_id: str
    company_id: str
    provider_connection_id: str
    sender_address: str
    domain: str
    domain_ownership_verified: bool
    spf_valid: bool
    dkim_valid: bool
    dmarc_present: bool
    alignment_valid: bool
    provider_verified: bool
    reputation: ReputationState
    evaluated_at: datetime

    @property
    def domain_auth_valid(self) -> bool:
        return all((self.domain_ownership_verified, self.spf_valid, self.dkim_valid,
                    self.dmarc_present, self.alignment_valid, self.provider_verified))


@dataclass(frozen=True, slots=True)
class DeliverabilityHealth:
    health_id: str
    tenant_id: str
    company_id: str
    provider_connection_id: str
    delivery_success_rate: float
    deferred_rate: float
    hard_bounce_rate: float
    complaint_rate: float
    rejection_rate: float
    provider_throttled: bool
    authentication_failures: int
    reputation_warnings: tuple[str, ...]
    last_successful_send_at: datetime | None
    last_callback_at: datetime | None
    last_reconciled_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CanaryEligibility:
    evaluation_id: str
    tenant_id: str
    company_id: str
    provider_connection_id: str
    sender_id: str
    state: ReadinessState
    gates: tuple[tuple[str, bool, str], ...]
    explicit_approval_present: bool
    live_send_enabled: bool
    evaluated_at: datetime

    @property
    def failed_reasons(self) -> tuple[str, ...]:
        return tuple(reason for _, passed, reason in self.gates if not passed)


@dataclass(frozen=True, slots=True)
class LiveGateCeremony:
    ceremony_id: str
    tenant_id: str
    company_id: str
    provider_connection_id: str
    sender_id: str
    operator_id: str
    monitoring_owner: str
    rollback_plan_ref: str
    kill_switch_test_ref: str
    approval_ref: str
    checklist: tuple[tuple[str, bool], ...]
    occurred_at: datetime
    live_gate_changed: bool = False


@dataclass(frozen=True, slots=True)
class CanaryPermit:
    permit_id: str
    tenant_id: str
    company_id: str
    provider_connection_id: str
    sender_id: str
    recipient_digests: tuple[str, ...]
    recipient_domains: tuple[str, ...]
    allowed_purposes: tuple[CommunicationPurpose, ...]
    max_sends_total: int
    max_sends_hour: int
    starts_at: datetime
    expires_at: datetime
    operator_owner: str
    founder_approval_ref: str
    monitoring_required: bool
    ceremony_id: str
    signature: str
    status: CanaryPermitStatus
    simulation_only: bool = True
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CanarySendReservation:
    reservation_id: str
    permit_id: str
    tenant_id: str
    company_id: str
    communication_id: str
    idempotency_key: str
    reserved_at: datetime


@dataclass(frozen=True, slots=True)
class CanaryMetric:
    metric_id: str
    tenant_id: str
    company_id: str
    name: str
    value: int
    source_ref: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class CanaryAlert:
    alert_id: str
    tenant_id: str
    company_id: str
    severity: CanaryAlertSeverity
    alert_type: str
    reason_code: str
    source_ref: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationTask:
    task_id: str
    tenant_id: str
    company_id: str
    provider_connection_id: str
    provider_request_id: str
    receipt_id: str
    state: ReconciliationState
    attempts: int
    next_attempt_at: datetime
    updated_at: datetime
