from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


class VerificationState(StrEnum):
    PROPOSED = "proposed"
    EXECUTED = "executed"
    TESTED = "tested"
    VERIFIED = "verified"
    FAILED = "failed"
    EXPIRED = "expired"


class VerificationMethod(StrEnum):
    AUTOMATED = "automated"
    HUMAN = "human"
    FOUNDER_ONLY = "founder_only"
    EXTERNAL = "external"
    AUTOMATED_PLUS_HUMAN = "automated_plus_human"


class EvidenceType(StrEnum):
    TEST_RESULT = "test_result"
    SCREENSHOT = "screenshot"
    PROVIDER_RECEIPT = "provider_receipt"
    AUTHORITY_CONFIRMATION = "authority_confirmation"
    DNS_OBSERVATION = "dns_observation"
    API_RESPONSE = "api_response_reference"
    FOUNDER_ATTESTATION = "founder_attestation"
    HUMAN_REVIEW = "human_review"
    AUDIT_RECORD = "audit_record"


class BlockerSeverity(StrEnum):
    INFORMATIONAL = "informational"
    NONCRITICAL = "noncritical"
    CRITICAL = "critical"


class ExternalState(StrEnum):
    PREPARED = "prepared"
    SUBMITTED = "submitted"
    OBSERVED = "observed"
    FOUNDER_ATTESTED = "founder_attested"
    EXTERNALLY_VERIFIED = "externally_verified"


class FounderActionStatus(StrEnum):
    REQUIRED = "required"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    VERIFIED = "verified"
    DECLINED = "declined"
    EXPIRED = "expired"
    WAIVED_NONCRITICAL = "waived_noncritical"


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    evidence_type: EvidenceType
    artifact_ref: str
    tenant_id: str
    company_id: str
    captured_at: datetime
    expires_at: datetime | None = None
    test_name: str | None = None
    test_passed: bool | None = None
    issuer: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def is_current(self, at: datetime) -> bool:
        return self.expires_at is None or self.expires_at > at

    def supports_defined_test(self, test_names: frozenset[str], at: datetime) -> bool:
        return (
            self.evidence_type is EvidenceType.TEST_RESULT
            and self.test_passed is True
            and bool(self.test_name)
            and (not test_names or self.test_name in test_names)
            and self.is_current(at)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "evidence_type": self.evidence_type.value,
            "artifact_ref": self.artifact_ref,
            "tenant_id": self.tenant_id,
            "company_id": self.company_id,
            "captured_at": iso(self.captured_at),
            "expires_at": iso(self.expires_at),
            "test_name": self.test_name,
            "test_passed": self.test_passed,
            "issuer": self.issuer,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceRef":
        return cls(
            evidence_id=value["evidence_id"],
            evidence_type=EvidenceType(value["evidence_type"]),
            artifact_ref=value["artifact_ref"],
            tenant_id=value["tenant_id"],
            company_id=value["company_id"],
            captured_at=datetime.fromisoformat(value["captured_at"].replace("Z", "+00:00")),
            expires_at=(
                datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
                if value.get("expires_at")
                else None
            ),
            test_name=value.get("test_name"),
            test_passed=value.get("test_passed"),
            issuer=value.get("issuer"),
            provenance=dict(value.get("provenance", {})),
        )


@dataclass(frozen=True)
class DependencyRef:
    dependency_id: str
    observed_version: int
    dependency_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dependency_id": self.dependency_id,
            "observed_version": self.observed_version,
            "dependency_type": self.dependency_type,
        }


