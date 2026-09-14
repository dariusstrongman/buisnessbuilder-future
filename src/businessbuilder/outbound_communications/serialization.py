from __future__ import annotations

from .models import (
    CommunicationPurpose, CommunicationRequest, ConsentState, ContactRelationship,
    DeliveryRecord, DeliveryStatus, DestinationType, PolicyDecision, PolicyOutcome,
    RateLimitReservation, RecipientRecord, SuppressionState,
)


def decode_communication_record(kind: str, data: dict):
    if kind.startswith("communication_compliance_"):
        from businessbuilder.communications_compliance.serialization import decode_compliance_record
        return decode_compliance_record(kind, data)
    if kind == "communication_recipient":
        data["destination_type"] = DestinationType(data["destination_type"])
        data["relationship"] = ContactRelationship(data["relationship"])
        data["consent_state"] = ConsentState(data["consent_state"])
        data["suppression_state"] = SuppressionState(data["suppression_state"])
        data["risk_flags"] = tuple(data.get("risk_flags", ()))
        return RecipientRecord(**data)
    if kind == "communication_request":
        data["purpose"] = CommunicationPurpose(data["purpose"])
        data["content_flags"] = frozenset(data.get("content_flags", ()))
        data["personalization_refs"] = tuple(data.get("personalization_refs", ()))
        return CommunicationRequest(**data)
    if kind == "communication_policy_decision":
        data["outcome"] = PolicyOutcome(data["outcome"])
        data["consent_basis"] = ConsentState(data["consent_basis"])
        return PolicyDecision(**data)
    if kind in {"communication_delivery", "communication_delivery_provider_request"}:
        data["purpose"] = CommunicationPurpose(data["purpose"])
        data["status"] = DeliveryStatus(data["status"])
        return DeliveryRecord(**data)
    if kind == "communication_rate_reservation":
        data["purpose"] = CommunicationPurpose(data["purpose"])
        return RateLimitReservation(**data)
    raise ValueError("unsupported communication record kind")
