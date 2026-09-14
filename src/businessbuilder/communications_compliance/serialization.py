from __future__ import annotations

from businessbuilder.outbound_communications.models import (
    CommunicationPurpose, ConsentState, DestinationType, PolicyOutcome,
)

from .models import (
    AbuseSignal, AbuseSignalType, AlertClass, AlertSeverity, ComplianceDecision,
    ConsentEvidence, ErasureRecord, JurisdictionContext, JurisdictionPolicy,
    KillSwitchRecord, KillSwitchScope, OperationalAlert, PIIClass,
    RetentionClass, RetentionRecord, RolloutPolicy, RolloutTier,
    UnsubscribeTokenRecord,
)


def decode_compliance_record(kind: str, data: dict):
    if kind == "communication_compliance_jurisdiction":
        return JurisdictionContext(**data)
    if kind == "communication_compliance_policy":
        data["channel"] = DestinationType(data["channel"])
        data["purposes"] = tuple(CommunicationPurpose(v) for v in data["purposes"])
        data["allowed_consent"] = tuple(ConsentState(v) for v in data["allowed_consent"])
        data["supported_jurisdictions"] = tuple(data["supported_jurisdictions"])
        data["quiet_hours"] = tuple(data["quiet_hours"]) if data.get("quiet_hours") else None
        return JurisdictionPolicy(**data)
    if kind == "communication_compliance_consent":
        data["purpose"] = CommunicationPurpose(data["purpose"]) if data.get("purpose") else None
        data["channel"] = DestinationType(data["channel"])
        data["consent_basis"] = ConsentState(data["consent_basis"])
        return ConsentEvidence(**data)
    if kind == "communication_compliance_unsubscribe_token":
        return UnsubscribeTokenRecord(**data)
    if kind == "communication_compliance_retention":
        data["pii_classes"] = tuple(PIIClass(v) for v in data["pii_classes"])
        data["retention_class"] = RetentionClass(data["retention_class"])
        return RetentionRecord(**data)
    if kind == "communication_compliance_erasure":
        return ErasureRecord(**data)
    if kind == "communication_compliance_kill_switch":
        data["scope"] = KillSwitchScope(data["scope"])
        return KillSwitchRecord(**data)
    if kind == "communication_compliance_rollout":
        data["tier"] = RolloutTier(data["tier"])
        data["approved_tenants"] = tuple(data["approved_tenants"])
        data["approved_companies"] = tuple(data["approved_companies"])
        data["approved_providers"] = tuple(data["approved_providers"])
        data["approved_purposes"] = tuple(CommunicationPurpose(v) for v in data["approved_purposes"])
        data["approved_recipient_domains"] = tuple(data["approved_recipient_domains"])
        return RolloutPolicy(**data)
    if kind == "communication_compliance_decision":
        data["purpose"] = CommunicationPurpose(data["purpose"])
        data["consent_basis"] = ConsentState(data["consent_basis"])
        data["retention_class"] = RetentionClass(data["retention_class"])
        data["rollout_tier"] = RolloutTier(data["rollout_tier"])
        data["outcome"] = PolicyOutcome(data["outcome"])
        return ComplianceDecision(**data)
    if kind == "communication_compliance_abuse_signal":
        data["signal_type"] = AbuseSignalType(data["signal_type"])
        data["safe_details"] = tuple(tuple(v) for v in data.get("safe_details", ()))
        return AbuseSignal(**data)
    if kind == "communication_compliance_alert":
        data["alert_class"] = AlertClass(data["alert_class"])
        data["severity"] = AlertSeverity(data["severity"])
        return OperationalAlert(**data)
    raise ValueError("unsupported compliance record kind")

