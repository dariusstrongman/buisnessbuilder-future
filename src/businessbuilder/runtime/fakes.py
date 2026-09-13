from __future__ import annotations

from dataclasses import dataclass

from hashlib import sha256
from datetime import datetime
from pathlib import Path
from typing import Any

from .ports import VerifiedApprovalActor


@dataclass(frozen=True, slots=True)
class FakeApprovalPrincipal:
    """Opaque principal used only by deterministic offline fixtures."""

    principal_id: str


class FakeApprovalPrincipalAuthority:
    def __init__(self) -> None:
        self._principals: dict[
            str, tuple[FakeApprovalPrincipal, str, str, VerifiedApprovalActor]
        ] = {}

    def issue(
        self,
        *,
        principal_id: str,
        tenant_id: str,
        company_id: str,
        actor_id: str,
        actor_role: str,
    ) -> FakeApprovalPrincipal:
        principal = FakeApprovalPrincipal(principal_id)
        self._principals[principal_id] = (
            principal,
            tenant_id,
            company_id,
            VerifiedApprovalActor(actor_id, actor_role),
        )
        return principal

    def verify_approval(
        self,
        principal: object,
        *,
        tenant_id: str,
        company_id: str,
        required_role: str,
        at,
    ) -> VerifiedApprovalActor:
        if not isinstance(principal, FakeApprovalPrincipal):
            raise PermissionError("trusted authenticated principal required")
        registered = self._principals.get(principal.principal_id)
        if registered is None or registered[0] is not principal:
            raise PermissionError("untrusted principal")
        _, expected_tenant, expected_company, actor = registered
        if (tenant_id, company_id) != (expected_tenant, expected_company):
            raise PermissionError("principal request scope mismatch")
        if actor.actor_role != required_role:
            raise PermissionError(f"approval requires role {required_role}")
        return actor

from .capabilities import Capability
from .contracts import ContractValidator
from .models import (
    ArtifactRef,
    CapabilityFailure,
    CapabilityRequest,
    CapabilityResult,
    FailureKind,
    Money,
)


class FakeCapability(Capability):
    def __init__(
        self,
        name: str,
        *,
        version: str = "v1",
        estimate_minor: int = 100,
        spend_minor: int | None = None,
        failures_before_success: int = 0,
        retryable_failure: bool = True,
    ) -> None:
        self.name = name
        self.version = version
        self.estimate_minor = estimate_minor
        self.spend_minor = spend_minor if spend_minor is not None else estimate_minor
        self.failures_before_success = failures_before_success
        self.retryable_failure = retryable_failure
        self.execute_count = 0
        self._results: dict[str, CapabilityResult] = {}
        self._statuses: dict[str, str] = {}

    def validate_request(self, request: CapabilityRequest) -> None:
        if request.capability != self.name or request.capability_version != self.version:
            raise ValueError("capability request does not match provider")
        if not request.inputs:
            raise ValueError("inputs cannot be empty")

    def estimate(self, request: CapabilityRequest) -> Money:
        return Money("USD", self.estimate_minor)

    def execute(self, request: CapabilityRequest) -> CapabilityResult:
        self.execute_count += 1
        provider_ref = f"local:{self.name}:{request.request_id}"
        if self.execute_count <= self.failures_before_success:
            result = CapabilityResult(
                provider_ref=provider_ref,
                status="failed",
                artifacts=(),
                spend=Money("USD", 0),
                progress=("accepted", "failed"),
                failure=CapabilityFailure(
                    code="FAKE_TRANSIENT" if self.retryable_failure else "FAKE_PERMANENT",
                    safe_message="Fake capability failure",
                    retryable=self.retryable_failure,
                    kind=FailureKind.RETRYABLE if self.retryable_failure else FailureKind.PERMANENT,
                ),
            )
        else:
            artifact_key = sha256(f"{request.company_id}:{request.idempotency_key}".encode()).hexdigest()[:20]
            result = CapabilityResult(
                provider_ref=provider_ref,
                status="completed",
                artifacts=(ArtifactRef(type="artifact", id=f"artifact_{artifact_key}"),),
                spend=Money("USD", self.spend_minor),
                progress=("accepted", "working", "completed"),
            )
        self._results[provider_ref] = result
        self._statuses[provider_ref] = result.status
        return result

    def status(self, provider_ref: str) -> str:
        return self._statuses.get(provider_ref, "unknown")

    def cancel(self, provider_ref: str) -> bool:
        if provider_ref not in self._statuses or self._statuses[provider_ref] in {"completed", "failed"}:
            return False
        self._statuses[provider_ref] = "cancelled"
        return True

    def collect_result(self, provider_ref: str) -> CapabilityResult | None:
        return self._results.get(provider_ref)


class FakeWebsiteCapability(FakeCapability):
    name = "fake.website.build"
    version = "v1"

    def __init__(
        self,
        contracts_dir: str | Path,
        *,
        estimate_minor: int = 400,
        spend_minor: int = 271,
        failures_before_success: int = 0,
    ) -> None:
        super().__init__(
            self.name,
            version=self.version,
            estimate_minor=estimate_minor,
            spend_minor=spend_minor,
            failures_before_success=failures_before_success,
        )
        self.validator = ContractValidator(contracts_dir)

    def validate_request(self, request: CapabilityRequest) -> None:
        super().validate_request(request)
        website_request = request.inputs.get("website_request")
        if not isinstance(website_request, dict):
            raise ValueError("website_request is required")
        if (
            website_request.get("company_id") != request.company_id
            or website_request.get("job_id") != request.job_id
            or website_request.get("correlation_id") != request.correlation_id
            or website_request.get("idempotency_key") != request.idempotency_key
        ):
            raise ValueError("website request scope/provenance mismatch")
        self.validator.validate("website-capability.schema.json", website_request)

    def execute(self, request: CapabilityRequest) -> CapabilityResult:
        result = super().execute(request)
        if result.failure:
            return result
        website_progress = (
            "accepted",
            "researching",
            "concepts_ready",
            "building",
            "reviewing",
            "package_ready",
        )
        package_id = f"artifact_{sha256(request.idempotency_key.encode()).hexdigest()[:20]}"
        final = CapabilityResult(
            provider_ref=result.provider_ref,
            status="package_ready",
            artifacts=(ArtifactRef(type="artifact", id=package_id),),
            spend=result.spend,
            progress=website_progress,
        )
        self._results[result.provider_ref] = final
        self._statuses[result.provider_ref] = "package_ready"
        return final

    def to_contract_result(
        self,
        request: CapabilityRequest,
        result: CapabilityResult,
        completed_at: datetime,
    ) -> dict[str, Any]:
        website_request = request.inputs["website_request"]
        value: dict[str, Any] = {
            "schema_version": "website.capability.result.v1",
            "request_id": website_request["request_id"],
            "company_id": request.company_id,
            "job_id": request.job_id,
            "provider_job_ref": result.provider_ref,
            "status": result.status,
            "artifact_refs": [artifact.to_contract() for artifact in result.artifacts],
            "verification_refs": [],
            "spend": result.spend.to_contract(),
            "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        }
        if result.failure:
            value["failure"] = {
                "code": result.failure.code,
                "retryable": result.failure.retryable,
                "safe_message": result.failure.safe_message,
            }
        self.validator.validate("website-capability.schema.json", value)
        return value


class FakeCrmCapability(FakeCapability):
    def __init__(self) -> None:
        super().__init__("fake.crm.setup", estimate_minor=125, spend_minor=100)


class FakeEmailCapability(FakeCapability):
    def __init__(self) -> None:
        super().__init__("fake.email.setup", estimate_minor=75, spend_minor=60)
