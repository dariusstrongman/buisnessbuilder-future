from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import re


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,159}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")


def _id(value: str, name: str) -> None:
    if not _ID.fullmatch(value):
        raise ValueError(f"{name} is not a safe opaque identifier")


def _time(value: datetime | None, name: str) -> None:
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


class DestinationType(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    VOICE = "voice"
    SOCIAL = "social"


class ContactRelationship(StrEnum):
    INBOUND_CUSTOMER = "inbound_customer"
    QUOTE_REQUESTER = "quote_requester"
    CUSTOMER = "customer"
    SERVICE_RECIPIENT = "service_recipient"
    LEAD = "lead"
    UNKNOWN = "unknown"


class ConsentState(StrEnum):
    EXPLICIT = "explicit"
    TRANSACTIONAL_RELATIONSHIP = "transactional_relationship"
    CUSTOMER_INITIATED = "customer_initiated"
    SERVICE_FOLLOWUP = "service_followup"
    UNKNOWN = "unknown"
    WITHDRAWN = "withdrawn"
    PROHIBITED = "prohibited"


class SuppressionState(StrEnum):
    CLEAR = "clear"
    SUPPRESSED = "suppressed"


class SuppressionReason(StrEnum):
    RECIPIENT_OPT_OUT = "recipient_opt_out"
    HARD_BOUNCE = "hard_bounce"
    COMPLAINT = "complaint"
    ADMIN = "admin_suppression"
    ABUSE_SIGNAL = "abuse_signal"
    LEGAL_BLOCK = "legal_compliance_block"
    INVALID_ADDRESS = "invalid_address"
    DISCONNECTED_RELATIONSHIP = "disconnected_relationship"
    CUSTOMER_REQUEST = "customer_request"


class CommunicationPurpose(StrEnum):
    REPLY_TO_INBOUND = "reply_to_inbound"
    QUOTE_RESPONSE = "quote_response"
    APPOINTMENT_CONFIRMATION = "appointment_confirmation"
    SERVICE_FOLLOWUP = "service_followup"
    REVIEW_REQUEST = "review_request"
    OPERATIONAL_NOTICE = "operational_notice"
    MARKETING = "marketing"
    RE_ENGAGEMENT = "re_engagement"


class PolicyOutcome(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"


class DeliveryStatus(StrEnum):
    QUEUED = "queued"
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    DEFERRED = "deferred"
    BOUNCED = "bounced"
    REJECTED = "rejected"
    COMPLAINED = "complained"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RecipientRecord:
    recipient_id: str
    tenant_id: str
    company_id: str
    normalized_destination: str
    destination_type: DestinationType
    source: str
    relationship: ContactRelationship
    consent_state: ConsentState
    consent_provenance: str
    consent_at: datetime | None
    suppression_state: SuppressionState
    suppression_reason: str | None
    opt_out_at: datetime | None
    last_contacted_at: datetime | None
    risk_flags: tuple[str, ...]
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in ("recipient_id", "tenant_id", "company_id", "source", "consent_provenance"):
            _id(getattr(self, name), name)
        if self.destination_type is not DestinationType.EMAIL:
            raise ValueError("only email recipient policy is implemented")
        if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", self.normalized_destination):
            raise ValueError("email destination is malformed")
        if len(self.normalized_destination) > 254 or any(c in self.normalized_destination for c in "\r\n\0"):
            raise ValueError("email destination is malformed")
        for value in (self.consent_at, self.opt_out_at, self.last_contacted_at, self.created_at, self.updated_at):
            _time(value, "recipient timestamp")
        if len(self.risk_flags) > 16 or any(not _ID.fullmatch(value) for value in self.risk_flags):
            raise ValueError("recipient risk flags are invalid")
        if self.suppression_state is SuppressionState.SUPPRESSED and not self.suppression_reason:
            raise ValueError("suppressed recipient requires a reason")
        if self.consent_state is ConsentState.WITHDRAWN and self.opt_out_at is None:
            raise ValueError("withdrawn consent requires opt-out timestamp")

    @property
    def destination_digest(self) -> str:
        return sha256(self.normalized_destination.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CommunicationRequest:
    communication_id: str
    tenant_id: str
    company_id: str
    recipient_id: str
    purpose: CommunicationPurpose
    agent_role: str
    capability: str
    provider_connection_id: str
    runtime_idempotency_key: str
    content_sha256: str
    content_flags: frozenset[str]
    personalization_refs: tuple[str, ...]
    context_ref: str | None
    approval_ref: str | None
    bulk_count: int
    created_at: datetime
    expires_at: datetime
    requires_founder_approval: bool = False
    canary_permit_ref: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "communication_id", "tenant_id", "company_id", "recipient_id", "agent_role",
            "capability", "provider_connection_id", "runtime_idempotency_key",
        ):
            _id(getattr(self, name), name)
        if not _HASH.fullmatch(self.content_sha256):
            raise ValueError("content_sha256 must be lowercase SHA-256")
        for value in self.content_flags:
            _id(value, "content flag")
        for value in self.personalization_refs:
            _id(value, "personalization reference")
        if self.context_ref:
            _id(self.context_ref, "context_ref")
        if self.approval_ref:
            _id(self.approval_ref, "approval_ref")
        if self.canary_permit_ref:
            _id(self.canary_permit_ref, "canary_permit_ref")
        if self.bulk_count < 1:
            raise ValueError("bulk_count must be positive")
        _time(self.created_at, "created_at")
        _time(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("communication request must expire after creation")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision_id: str
    tenant_id: str
    company_id: str
    communication_id: str
    job_id: str
    recipient_id: str
    outcome: PolicyOutcome
    reason_code: str
    consent_basis: ConsentState
    suppression_result: str
    approval_result: str
    content_result: str
    rate_limit_reservation_id: str | None
    policy_version: str
    decided_at: datetime

    def __post_init__(self) -> None:
        for name in ("decision_id", "tenant_id", "company_id", "communication_id", "job_id", "recipient_id", "reason_code", "policy_version"):
            _id(getattr(self, name), name)
        if self.rate_limit_reservation_id:
            _id(self.rate_limit_reservation_id, "rate_limit_reservation_id")
        _time(self.decided_at, "decided_at")


@dataclass(frozen=True, slots=True)
class RateLimitReservation:
    reservation_id: str
    tenant_id: str
    company_id: str
    recipient_id: str
    purpose: CommunicationPurpose
    communication_id: str
    idempotency_key: str
    reserved_at: datetime

    def __post_init__(self) -> None:
        for name in ("reservation_id", "tenant_id", "company_id", "recipient_id", "communication_id", "idempotency_key"):
            _id(getattr(self, name), name)
        _time(self.reserved_at, "reserved_at")


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    delivery_id: str
    tenant_id: str
    company_id: str
    communication_id: str
    recipient_id: str
    provider: str
    provider_connection_id: str
    provider_receipt_id: str
    provider_request_id: str
    purpose: CommunicationPurpose
    status: DeliveryStatus
    response_classification: str
    external_object_ref: str | None
    reconciliation_status: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "delivery_id", "tenant_id", "company_id", "communication_id", "recipient_id",
            "provider", "provider_connection_id", "provider_receipt_id", "provider_request_id",
            "reconciliation_status",
        ):
            _id(getattr(self, name), name)
        _time(self.created_at, "created_at")
        _time(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class VerifiedDeliveryEvent:
    event_id: str
    provider: str
    provider_request_id: str
    status: DeliveryStatus
    occurred_at: datetime
    response_classification: str

    def __post_init__(self) -> None:
        for name in ("event_id", "provider", "provider_request_id", "response_classification"):
            _id(getattr(self, name), name)
        _time(self.occurred_at, "occurred_at")


@dataclass(frozen=True, slots=True)
class ContentEvidence:
    """Trusted, server-created evidence used while inspecting ephemeral message content."""

    company_fact_refs: tuple[str, ...] = ()
    pricing_authorized: bool = False
    discount_authorized: bool = False
    availability_authorized: bool = False
    incentive_approved: bool = False
    required_disclosure_present: bool = False
    unsupported_claims: bool = False
    cross_scope_data: bool = False
    privacy_leak: bool = False
    sensitive_or_high_impact: bool = False

    def __post_init__(self) -> None:
        for value in self.company_fact_refs:
            _id(value, "company fact reference")


def normalize_email(value: str) -> str:
    if not isinstance(value, str) or any(c in value for c in "\r\n\0"):
        raise ValueError("email destination is malformed")
    # Case normalization is safe; display-name/address syntax is intentionally rejected.
    return value.strip().lower()
