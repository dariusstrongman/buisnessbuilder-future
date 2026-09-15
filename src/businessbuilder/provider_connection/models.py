from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ProviderFailureClass(StrEnum):
    RETRYABLE = "retryable"
    RECONNECT_REQUIRED = "requires_reconnect"
    PERMANENTLY_DENIED = "permanently_denied"
    PROVIDER_UNHEALTHY = "provider_unhealthy"


class ConnectionHealthState(StrEnum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    DISCONNECTED = "DISCONNECTED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    PERMISSION_ERROR = "PERMISSION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    BILLING_OR_CREDITS = "BILLING_OR_CREDITS"
    PROVIDER_OUTAGE = "PROVIDER_OUTAGE"
    UNKNOWN = "UNKNOWN"


def classify_provider_failure(code: str) -> ProviderFailureClass:
    normalized = code.strip().lower()
    if normalized in {"invalid_grant", "refresh_token_revoked", "account_deleted", "token_revoked"}:
        return ProviderFailureClass.RECONNECT_REQUIRED
    if normalized in {"scope_missing", "bad_signature", "malformed_response", "permission_denied"}:
        return ProviderFailureClass.PERMANENTLY_DENIED
    if normalized in {"provider_unavailable", "provider_5xx"}:
        return ProviderFailureClass.PROVIDER_UNHEALTHY
    if normalized in {"timeout", "rate_limit", "temporarily_unavailable"}:
        return ProviderFailureClass.RETRYABLE
    return ProviderFailureClass.PERMANENTLY_DENIED


@dataclass(frozen=True, slots=True)
class OAuthTransaction:
    flow_id: str
    state_digest: str
    pkce_verifier_digest: str
    session_id: str
    tenant_id: str
    company_id: str
    connection_id: str
    provider: str
    redirect_uri_digest: str
    nonce_digest: str
    scopes_requested: frozenset[str]
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuthorizationStart:
    connection_id: str
    authorization_url: str
    state: str = field(repr=False)
    pkce_verifier: str = field(repr=False)
    expires_at: datetime = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    connection_id: str
    tenant_id: str
    company_id: str
    usable: bool
    auth_successes: int
    auth_failures: int
    refresh_successes: int
    refresh_failures: int
    recent_error_class: str | None
    rate_limited_until: datetime | None
    last_reconciled_at: datetime | None
    updated_at: datetime
    state: ConnectionHealthState = ConnectionHealthState.UNKNOWN
    last_successful_check_at: datetime | None = None
    last_failure_at: datetime | None = None
    action_required: str | None = None
    quota_remaining: int | None = None
    quota_unit: str | None = None
    quota_checked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ConnectionCommand:
    """Safe idempotency record; it contains account identity, never submitted credential material."""

    tenant_id: str
    company_id: str
    idempotency_key: str
    provider: str
    account_ref: str
    connection_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderReality:
    active: bool
    provider_account_id: str | None
    scopes_granted: frozenset[str]
    safe_metadata: tuple[tuple[str, str], ...] = ()
    failure_class: ProviderFailureClass | None = None


@dataclass(frozen=True, slots=True)
class ProviderCallbackEvent:
    event_id: str
    provider: str
    connection_id: str
    event_type: str
    occurred_at: datetime
    safe_reason: str


class OAuthTokenMaterial:
    """Process-only OAuth material with redacted repr and best-effort zeroization."""

    __slots__ = ("_access", "_refresh", "expires_at", "scopes", "provider_account_id", "safe_metadata", "_closed")

    def __init__(self, *, access_token: bytes, refresh_token: bytes | None, expires_at: datetime,
                 scopes: frozenset[str], provider_account_id: str, safe_metadata: tuple[tuple[str, str], ...] = ()) -> None:
        self._access = bytearray(access_token)
        self._refresh = bytearray(refresh_token) if refresh_token is not None else None
        self.expires_at = expires_at
        self.scopes = scopes
        self.provider_account_id = provider_account_id
        self.safe_metadata = safe_metadata
        self._closed = False

    def __repr__(self) -> str:
        return f"OAuthTokenMaterial(redacted=True, closed={self._closed})"

    def encode_for_vault(self) -> bytes:
        if self._closed:
            raise RuntimeError("OAuth token material was discarded")
        return json.dumps({
            "access_token": bytes(self._access).hex(),
            "refresh_token": bytes(self._refresh).hex() if self._refresh is not None else None,
            "expires_at": iso(self.expires_at),
            "scopes": sorted(self.scopes),
        }, separators=(",", ":"), sort_keys=True).encode()

    @classmethod
    def decode_from_vault(cls, value: bytes, *, provider_account_id: str, safe_metadata=()):
        body = json.loads(value)
        return cls(
            access_token=bytes.fromhex(body["access_token"]),
            refresh_token=bytes.fromhex(body["refresh_token"]) if body.get("refresh_token") else None,
            expires_at=datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")),
            scopes=frozenset(body["scopes"]), provider_account_id=provider_account_id,
            safe_metadata=tuple(safe_metadata),
        )

    def access_token(self) -> bytes:
        if self._closed:
            raise RuntimeError("OAuth token material was discarded")
        return bytes(self._access)

    def refresh_token(self) -> bytes | None:
        if self._closed:
            raise RuntimeError("OAuth token material was discarded")
        return bytes(self._refresh) if self._refresh is not None else None

    def close(self) -> None:
        if self._closed:
            return
        for value in (self._access, self._refresh):
            if value is not None:
                for index in range(len(value)):
                    value[index] = 0
                value.clear()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def digest_text(value: str) -> str:
    return sha256(value.encode()).hexdigest()
