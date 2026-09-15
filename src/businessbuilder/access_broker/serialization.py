from __future__ import annotations

from .models import (
    ArtifactClassification,
    ArtifactRecord,
    ArtifactStatus,
    CapabilityGrant,
    ConnectionStatus,
    CredentialScope,
    ExternalAccount,
    ExternalCredentialRef,
    ProviderConnection,
    ProviderReceipt,
    ReceiptStatus,
)


def decode_broker_record(kind: str, data: dict):
    if kind.startswith("cleaning_evidence_"):
        from businessbuilder.residential_cleaning.evidence_review import decode_evidence_record
        return decode_evidence_record(kind, data)
    if kind.startswith("live_canary_"):
        from businessbuilder.live_canary.serialization import decode_canary_record
        return decode_canary_record(kind, data)
    if kind.startswith("communication_"):
        from businessbuilder.outbound_communications.serialization import decode_communication_record
        return decode_communication_record(kind, data)
    if kind == "external_account":
        data["status"] = ConnectionStatus(data["status"])
        return ExternalAccount(**data)
    if kind == "provider_connection":
        data["status"] = ConnectionStatus(data["status"])
        data["scopes_requested"] = frozenset(data.get("scopes_requested", ()))
        data["scopes_granted"] = frozenset(data.get("scopes_granted", ()))
        data["provider_metadata"] = tuple(tuple(item) for item in data.get("provider_metadata", ()))
        data["secret_ref_ids"] = tuple(data.get("secret_ref_ids", ()))
        return ProviderConnection(**data)
    if kind == "external_credential_ref":
        data["status"] = ConnectionStatus(data["status"])
        return ExternalCredentialRef(**data)
    if kind == "capability_grant":
        scope = data["scope"]
        data["scope"] = CredentialScope(
            scope["capability"], frozenset(scope["operations"]), frozenset(scope["secret_types"])
        )
        data["artifact_classifications"] = frozenset(
            ArtifactClassification(value) for value in data["artifact_classifications"]
        )
        return CapabilityGrant(**data)
    if kind == "artifact":
        data["classification"] = ArtifactClassification(data["classification"])
        data["status"] = ArtifactStatus(data["status"])
        return ArtifactRecord(**data)
    if kind in {"provider_health", "connection_command"}:
        from businessbuilder.provider_connection.serialization import decode_provider_record
        return decode_provider_record(kind, data)
    raise ValueError("unsupported broker record kind")


def decode_provider_receipt(data: dict) -> ProviderReceipt:
    data["status"] = ReceiptStatus(data["status"])
    return ProviderReceipt(**data)
