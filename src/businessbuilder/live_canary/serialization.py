from __future__ import annotations

from businessbuilder.outbound_communications.models import CommunicationPurpose

from .models import (
    CanaryAlert, CanaryAlertSeverity, CanaryEligibility, CanaryMetric, CanaryPermit,
    CanaryPermitStatus, CanarySendReservation, DeliverabilityHealth, LiveGateCeremony,
    ReadinessState, ReconciliationState, ReconciliationTask, ReputationState,
    SenderIdentityReadiness,
)


def decode_canary_record(kind: str, data: dict):
    if kind == "live_canary_sender":
        data["reputation"] = ReputationState(data["reputation"])
        return SenderIdentityReadiness(**data)
    if kind == "live_canary_deliverability":
        data["reputation_warnings"] = tuple(data.get("reputation_warnings", ()))
        return DeliverabilityHealth(**data)
    if kind == "live_canary_eligibility":
        data["state"] = ReadinessState(data["state"])
        data["gates"] = tuple(tuple(v) for v in data["gates"])
        return CanaryEligibility(**data)
    if kind == "live_canary_ceremony":
        data["checklist"] = tuple(tuple(v) for v in data["checklist"])
        return LiveGateCeremony(**data)
    if kind == "live_canary_permit":
        data["allowed_purposes"] = tuple(CommunicationPurpose(v) for v in data["allowed_purposes"])
        data["recipient_digests"] = tuple(data["recipient_digests"])
        data["recipient_domains"] = tuple(data["recipient_domains"])
        data["status"] = CanaryPermitStatus(data["status"])
        return CanaryPermit(**data)
    if kind == "live_canary_send_reservation":
        return CanarySendReservation(**data)
    if kind == "live_canary_metric":
        return CanaryMetric(**data)
    if kind == "live_canary_alert":
        data["severity"] = CanaryAlertSeverity(data["severity"])
        return CanaryAlert(**data)
    if kind == "live_canary_reconciliation":
        data["state"] = ReconciliationState(data["state"])
        return ReconciliationTask(**data)
    raise ValueError("unsupported live canary record kind")
