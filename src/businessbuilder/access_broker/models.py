from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Any


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,159}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _require_id(value: str, name: str) -> None:
    if not _ID.fullmatch(value):
        raise ValueError(f"{name} is not a safe opaque identifier")


def _require_time(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


def iso(value: datetime) -> str:
    _require_time(value, "timestamp")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ConnectionStatus(StrEnum):
    PENDING_AUTHORIZATION = "pending_authorization"
    ACTIVE = "active"
    RECONNECT_REQUIRED = "reconnect_required"
    DISCONNECTED = "disconnected"
    REVOKED = "revoked"
    EXPIRED = "expired"
    COMPROMISED = "compromised"


class ArtifactClassification(StrEnum):
    CUSTOMER_UPLOAD = "customer_upload"
    BUSINESS_DOCUMENT = "business_document"
    BRAND_ASSET = "brand_asset"
    SCREENSHOT = "screenshot"
    GENERATED_ASSET = "generated_asset"
    WEBSITE_EXPORT = "website_export"
    VERIFICATION_EVIDENCE = "verification_evidence"
    EXTERNAL_ATTACHMENT = "external_attachment"


class ArtifactStatus(StrEnum):
    AVAILABLE = "available"
    QUARANTINED = "quarantined"
    REVOKED = "revoked"


class ReceiptStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class CredentialScope:
    capability: str
    operations: frozenset[str]
    secret_types: frozenset[str]

    def __post_init__(self) -> None:
        _require_id(self.capability, "capability")
        if not self.operations or not self.secret_types:
            raise ValueError("credential scope requires operations and secret types")
        for value in (*self.operations, *self.secret_types):
            _require_id(value, "credential scope value")


@dataclass(frozen=True, slots=True)
class ExternalAccount:
    account_id: str
    tenant_id: str
    company_id: str
    provider: str
    external_account_ref: str
    label: str
    status: ConnectionStatus
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in ("account_id", "tenant_id", "company_id", "provider", "external_account_ref"):
            _require_id(getattr(self, name), name)
        _require_time(self.created_at, "created_at")
        _require_time(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    connection_id: str
    account_id: str
    tenant_id: str
    company_id: str
    provider: str
    status: ConnectionStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    revoked_reason: str | None = None
    account_type: str = "external_service"
    provider_account_id: str | None = None
    scopes_requested: frozenset[str] = frozenset()
    scopes_granted: frozenset[str] = frozenset()
    auth_method: str = "oauth2"
    connected_by: str | None = None
    connected_at: datetime | None = None
    refreshed_at: datetime | None = None
    revoked_at: datetime | None = None
    compromised_at: datetime | None = None
    provider_metadata: tuple[tuple[str, str], ...] = ()
    secret_ref_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("connection_id", "account_id", "tenant_id", "company_id", "provider"):
            _require_id(getattr(self, name), name)
        _require_time(self.created_at, "created_at")
        _require_time(self.updated_at, "updated_at")
        if self.expires_at:
            _require_time(self.expires_at, "expires_at")
        for value in (self.connected_at, self.refreshed_at, self.revoked_at, self.compromised_at):
            if value:
                _require_time(value, "connection timestamp")
        if self.provider_account_id:
            _require_id(self.provider_account_id, "provider_account_id")
        if self.connected_by:
            _require_id(self.connected_by, "connected_by")
        for scope in (*self.scopes_requested, *self.scopes_granted):
            _require_id(scope, "oauth scope")
        if not self.account_type or not self.auth_method:
            raise ValueError("provider connection account_type and auth_method are required")
        if len(self.provider_metadata) > 32 or any(
            not key or len(key) > 80 or len(value) > 300
            for key, value in self.provider_metadata
        ):
            raise ValueError("provider metadata is not safe for persistence")

    @property
    def provider_connection_id(self) -> str:
        return self.connection_id


@dataclass(frozen=True, slots=True)
class ExternalCredentialRef:
    secret_ref: str
    connection_id: str
    tenant_id: str
    company_id: str
    provider: str
    secret_type: str
    secret_locator: str
    status: ConnectionStatus
    created_at: datetime
    rotated_at: datetime
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("secret_ref", "connection_id", "tenant_id", "company_id", "provider", "secret_type"):
            _require_id(getattr(self, name), name)
        if not self.secret_ref.startswith("secretref_"):
            raise ValueError("opaque secret references must use the secretref_ prefix")
        if not self.secret_locator or any(c in self.secret_locator for c in "\r\n\0"):
            raise ValueError("secret locator is invalid")
        _require_time(self.created_at, "created_at")
        _require_time(self.rotated_at, "rotated_at")
        if self.expires_at:
            _require_time(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True)
class JobSecretRef:
    """Credential-free projection allowed in a Runtime/SQS job envelope."""

    secret_ref: str
    provider: str
    capability: str
    tenant_id: str
    company_id: str

    def __post_init__(self) -> None:
        for name in ("secret_ref", "provider", "capability", "tenant_id", "company_id"):
            _require_id(getattr(self, name), name)
        if not self.secret_ref.startswith("secretref_"):
            raise ValueError("opaque secret references must use the secretref_ prefix")

    def to_contract(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    grant_id: str
    tenant_id: str
    company_id: str
    connection_id: str
    agent_role: str
    scope: CredentialScope
    artifact_classifications: frozenset[ArtifactClassification]
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("grant_id", "tenant_id", "company_id", "connection_id", "agent_role"):
            _require_id(getattr(self, name), name)
        _require_time(self.created_at, "created_at")
        for value in (self.expires_at, self.revoked_at):
            if value:
                _require_time(value, "grant timestamp")

    def permits(self, *, capability: str, operation: str, secret_type: str, at: datetime) -> bool:
        return (
            self.revoked_at is None
            and (self.expires_at is None or self.expires_at > at)
            and self.scope.capability == capability
            and operation in self.scope.operations
            and secret_type in self.scope.secret_types
        )


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    tenant_id: str
    company_id: str
    object_key: str
    content_sha256: str
    content_type: str
    size_bytes: int
    classification: ArtifactClassification
    status: ArtifactStatus
    provenance_ref: str
    created_at: datetime
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("artifact_id", "tenant_id", "company_id", "provenance_ref"):
            _require_id(getattr(self, name), name)
        if not _SHA256.fullmatch(self.content_sha256):
            raise ValueError("artifact content_sha256 must be lowercase SHA-256")
        if not self.content_type or len(self.content_type) > 200:
            raise ValueError("artifact content type is invalid")
        if not 0 <= self.size_bytes <= 25 * 1024 * 1024:
            raise ValueError("artifact exceeds broker size limit")
        expected = f"tenant/{self.tenant_id}/company/{self.company_id}/"
        if not self.object_key.startswith(expected) or ".." in self.object_key:
            raise ValueError("artifact object key is outside tenant/company scope")
        _require_time(self.created_at, "created_at")
        if self.expires_at:
            _require_time(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True)
class SignedArtifactAccess:
    artifact_id: str
    content_sha256: str
    content_type: str
    size_bytes: int
    expires_at: datetime
    _url: str = field(repr=False)

    def consume_url(self) -> str:
        return self._url


@dataclass(frozen=True, slots=True)
class ProviderReceipt:
    receipt_id: str
    tenant_id: str
    company_id: str
    job_id: str
    capability: str
    provider: str
    operation: str
    provider_request_id: str
    idempotency_key: str
    status: ReceiptStatus
    timestamp: datetime
    response_classification: str
    external_object_ref: str | None
    retryable: bool
    attempts: int = 1
    completed_at: datetime | None = None
    connection_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "receipt_id", "tenant_id", "company_id", "job_id", "capability",
            "provider", "operation", "provider_request_id", "idempotency_key",
        ):
            _require_id(getattr(self, name), name)
        if self.attempts < 1:
            raise ValueError("provider receipt attempts must be positive")
        _require_time(self.timestamp, "timestamp")
        if self.completed_at:
            _require_time(self.completed_at, "completed_at")


@dataclass(frozen=True, slots=True)
class ProviderActionResult:
    status: ReceiptStatus
    response_classification: str
    external_object_ref: str | None
    retryable: bool = False


def stable_id(prefix: str, *parts: str) -> str:
    body = json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode()
    return f"{prefix}_{sha256(body).hexdigest()[:24]}"
