from __future__ import annotations

from .models import ConnectionCommand, ConnectionHealthState, OAuthTransaction, ProviderHealth


def decode_oauth_transaction(data: dict) -> OAuthTransaction:
    data["scopes_requested"] = frozenset(data["scopes_requested"])
    return OAuthTransaction(**data)


def decode_provider_record(kind: str, data: dict):
    if kind == "provider_health":
        data["state"] = ConnectionHealthState(data.get("state", "UNKNOWN"))
        return ProviderHealth(**data)
    if kind == "connection_command":
        return ConnectionCommand(**data)
    raise ValueError("unsupported provider connection record kind")
