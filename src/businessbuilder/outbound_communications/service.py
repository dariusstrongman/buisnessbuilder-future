from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
import re
from typing import Callable

from businessbuilder.access_broker.models import ConnectionStatus, ProviderReceipt, ReceiptStatus, stable_id
from businessbuilder.agent_runtime.models import AgentJobEnvelope
from businessbuilder.agent_runtime.service import AgentRuntimeService
from businessbuilder.identity import (
    AuthenticatedPrincipal, AuthorizationContext, AuthorizationPolicy, Permission,
    PrincipalContextAuthority,
)
from businessbuilder.runtime.storage import RuntimeRepository
from businessbuilder.company_brain import Scope

from .models import (
    CommunicationPurpose, CommunicationRequest, ConsentState, ContactRelationship,
    ContentEvidence, DeliveryRecord, DeliveryStatus, DestinationType, PolicyDecision,
    PolicyOutcome, RateLimitReservation, RecipientRecord, SuppressionState,
    VerifiedDeliveryEvent, normalize_email,
)


class CommunicationDenied(PermissionError):
    pass


POLICY_VERSION = "outbound-email.v1"

_PURPOSE_OPERATIONS: dict[CommunicationPurpose, frozenset[str]] = {
    CommunicationPurpose.REPLY_TO_INBOUND: frozenset({"send_preapproved_reply"}),
    CommunicationPurpose.QUOTE_RESPONSE: frozenset({"send_preapproved_reply"}),
    CommunicationPurpose.REVIEW_REQUEST: frozenset({"send_preapproved_review_request"}),
    CommunicationPurpose.APPOINTMENT_CONFIRMATION: frozenset({"send_preapproved_reply"}),
    CommunicationPurpose.SERVICE_FOLLOWUP: frozenset({"send_preapproved_reply"}),
    CommunicationPurpose.OPERATIONAL_NOTICE: frozenset({"send_preapproved_reply"}),
    CommunicationPurpose.MARKETING: frozenset({"send_preapproved_reply"}),
    CommunicationPurpose.RE_ENGAGEMENT: frozenset({"send_preapproved_reply"}),
}

_CONSENT: dict[CommunicationPurpose, frozenset[ConsentState]] = {
    CommunicationPurpose.REPLY_TO_INBOUND: frozenset({ConsentState.CUSTOMER_INITIATED, ConsentState.EXPLICIT, ConsentState.TRANSACTIONAL_RELATIONSHIP}),
    CommunicationPurpose.QUOTE_RESPONSE: frozenset({ConsentState.CUSTOMER_INITIATED, ConsentState.EXPLICIT, ConsentState.TRANSACTIONAL_RELATIONSHIP}),
    CommunicationPurpose.APPOINTMENT_CONFIRMATION: frozenset({ConsentState.CUSTOMER_INITIATED, ConsentState.EXPLICIT, ConsentState.TRANSACTIONAL_RELATIONSHIP}),
    CommunicationPurpose.SERVICE_FOLLOWUP: frozenset({ConsentState.SERVICE_FOLLOWUP, ConsentState.EXPLICIT, ConsentState.TRANSACTIONAL_RELATIONSHIP}),
    CommunicationPurpose.REVIEW_REQUEST: frozenset({ConsentState.SERVICE_FOLLOWUP, ConsentState.EXPLICIT, ConsentState.TRANSACTIONAL_RELATIONSHIP}),
    CommunicationPurpose.OPERATIONAL_NOTICE: frozenset({ConsentState.EXPLICIT, ConsentState.TRANSACTIONAL_RELATIONSHIP}),
    CommunicationPurpose.MARKETING: frozenset({ConsentState.EXPLICIT}),
    CommunicationPurpose.RE_ENGAGEMENT: frozenset({ConsentState.EXPLICIT}),
}

