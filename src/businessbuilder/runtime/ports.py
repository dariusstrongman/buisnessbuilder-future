from __future__ import annotations

from typing import Any, Protocol


class CompanyStateReader(Protocol):
    """Read-only seam implemented later by the Company Brain worker."""

    def company_exists(self, tenant_id: str, company_id: str) -> bool: ...


class CompanySnapshotProvider(Protocol):
    def get_snapshot(self, tenant_id: str, company_id: str) -> dict[str, Any]: ...


class VerificationPort(Protocol):
    """Command seam implemented later by the Verification worker."""

    def request_verification(
        self,
        *,
        tenant_id: str,
        company_id: str,
        job_id: str,
        artifact_refs: list[dict[str, Any]],
        correlation_id: str,
    ) -> None: ...


class FakeCompanyStateReader:
    def __init__(self, companies: set[tuple[str, str]] | None = None) -> None:
        self._companies = companies or set()

    def add(self, tenant_id: str, company_id: str) -> None:
        self._companies.add((tenant_id, company_id))

    def company_exists(self, tenant_id: str, company_id: str) -> bool:
        return (tenant_id, company_id) in self._companies


class RecordingVerificationPort:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def request_verification(self, **request: Any) -> None:
        self.requests.append(request)
