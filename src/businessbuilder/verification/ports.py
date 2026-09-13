from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import FounderActionStatus


@dataclass(frozen=True)
class FounderActionSnapshot:
    action_id: str
    status: FounderActionStatus
    critical: bool
    selected: bool = True


@dataclass(frozen=True)
class CompanySnapshot:
    """Public read model expected from Company Brain, never its storage model."""

    tenant_id: str
    company_id: str
    version: int
    approved_offer_ids: tuple[str, ...]
    approved_service_area_ids: tuple[str, ...]
    owner_ids: tuple[str, ...]
    founder_actions: tuple[FounderActionSnapshot, ...]
    scheduling_required: bool = True
    facts: frozenset[str] = frozenset()
    selected_fully_set_requirements: tuple[str, ...] = ()
    material_waivers: tuple[str, ...] = ()
    critical_unresolved_obligations: tuple[str, ...] = ()


class CompanySnapshotPort(Protocol):
    def get_snapshot(self, tenant_id: str, company_id: str) -> CompanySnapshot: ...


class DictCompanySnapshotPort:
    """Fixture adapter used until Worker 1's public snapshot service is merged."""

    def __init__(self, snapshots: tuple[CompanySnapshot, ...]) -> None:
        self._snapshots = {(item.tenant_id, item.company_id): item for item in snapshots}

    def get_snapshot(self, tenant_id: str, company_id: str) -> CompanySnapshot:
        try:
            return self._snapshots[(tenant_id, company_id)]
        except KeyError as exc:
            raise KeyError("company snapshot not found in tenant/company scope") from exc