_RELATIONSHIPS: dict[CommunicationPurpose, frozenset[ContactRelationship]] = {
    CommunicationPurpose.REPLY_TO_INBOUND: frozenset({ContactRelationship.INBOUND_CUSTOMER, ContactRelationship.CUSTOMER, ContactRelationship.QUOTE_REQUESTER}),
    CommunicationPurpose.QUOTE_RESPONSE: frozenset({ContactRelationship.INBOUND_CUSTOMER, ContactRelationship.QUOTE_REQUESTER, ContactRelationship.LEAD}),
    CommunicationPurpose.REVIEW_REQUEST: frozenset({ContactRelationship.CUSTOMER, ContactRelationship.SERVICE_RECIPIENT}),
}

_CONTEXT_EVENTS: dict[CommunicationPurpose, frozenset[str]] = {
    CommunicationPurpose.REPLY_TO_INBOUND: frozenset({"integration.message.received"}),
    CommunicationPurpose.QUOTE_RESPONSE: frozenset({"integration.quote.requested", "integration.message.received"}),
    CommunicationPurpose.REVIEW_REQUEST: frozenset({"service.completed"}),
}

_APPROVAL_REQUIRED = frozenset({
    CommunicationPurpose.MARKETING, CommunicationPurpose.RE_ENGAGEMENT,
})

_DELIVERY_TRANSITIONS: dict[DeliveryStatus, frozenset[DeliveryStatus]] = {
    DeliveryStatus.QUEUED: frozenset({DeliveryStatus.ACCEPTED, DeliveryStatus.DEFERRED,
                                      DeliveryStatus.REJECTED, DeliveryStatus.UNKNOWN}),
    DeliveryStatus.ACCEPTED: frozenset({DeliveryStatus.DELIVERED, DeliveryStatus.DEFERRED,
                                        DeliveryStatus.BOUNCED, DeliveryStatus.REJECTED,
                                        DeliveryStatus.COMPLAINED, DeliveryStatus.UNKNOWN}),
    DeliveryStatus.DEFERRED: frozenset({DeliveryStatus.ACCEPTED, DeliveryStatus.DELIVERED,
                                        DeliveryStatus.BOUNCED, DeliveryStatus.REJECTED,
                                        DeliveryStatus.COMPLAINED, DeliveryStatus.UNKNOWN}),
    DeliveryStatus.DELIVERED: frozenset({DeliveryStatus.COMPLAINED}),
    DeliveryStatus.UNKNOWN: frozenset(DeliveryStatus),
    DeliveryStatus.BOUNCED: frozenset(),
    DeliveryStatus.REJECTED: frozenset(),
    DeliveryStatus.COMPLAINED: frozenset(),
}

# Company limits are deliberately conservative while all sending remains sandbox-only.
_LIMITS: dict[CommunicationPurpose, dict[str, int]] = {
    purpose: {
        "company_minute": 20, "company_hour": 100, "company_day": 500,
        "recipient_minute": 2, "recipient_hour": 6, "recipient_day": 12,
        "burst": 2,
    }
    for purpose in CommunicationPurpose
}
_LIMITS[CommunicationPurpose.REVIEW_REQUEST] = {
    "company_minute": 10, "company_hour": 50, "company_day": 200,
    "recipient_minute": 1, "recipient_hour": 1, "recipient_day": 1, "burst": 1,
}
_LIMITS[CommunicationPurpose.MARKETING] = {
    "company_minute": 1, "company_hour": 5, "company_day": 10,
    "recipient_minute": 1, "recipient_hour": 1, "recipient_day": 1, "burst": 1,
}
_LIMITS[CommunicationPurpose.RE_ENGAGEMENT] = dict(_LIMITS[CommunicationPurpose.MARKETING])

_SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk_live_[A-Za-z0-9]{16,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_ -]?key|password|refresh[_ -]?token)\s*[:=]\s*\S+"),
)


