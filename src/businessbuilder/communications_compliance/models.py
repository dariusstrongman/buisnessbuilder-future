from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from businessbuilder.outbound_communications.models import (
    CommunicationPurpose,
    ConsentState,
    DestinationType,
    PolicyOutcome,
)


class RolloutTier(StrEnum):
    DISABLED = "disabled"
    SANDBOX = "sandbox"
    INTERNAL_CANARY = "internal_canary"
    LIMITED_LIVE = "limited_live"
    GENERAL_LIVE = "general_live"


class KillSwitchScope(StrEnum):
    GLOBAL = "global"
    TENANT = "tenant"
    COMPANY = "company"
    PROVIDER_CONNECTION = "provider_connection"
    CHANNEL = "channel"


class PIIClass(StrEnum):
    DESTINATION_IDENTIFIER = "destination_identifier"
    RECIPIENT_METADATA = "recipient_metadata"
    MESSAGE_CONTENT = "message_content"
    DELIVERY_METADATA = "delivery_metadata"
    CONSENT_EVIDENCE = "consent_evidence"
    AUDIT_METADATA = "audit_metadata"
    PROVIDER_IDENTIFIER = "provider_identifier"


class RetentionClass(StrEnum):
    OPERATIONAL_SHORT = "operational_short"
    DELIVERY_OPERATIONS = "delivery_operations"
    PROVIDER_RECEIPT = "provider_receipt"
    COMPLIANCE_EVIDENCE = "compliance_evidence"
    AUDIT_REQUIRED = "audit_required"


class AlertClass(StrEnum):
    PROVIDER_AUTH_FAILURE = "provider_auth_failure"
    COMPLAINT_SPIKE = "complaint_spike"
    BOUNCE_SPIKE = "bounce_spike"
    SUPPRESSION_VIOLATION = "suppression_violation"
    CONTENT_POLICY_DENIAL = "content_policy_denial"
    SEND_RATE_ANOMALY = "send_rate_anomaly"
    CALLBACK_VERIFICATION_FAILURE = "callback_verification_failure"
    RECONCILIATION_FAILURE = "reconciliation_failure"
    KILL_SWITCH_ENGAGED = "kill_switch_engaged"
    PROVIDER_OUTAGE = "provider_outage"


class AlertSeverity(StrEnum):
    WARNING = "warning"
    THROTTLE = "throttle"
    SUSPEND = "suspend"
    EMERGENCY = "emergency"


class AbuseSignalType(StrEnum):
    COMPLAINT = "complaint"
    HARD_BOUNCE = "hard_bounce"
    SEND_BURST = "send_burst"
    DENIED_SEND = "denied_send"
    INVALID_RECIPIENT = "invalid_recipient"
    OPT_OUT = "opt_out"
    SUSPICIOUS_PURPOSE = "suspicious_purpose"
    PROVIDER_AUTH_FAILURE = "provider_auth_failure"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    CALLBACK_VERIFICATION_FAILURE = "callback_verification_failure"


@dataclass(frozen=True, slots=True)
class JurisdictionContext:
    tenant_id: str
    company_id: str
    tenant_jurisdiction: str
    company_jurisdiction: str
    recipient_id: str | None
    recipient_jurisdiction: str | None
    established_at: datetime
    source_ref: str


@dataclass(frozen=True, slots=True)
class JurisdictionPolicy:
    policy_id: str
    version: str
    supported_jurisdictions: tuple[str, ...]
    channel: DestinationType
    purposes: tuple[CommunicationPurpose, ...]
    allowed_consent: tuple[ConsentState, ...]
    retention_days: int
    evidence_recordkeeping_days: int
    opt_out_required: bool
    disclosure_required: bool
    marketing_restricted: bool
    quiet_hours: tuple[int, int] | None
    current_consent_version_required: bool
    legally_reviewed: bool = False


@dataclass(frozen=True, slots=True)
class ConsentEvidence:
    consent_evidence_id: str
    tenant_id: str
    company_id: str
    recipient_id: str
    purpose: CommunicationPurpose | None
    channel: DestinationType
    consent_basis: ConsentState
    source: str
    captured_at: datetime
    actor_or_event_ref: str
    evidence_ref: str
    policy_version: str
    terms_version: str | None
    expires_at: datetime | None
    withdrawn_at: datetime | None = None
    supersedes_id: str | None = None
    superseded_by_id: str | None = None

    def active_at(self, at: datetime) -> bool:
        return (
            self.withdrawn_at is None
            and self.superseded_by_id is None
            and (self.expires_at is None or self.expires_at > at)
            and self.consent_basis not in {ConsentState.UNKNOWN, ConsentState.WITHDRAWN, ConsentState.PROHIBITED}
        )


@dataclass(frozen=True, slots=True)
class UnsubscribeTokenRecord:
    token_record_id: str
    token_digest: str
    tenant_id: str
    company_id: str
    recipient_id: str
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RetentionRecord:
    retention_record_id: str
    tenant_id: str
    company_id: str
    subject_type: str
    subject_id: str
    pii_classes: tuple[PIIClass, ...]
    retention_class: RetentionClass
    created_at: datetime
    expires_at: datetime
    compliance_hold: bool
    deletion_eligible: bool
    deletion_reason: str | None
    policy_version: str
    deleted_at: datetime | None = None
    pseudonymized: bool = False


@dataclass(frozen=True, slots=True)
class ErasureRecord:
    erasure_id: str
    tenant_id: str
    company_id: str
    recipient_id: str
    requested_by: str
    reason: str
    requested_at: datetime
    completed_at: datetime | None
    operational_pii_removed: bool
    compliance_evidence_preserved: bool
    blocked_by_hold: bool


@dataclass(frozen=True, slots=True)
class KillSwitchRecord:
    kill_switch_id: str
    scope: KillSwitchScope
    scope_id: str
    tenant_id: str
    company_id: str
    engaged: bool
    reason_code: str
    changed_by: str
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class RolloutPolicy:
    rollout_id: str
    tenant_id: str
    company_id: str
    tier: RolloutTier
    approved_tenants: tuple[str, ...]
    approved_companies: tuple[str, ...]
    approved_providers: tuple[str, ...]
    approved_purposes: tuple[CommunicationPurpose, ...]
    approved_recipient_domains: tuple[str, ...]
    max_sends_hour: int
    max_sends_day: int
    max_recipients: int
    founder_approval_required: bool
    monitoring_required: bool
    changed_by: str
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class ComplianceDecision:
    compliance_decision_id: str
    tenant_id: str
    company_id: str
    communication_id: str
    job_id: str | None
    recipient_id: str
    purpose: CommunicationPurpose
    tenant_jurisdiction: str
    company_jurisdiction: str
    recipient_jurisdiction: str
    policy_version: str
    consent_basis: ConsentState
    suppression_result: str
    retention_class: RetentionClass
    approval_result: str
    rollout_tier: RolloutTier
    kill_switch_result: str
    outcome: PolicyOutcome
    reason_code: str
    stage: str
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class AbuseSignal:
    signal_id: str
    tenant_id: str
    company_id: str
    signal_type: AbuseSignalType
    source_ref: str
    occurred_at: datetime
    safe_details: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class AbuseThresholds:
    warning: int = 3
    throttle: int = 5
    suspend: int = 10
    emergency: int = 20
    window_seconds: int = 3600


@dataclass(frozen=True, slots=True)
class OperationalAlert:
    alert_id: str
    tenant_id: str
    company_id: str
    alert_class: AlertClass
    severity: AlertSeverity
    reason_code: str
    source_ref: str
    created_at: datetime
    acknowledged_at: datetime | None = None

