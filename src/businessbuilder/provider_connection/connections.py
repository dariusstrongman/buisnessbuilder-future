"""Founder-safe connection status. Provider errors and credential refs never cross this boundary."""

from __future__ import annotations

from datetime import datetime

from businessbuilder.access_broker.models import ConnectionStatus, ProviderConnection

from .models import ConnectionHealthState, ProviderHealth, iso


_ERROR_STATES = {
    "invalid_grant": ConnectionHealthState.AUTH_EXPIRED,
    "refresh_invalid_grant": ConnectionHealthState.AUTH_EXPIRED,
    "refresh_token_revoked": ConnectionHealthState.AUTH_EXPIRED,
    "token_revoked": ConnectionHealthState.AUTH_EXPIRED,
    "scope_missing": ConnectionHealthState.PERMISSION_ERROR,
    "permission_denied": ConnectionHealthState.PERMISSION_ERROR,
    "rate_limit": ConnectionHealthState.RATE_LIMITED,
    "quota_exceeded": ConnectionHealthState.BILLING_OR_CREDITS,
    "insufficient_credits": ConnectionHealthState.BILLING_OR_CREDITS,
    "billing_required": ConnectionHealthState.BILLING_OR_CREDITS,
    "provider_unavailable": ConnectionHealthState.PROVIDER_OUTAGE,
    "provider_5xx": ConnectionHealthState.PROVIDER_OUTAGE,
    "customer_disconnect": ConnectionHealthState.DISCONNECTED,
    "reconnect_required": ConnectionHealthState.ACTION_REQUIRED,
}

_MESSAGES = {
    ConnectionHealthState.HEALTHY: "This connection is working.",
    ConnectionHealthState.WARNING: "This connection may need attention soon.",
    ConnectionHealthState.ACTION_REQUIRED: "This connection needs your attention before Business Builder can use it.",
    ConnectionHealthState.DISCONNECTED: "Business Builder no longer has access to this account.",
    ConnectionHealthState.AUTH_EXPIRED: "Your authorization expired. Reconnect this account to restore access.",
    ConnectionHealthState.PERMISSION_ERROR: "Permissions changed. Reconnect and approve the required access.",
    ConnectionHealthState.RATE_LIMITED: "This provider is limiting requests. Business Builder will wait before retrying.",
    ConnectionHealthState.BILLING_OR_CREDITS: "This provider's billing or credits need attention before work can continue.",
    ConnectionHealthState.PROVIDER_OUTAGE: "This provider is unavailable right now. Business Builder will retry when it recovers.",
    ConnectionHealthState.UNKNOWN: "Connection health has not been verified yet.",
}


def normalize_health(error_code: str | None, *, usable: bool) -> ConnectionHealthState:
    if error_code:
        return _ERROR_STATES.get(error_code.strip().lower(), ConnectionHealthState.ACTION_REQUIRED)
    return ConnectionHealthState.HEALTHY if usable else ConnectionHealthState.UNKNOWN


def connection_state(connection: ProviderConnection, health: ProviderHealth | None,
                     *, at: datetime) -> ConnectionHealthState:
    if connection.status in {ConnectionStatus.DISCONNECTED, ConnectionStatus.REVOKED}:
        return ConnectionHealthState.DISCONNECTED
    if connection.status in {ConnectionStatus.RECONNECT_REQUIRED, ConnectionStatus.COMPROMISED}:
        return ConnectionHealthState.ACTION_REQUIRED
    # expires_at is the rotating OAuth access-token expiry; a valid refresh credential
    # may recover it. Only an explicitly expired/revoked connection is unusable.
    if connection.status is ConnectionStatus.EXPIRED:
        return ConnectionHealthState.AUTH_EXPIRED
    if connection.status is not ConnectionStatus.ACTIVE:
        return ConnectionHealthState.UNKNOWN
    if health is None:
        return ConnectionHealthState.UNKNOWN
    if health.rate_limited_until and health.rate_limited_until > at:
        return ConnectionHealthState.RATE_LIMITED
    if not health.usable and health.state in {ConnectionHealthState.HEALTHY, ConnectionHealthState.WARNING}:
        return ConnectionHealthState.UNKNOWN
    return health.state


def safe_connection(connection: ProviderConnection, health: ProviderHealth | None,
                    *, at: datetime) -> dict:
    state = connection_state(connection, health, at=at)
    provider_name = connection.provider.replace("_", " ").title()
    message = _MESSAGES[state]
    if state is ConnectionHealthState.WARNING and health and health.quota_remaining is not None:
        message = f"{provider_name} {health.quota_unit or 'capacity'} is running low. Add funds or capacity to avoid interruption."
    return {
        "connection_id": connection.connection_id,
        "provider": connection.provider,
        "capability": connection.capability,
        "connected_account": connection.provider_account_id if connection.status is ConnectionStatus.ACTIVE else None,
        "account_ownership": connection.account_ownership,
        "auth_method": connection.auth_method,
        "scopes_granted": sorted(connection.scopes_granted),
        "status": connection.status.value,
        "health": state.value,
        "message": message,
        "connected_at": iso(connection.connected_at) if connection.connected_at else None,
        "last_healthy_check": iso(health.last_successful_check_at) if health and health.last_successful_check_at else None,
        "last_failure": iso(health.last_failure_at) if health and health.last_failure_at else None,
        "action_required": health.action_required if health and state not in {ConnectionHealthState.HEALTHY, ConnectionHealthState.WARNING} else None,
        "reconnect_available": state in {ConnectionHealthState.ACTION_REQUIRED, ConnectionHealthState.AUTH_EXPIRED, ConnectionHealthState.PERMISSION_ERROR, ConnectionHealthState.DISCONNECTED},
        "disconnect_available": connection.status is ConnectionStatus.ACTIVE,
        "quota": ({"remaining": health.quota_remaining, "unit": health.quota_unit,
                   "checked_at": iso(health.quota_checked_at) if health.quota_checked_at else None}
                  if health and health.quota_remaining is not None else None),
    }