class OutboundCommunicationSafety:
    """Execution-time policy gate below Runtime and above every provider send."""

    def __init__(
        self,
        *,
        repository: RuntimeRepository,
        agent_runtime: AgentRuntimeService,
        principal_authority: PrincipalContextAuthority,
        authorization: AuthorizationPolicy,
        audit,
        clock: Callable[[], datetime],
        id_factory: Callable[[str], str],
        delivery_verifiers: dict[str, object] | None = None,
        limits: dict[CommunicationPurpose, dict[str, int]] | None = None,
        company_brain=None,
    ) -> None:
        self.repository = repository
        self.agent_runtime = agent_runtime
        self.principal_authority = principal_authority
        self.authorization = authorization
        self.audit = audit
        self.clock = clock
        self.id_factory = id_factory
        self.delivery_verifiers = delivery_verifiers or {}
        self.limits = limits or _LIMITS
        self.company_brain = company_brain

    def register_from_canonical_event(
        self,
        *,
        tenant_id: str,
        company_id: str,
        destination: str,
        relationship: ContactRelationship,
        consent_state: ConsentState,
        consent_provenance: str,
        source_event_ref: str,
        recipient_id: str | None = None,
    ) -> RecipientRecord:
        events = self.repository.list_events(tenant_id, company_id)
        if not any(item["event_id"] == source_event_ref for item in events):
            raise CommunicationDenied("recipient source is not a canonical event in scope")
        now = self.clock()
        normalized = normalize_email(destination)
        rid = recipient_id or stable_id("recipient", tenant_id, company_id, normalized)
        current = self.repository.get_broker_record("communication_recipient", tenant_id, company_id, rid)
        if current and current.normalized_destination != normalized:
            raise CommunicationDenied("recipient identifier is already bound to another destination")
        recipient = RecipientRecord(
            rid, tenant_id, company_id, normalized, DestinationType.EMAIL,
            source_event_ref, relationship, consent_state, consent_provenance,
            now, SuppressionState.CLEAR, None, None,
            current.last_contacted_at if current else None,
            current.risk_flags if current else (), current.created_at if current else now, now,
        )
        self.repository.save_broker_record("communication_recipient", rid, tenant_id, company_id, recipient)
        self._audit(tenant_id, company_id, "canonical_event", "communication.recipient.registered",
                    "recipient", rid, "canonical recipient context registered",
                    {"source_event_ref": source_event_ref, "relationship": relationship.value,
                     "consent_basis": consent_state.value, "destination_type": "email"})
        return recipient

    def prepare_request(
        self,
        *,
        tenant_id: str,
        company_id: str,
        recipient_id: str,
        purpose: CommunicationPurpose,
        agent_role: str,
        capability: str,
        provider_connection_id: str,
        runtime_idempotency_key: str,
        content: str,
        evidence: ContentEvidence = ContentEvidence(),
        context_ref: str | None = None,
        approval_ref: str | None = None,
        bulk_count: int = 1,
        expires_in: timedelta = timedelta(minutes=15),
    ) -> CommunicationRequest:
        if capability != "communications.email" or not isinstance(content, str) or not content or len(content.encode()) > 64_000:
            raise ValueError("communication content is invalid")
        if expires_in <= timedelta(0) or expires_in > timedelta(hours=24):
            raise ValueError("communication request expiry must be within 24 hours")
        if purpose in _CONTEXT_EVENTS:
            event = next((item for item in self.repository.list_events(tenant_id, company_id)
                          if item["event_id"] == context_ref), None)
            if event is None or event["type"] not in _CONTEXT_EVENTS[purpose]:
                raise CommunicationDenied("required canonical communication context is missing")
        if self.repository.get_broker_record("communication_recipient", tenant_id, company_id, recipient_id) is None:
            raise CommunicationDenied("recipient is outside tenant/company scope")
        connection = self.repository.get_broker_record(
            "provider_connection", tenant_id, company_id, provider_connection_id
        )
        if connection is None:
            raise CommunicationDenied("provider connection is outside tenant/company scope")
        if evidence.company_fact_refs:
            if self.company_brain is None:
                raise CommunicationDenied("Company Brain fact authority is unavailable")
            current = {item.record_id: item for item in self.company_brain.query_current_state(
                Scope(tenant_id, company_id))}
            if any(ref not in current or current[ref].lifecycle != "active"
                   for ref in evidence.company_fact_refs):
                raise CommunicationDenied("personalization fact is not current Company Brain truth")
        now = self.clock()
        flags = self._inspect_content(content, purpose, evidence)
        communication_id = stable_id("communication", tenant_id, company_id, runtime_idempotency_key)
        value = CommunicationRequest(
            communication_id, tenant_id, company_id, recipient_id, purpose, agent_role,
            capability, provider_connection_id, runtime_idempotency_key,
            sha256(content.encode()).hexdigest(), flags, evidence.company_fact_refs,
            context_ref, approval_ref, bulk_count, now, now + expires_in,
            evidence.sensitive_or_high_impact,
        )
        self.repository.save_broker_record("communication_request", communication_id,
                                           tenant_id, company_id, value)
        return value

    def authorize(
        self,
        envelope: AgentJobEnvelope,
        *,
        communication_ref: str,
        provider_connection_id: str,
        operation: str,
    ) -> PolicyDecision:
        communication = self.repository.get_broker_record(
            "communication_request", envelope.tenant_id, envelope.company_id, communication_ref
        )
        if communication is None:
            raise CommunicationDenied("communication request is outside Runtime scope")
        try:
            self.agent_runtime.revalidate(envelope)
            self._authorize_loaded(envelope, communication, provider_connection_id, operation)
            recipient = self.repository.get_broker_record(
                "communication_recipient", envelope.tenant_id, envelope.company_id,
                communication.recipient_id,
            )
            if recipient is None:
                raise CommunicationDenied("recipient is outside Runtime scope")
            self._recipient_policy(communication, recipient)
            self._provider_health(communication)
            reservation = RateLimitReservation(
                stable_id("send_reservation", communication.communication_id),
                envelope.tenant_id, envelope.company_id, recipient.recipient_id,
                communication.purpose, communication.communication_id,
                stable_id("send", envelope.idempotency_key, communication.communication_id),
                self.clock(),
            )
            claimed, allowed, reason = self.repository.reserve_communication_send(
                reservation, self.limits[communication.purpose]
            )
            if not allowed and reason != "duplicate":
                raise CommunicationDenied(reason)
            decision = self._decision(envelope, communication, recipient, PolicyOutcome.ALLOWED,
                                      "policy_allowed", claimed.reservation_id)
            self._save_decision(decision)
            self._audit_decision(decision)
            return decision
        except Exception as exc:
            reason = self._reason(exc)
            recipient = self.repository.get_broker_record(
                "communication_recipient", envelope.tenant_id, envelope.company_id,
                communication.recipient_id,
            )
            decision = self._decision(envelope, communication, recipient, PolicyOutcome.DENIED,
                                      reason, None)
            self._save_decision(decision)
            self._audit_decision(decision)
            if isinstance(exc, CommunicationDenied):
                raise
            raise CommunicationDenied("outbound communication denied") from exc

    def _authorize_loaded(self, envelope, communication, connection_id, operation) -> None:
        if (
            communication.tenant_id != envelope.tenant_id
            or communication.company_id != envelope.company_id
            or communication.runtime_idempotency_key != envelope.idempotency_key
            or communication.agent_role != envelope.agent_role
            or communication.capability != envelope.capability
            or communication.provider_connection_id != connection_id
        ):
            raise CommunicationDenied("communication authority does not match Runtime job")
        if self.clock() >= communication.expires_at:
            raise CommunicationDenied("communication request expired")
        if operation not in _PURPOSE_OPERATIONS[communication.purpose]:
            raise CommunicationDenied("provider operation is not allowed for communication purpose")
        if communication.bulk_count != 1:
            raise CommunicationDenied("bulk communication is disabled")
        if communication.content_flags:
            raise CommunicationDenied("content policy denied the communication")
        if communication.purpose in _APPROVAL_REQUIRED or communication.requires_founder_approval:
            if not communication.approval_ref or communication.approval_ref not in envelope.approval_refs:
                raise CommunicationDenied("founder approval is required")

    def _recipient_policy(self, communication, recipient) -> None:
        if recipient.destination_type is not DestinationType.EMAIL:
            raise CommunicationDenied("only email is enabled")
        if recipient.suppression_state is SuppressionState.SUPPRESSED:
            raise CommunicationDenied("recipient is suppressed")
        if recipient.opt_out_at is not None or recipient.consent_state in {ConsentState.WITHDRAWN, ConsentState.PROHIBITED}:
            raise CommunicationDenied("recipient opted out or is prohibited")
        if recipient.consent_state not in _CONSENT[communication.purpose]:
            raise CommunicationDenied("communication purpose lacks required consent or context")
        relationships = _RELATIONSHIPS.get(communication.purpose)
        if relationships is not None and recipient.relationship not in relationships:
            raise CommunicationDenied("recipient relationship does not support communication purpose")
        if recipient.risk_flags:
            raise CommunicationDenied("recipient risk state requires review")

    def _provider_health(self, communication) -> None:
        connection = self.repository.get_broker_record(
            "provider_connection", communication.tenant_id, communication.company_id,
            communication.provider_connection_id,
        )
        now = self.clock()
        if connection is None or connection.status is not ConnectionStatus.ACTIVE:
            raise CommunicationDenied("provider connection is inactive")
        health = self.repository.get_broker_record(
            "provider_health", communication.tenant_id, communication.company_id,
            communication.provider_connection_id,
        )
        if health is not None and (
            not health.usable or (health.rate_limited_until and health.rate_limited_until > now)
        ):
            raise CommunicationDenied("provider is not currently usable")

    def opt_out(self, principal: AuthenticatedPrincipal, *, tenant_id: str, company_id: str,
                recipient_id: str, event_id: str, reason: str = "customer_request") -> RecipientRecord:
        verified = self._manage(principal, tenant_id, company_id)
        return self._suppress(tenant_id, company_id, recipient_id, event_id=event_id,
                              reason=reason, actor_id=verified.user_id, withdrawn=True)

    def explicitly_reenable(self, principal: AuthenticatedPrincipal, *, tenant_id: str,
                            company_id: str, recipient_id: str,
                            consent_provenance: str) -> RecipientRecord:
        verified = self._manage(principal, tenant_id, company_id)
        current = self._recipient(tenant_id, company_id, recipient_id)
        now = self.clock()
        changed = replace(current, consent_state=ConsentState.EXPLICIT,
                          consent_provenance=consent_provenance, consent_at=now,
                          suppression_state=SuppressionState.CLEAR,
                          suppression_reason=None, opt_out_at=None, updated_at=now)
        self.repository.save_broker_record("communication_recipient", recipient_id,
                                           tenant_id, company_id, changed)
        self._audit(tenant_id, company_id, verified.user_id, "communication.recipient.reenabled",
                    "recipient", recipient_id, "explicit authorized re-enable",
                    {"consent_basis": "explicit", "consent_provenance": consent_provenance})
        return changed

    def _suppress(self, tenant_id, company_id, recipient_id, *, event_id, reason,
                  actor_id, withdrawn=False):
        current = self._recipient(tenant_id, company_id, recipient_id)
        if not self.repository.claim_communication_event("suppression", event_id):
            return current
        now = self.clock()
        changed = replace(
            current, suppression_state=SuppressionState.SUPPRESSED,
            suppression_reason=reason[:120],
            consent_state=ConsentState.WITHDRAWN if withdrawn else current.consent_state,
            opt_out_at=now if withdrawn else current.opt_out_at, updated_at=now,
        )
        self.repository.save_broker_record("communication_recipient", recipient_id,
                                           tenant_id, company_id, changed)
        self._audit(tenant_id, company_id, actor_id,
                    "communication.opt_out.recorded" if withdrawn else "communication.suppression.created",
                    "recipient", recipient_id, reason[:120],
                    {"suppression_state": "suppressed", "source_event_id": event_id})
        return changed

    def record_provider_result(self, envelope: AgentJobEnvelope, decision: PolicyDecision,
                               receipt: ProviderReceipt) -> DeliveryRecord:
        if (receipt.tenant_id, receipt.company_id, receipt.job_id) != (
            envelope.tenant_id, envelope.company_id, envelope.job_id
        ):
            raise CommunicationDenied("provider receipt is outside communication scope")
        current = self.repository.get_broker_record(
            "communication_delivery", envelope.tenant_id, envelope.company_id,
            stable_id("delivery", decision.communication_id),
        )
        if current:
            return current
        now = self.clock()
        status = DeliveryStatus.ACCEPTED if receipt.status is ReceiptStatus.SUCCEEDED else DeliveryStatus.REJECTED
        delivery = DeliveryRecord(
            stable_id("delivery", decision.communication_id), envelope.tenant_id,
            envelope.company_id, decision.communication_id, decision.recipient_id,
            receipt.provider, receipt.connection_id or "connection_unknown",
            receipt.receipt_id, receipt.provider_request_id, self._request(decision).purpose,
            status, receipt.response_classification or "unknown", receipt.external_object_ref,
            "provider_receipt", now, now,
        )
        self.repository.save_broker_record("communication_delivery", delivery.delivery_id,
                                           delivery.tenant_id, delivery.company_id, delivery)
        self.repository.save_broker_record(
            "communication_delivery_provider_request",
            stable_id("delivery_lookup", delivery.provider, delivery.provider_request_id),
            delivery.tenant_id, delivery.company_id, delivery,
        )
        recipient = self._recipient(delivery.tenant_id, delivery.company_id, delivery.recipient_id)
        self.repository.save_broker_record(
            "communication_recipient", recipient.recipient_id, recipient.tenant_id,
            recipient.company_id, replace(recipient, last_contacted_at=now, updated_at=now),
        )
        self._audit(delivery.tenant_id, delivery.company_id, "outbound_safety",
                    "communication.delivery.recorded", "delivery", delivery.delivery_id,
                    "safe provider delivery state recorded",
                    {"communication_id": delivery.communication_id, "recipient_id": delivery.recipient_id,
                     "provider_receipt_id": receipt.receipt_id, "status": status.value})
        return delivery

    def handle_delivery_callback(self, *, provider_name: str, body: bytes,
                                 signature: str) -> DeliveryRecord:
        verifier = self.delivery_verifiers.get(provider_name)
        if verifier is None or not hasattr(verifier, "verify_delivery_callback"):
            raise CommunicationDenied("delivery callback verifier unavailable")
        event: VerifiedDeliveryEvent = verifier.verify_delivery_callback(body=body, signature=signature)
        if event.provider != provider_name:
            raise CommunicationDenied("delivery callback provider mismatch")
        delivery = self._delivery_by_request(provider_name, event.provider_request_id)
        if delivery is None:
            raise CommunicationDenied("delivery callback does not match a known send")
        if not self.repository.claim_communication_event(provider_name, event.event_id):
            return delivery
        if (event.status is delivery.status or event.occurred_at < delivery.updated_at
                or event.status not in _DELIVERY_TRANSITIONS[delivery.status]):
            self._audit(delivery.tenant_id, delivery.company_id, provider_name,
                        "communication.delivery.stale_ignored", "delivery",
                        delivery.delivery_id, "stale or invalid delivery transition ignored",
                        {"recipient_id": delivery.recipient_id,
                         "provider_event_id": event.event_id,
                         "current_status": delivery.status.value,
                         "requested_status": event.status.value})
            return delivery
        changed = replace(delivery, status=event.status,
                          response_classification=event.response_classification,
                          reconciliation_status="callback_verified", updated_at=event.occurred_at)
        self.repository.save_broker_record("communication_delivery", changed.delivery_id,
                                           changed.tenant_id, changed.company_id, changed)
        self.repository.save_broker_record(
            "communication_delivery_provider_request",
            stable_id("delivery_lookup", changed.provider, changed.provider_request_id),
            changed.tenant_id, changed.company_id, changed,
        )
        if event.status in {DeliveryStatus.BOUNCED, DeliveryStatus.COMPLAINED}:
            self._suppress(changed.tenant_id, changed.company_id, changed.recipient_id,
                           event_id=f"suppression_{event.event_id}",
                           reason="hard_bounce" if event.status is DeliveryStatus.BOUNCED else "complaint",
                           actor_id=provider_name)
        self._audit(changed.tenant_id, changed.company_id, provider_name,
                    "communication.delivery.reconciled", "delivery", changed.delivery_id,
                    "authenticated provider delivery update",
                    {"recipient_id": changed.recipient_id, "status": changed.status.value,
                     "provider_event_id": event.event_id})
        return changed

    def get_recipient(self, principal, *, tenant_id, company_id, recipient_id):
        self._view(principal, tenant_id, company_id)
        return self._recipient(tenant_id, company_id, recipient_id)

    def list_deliveries(self, principal, *, tenant_id, company_id):
        self._view(principal, tenant_id, company_id)
        return self.repository.list_broker_records("communication_delivery", tenant_id, company_id)

    def get_delivery(self, principal, *, tenant_id, company_id, delivery_id):
        self._view(principal, tenant_id, company_id)
        value = self.repository.get_broker_record("communication_delivery", tenant_id, company_id, delivery_id)
        if value is None:
            raise LookupError("delivery not found")
        return value

    def policy_status(self, principal, *, tenant_id, company_id):
        self._view(principal, tenant_id, company_id)
        return {"version": POLICY_VERSION, "channel": "email", "sandbox_only": True,
                "bulk_enabled": False, "purposes": tuple(item.value for item in CommunicationPurpose)}

    def _manage(self, principal, tenant_id, company_id):
        verified = self.principal_authority.verify(principal, tenant_id=tenant_id, company_id=company_id)
        self.authorization.require(AuthorizationContext(
            verified.user_id, tenant_id, company_id,
            verified.support_impersonation_session_id),
                                   Permission.MANAGE_COMMUNICATIONS, at=self.clock())
        return verified

    def _view(self, principal, tenant_id, company_id):
        verified = self.principal_authority.verify(principal, tenant_id=tenant_id, company_id=company_id)
        self.authorization.require(AuthorizationContext(
            verified.user_id, tenant_id, company_id,
            verified.support_impersonation_session_id),
                                   Permission.VIEW_COMPANY_STATE, at=self.clock())
        return verified

    def _request(self, decision):
        value = self.repository.get_broker_record("communication_request", decision.tenant_id,
                                                  decision.company_id, decision.communication_id)
        if value is None:
            raise LookupError("communication request not found")
        return value

    def _recipient(self, tenant_id, company_id, recipient_id):
        value = self.repository.get_broker_record("communication_recipient", tenant_id, company_id, recipient_id)
        if value is None:
            raise LookupError("recipient not found")
        return value

    def _delivery_by_request(self, provider, provider_request_id):
        # Provider request IDs are globally opaque; lookup never returns recipient content.
        value = self.repository.get_broker_record_by_id(
            "communication_delivery_provider_request", stable_id("delivery_lookup", provider, provider_request_id)
        )
        if value:
            return self.repository.get_broker_record("communication_delivery", value.tenant_id,
                                                     value.company_id, value.delivery_id)
        return None

    def _decision(self, envelope, request, recipient, outcome, reason, reservation_id):
        consent = recipient.consent_state if recipient else ConsentState.UNKNOWN
        suppressed = "suppressed" if recipient and recipient.suppression_state is SuppressionState.SUPPRESSED else "clear"
        approval = "not_required"
        if request.purpose in _APPROVAL_REQUIRED or request.requires_founder_approval:
            approval = "granted" if request.approval_ref in envelope.approval_refs else "missing"
        return PolicyDecision(
            stable_id("communication_decision", request.communication_id, envelope.job_id),
            envelope.tenant_id, envelope.company_id, request.communication_id,
            envelope.job_id, request.recipient_id, outcome, reason, consent, suppressed,
            approval, "passed" if not request.content_flags else "denied",
            reservation_id, POLICY_VERSION, self.clock(),
        )

    def _save_decision(self, decision):
        self.repository.save_broker_record("communication_policy_decision", decision.decision_id,
                                           decision.tenant_id, decision.company_id, decision)

    def _audit_decision(self, decision):
        self._audit(decision.tenant_id, decision.company_id, "outbound_safety",
                    "communication.policy.allowed" if decision.outcome is PolicyOutcome.ALLOWED else "communication.policy.denied",
                    "communication", decision.communication_id, decision.reason_code,
                    {"job_id": decision.job_id, "recipient_id": decision.recipient_id,
                     "consent_basis": decision.consent_basis.value,
                     "suppression_result": decision.suppression_result,
                     "approval_result": decision.approval_result,
                     "content_result": decision.content_result,
                     "rate_limit_reservation_id": decision.rate_limit_reservation_id,
                     "policy_version": decision.policy_version})

    @staticmethod
    def _reason(exc: Exception) -> str:
        text = str(exc).lower()
        mapping = (
            ("bulk", "bulk_disabled"), ("suppressed", "recipient_suppressed"),
            ("opted out", "recipient_opted_out"), ("consent", "consent_missing"),
            ("relationship", "relationship_invalid"), ("rate", "rate_limit_exceeded"),
            ("approval", "approval_missing"), ("content", "content_policy_denied"),
            ("expired", "request_expired"), ("provider", "provider_unavailable"),
            ("scope", "scope_mismatch"), ("authority", "authority_mismatch"),
        )
        return next((code for marker, code in mapping if marker in text), "default_deny")

    @staticmethod
    def _inspect_content(content: str, purpose: CommunicationPurpose,
                         evidence: ContentEvidence) -> frozenset[str]:
        lowered = content.lower()
        flags: set[str] = set()
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS): flags.add("secret_detected")
        if evidence.unsupported_claims: flags.add("fabricated_claim")
        if evidence.cross_scope_data: flags.add("cross_scope_data")
        if evidence.privacy_leak: flags.add("privacy_leak")
        if re.search(r"\b(?:guaranteed|100% guaranteed|risk[- ]free)\b", lowered): flags.add("unsupported_guarantee")
        if re.search(r"\b(?:act now|last chance|immediately or|expires in minutes)\b", lowered): flags.add("false_urgency")
        if ("% off" in lowered or "discount" in lowered) and not (evidence.discount_authorized and evidence.company_fact_refs): flags.add("unauthorized_discount")
        if re.search(r"[$€£]\s*\d", content) and not (evidence.pricing_authorized and evidence.company_fact_refs): flags.add("invented_pricing")
        if re.search(r"\b(?:available today|same-day|immediate availability)\b", lowered) and not (evidence.availability_authorized and evidence.company_fact_refs): flags.add("unsupported_availability")
        if purpose is CommunicationPurpose.REVIEW_REQUEST and re.search(r"\b(?:gift|reward|incentive|coupon)\b", lowered) and not (evidence.incentive_approved and evidence.company_fact_refs): flags.add("review_incentive")
        if re.search(r"\{\{[^}]+\}\}|\{[A-Za-z_][A-Za-z0-9_]*\}", content): flags.add("malformed_template")
        if any(word in lowered for word in ("idiot", "stupid customer", "shut up")): flags.add("abusive_content")
        if purpose in {CommunicationPurpose.MARKETING, CommunicationPurpose.RE_ENGAGEMENT} and not evidence.required_disclosure_present:
            flags.add("missing_opt_out_disclosure")
        return frozenset(flags)

    def _audit(self, tenant_id, company_id, actor_id, action, target_type, target_id, reason, details):
        self.audit.record(
            tenant_id=tenant_id, company_id=company_id, actor_type="system",
            actor_id=actor_id, action=action, target_type=target_type,
            target_id=target_id, correlation_id=details.get("job_id", self.id_factory("correlation")),
            reason=reason, source="outbound_communications_safety",
            details=details, after={"target_id": target_id, "action": action},
        )