@dataclass(frozen=True)
class VerificationRecord:
    verification_id: str
    tenant_id: str
    company_id: str
    definition_id: str
    target_type: str
    target_id: str
    state: VerificationState
    owner: str | None
    scope: str
    method: VerificationMethod
    evidence: tuple[EvidenceRef, ...] = ()
    expires_at: datetime | None = None
    dependencies: tuple[DependencyRef, ...] = ()
    failure_reason: str | None = None
    stale_reason: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    provenance: dict[str, Any] = field(default_factory=dict)
    version: int = 1

    def with_update(self, **changes: Any) -> "VerificationRecord":
        return replace(self, updated_at=changes.pop("updated_at", utc_now()), version=self.version + 1, **changes)

    def is_current(self, at: datetime) -> bool:
        record_current = self.expires_at is None or self.expires_at > at
        return record_current and all(item.is_current(at) for item in self.evidence)

    def to_contract(self) -> dict[str, Any]:
        """Losslessly exposes what Verification v1 can represent.

        Tenant, definition, owner, dependencies and full provenance remain in the
        domain record because the shared v1 schema does not yet contain them.
        """
        return {
            "schema_version": "verification.v1",
            "verification_id": self.verification_id,
            "company_id": self.company_id,
            "target_ref": {"type": self.target_type, "id": self.target_id},
            "state": self.state.value,
            "method": self.method.value,
            "evidence_refs": [
                {"type": "artifact", "id": artifact_ref}
                for artifact_ref in dict.fromkeys(item.artifact_ref for item in self.evidence)
            ],
            "verified_scope": self.scope or "not-yet-defined",
            "failure_code": self.provenance.get("failure_code"),
            "expires_at": iso(self.expires_at),
            "retest_on": [item.dependency_id for item in self.dependencies],
            "criticality": self.provenance.get("criticality", "material"),
            "updated_at": iso(self.updated_at),
            "version": self.version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "tenant_id": self.tenant_id,
            "company_id": self.company_id,
            "definition_id": self.definition_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "state": self.state.value,
            "owner": self.owner,
            "scope": self.scope,
            "method": self.method.value,
            "evidence": [item.to_dict() for item in self.evidence],
            "expires_at": iso(self.expires_at),
            "dependencies": [item.to_dict() for item in self.dependencies],
            "failure_reason": self.failure_reason,
            "stale_reason": self.stale_reason,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "provenance": self.provenance,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VerificationRecord":
        parse = lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
        return cls(
            verification_id=value["verification_id"],
            tenant_id=value["tenant_id"],
            company_id=value["company_id"],
            definition_id=value["definition_id"],
            target_type=value["target_type"],
            target_id=value["target_id"],
            state=VerificationState(value["state"]),
            owner=value.get("owner"),
            scope=value.get("scope", ""),
            method=VerificationMethod(value["method"]),
            evidence=tuple(EvidenceRef.from_dict(item) for item in value.get("evidence", [])),
            expires_at=parse(value.get("expires_at")),
            dependencies=tuple(DependencyRef(**item) for item in value.get("dependencies", [])),
            failure_reason=value.get("failure_reason"),
            stale_reason=value.get("stale_reason"),
            created_at=parse(value["created_at"]),
            updated_at=parse(value["updated_at"]),
            provenance=dict(value.get("provenance", {})),
            version=value.get("version", 1),
        )


@dataclass(frozen=True)
class Blocker:
    blocker_id: str
    tenant_id: str
    company_id: str
    severity: BlockerSeverity
    source: str
    affected_target: str
    reason: str
    remediation: str
    owner: str
    open: bool = True


@dataclass(frozen=True)
class ExternalRecord:
    external_record_id: str
    tenant_id: str
    company_id: str
    subject: str
    state: ExternalState
    evidence: tuple[EvidenceRef, ...] = ()
    authority: str | None = None
    updated_at: datetime = field(default_factory=utc_now)

    def is_authoritatively_verified(self, at: datetime) -> bool:
        if self.state is not ExternalState.EXTERNALLY_VERIFIED or not self.authority:
            return False
        return any(
            item.evidence_type in {EvidenceType.AUTHORITY_CONFIRMATION, EvidenceType.PROVIDER_RECEIPT}
            and item.is_current(at)
            and item.issuer == self.authority
            for item in self.evidence
        )
