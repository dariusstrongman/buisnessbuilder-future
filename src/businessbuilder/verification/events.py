from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import VerificationRecord
from .service import VerificationService


@dataclass(frozen=True)
class CanonicalEvent:
    event_id: str
    tenant_id: str
    company_id: str
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any]


class VerificationEventHandler:
    """Idempotent-shaped boundary for Worker 2; event durability remains Worker 2's."""

    DEPENDENCY_EVENTS = frozenset(
        {
            "dependency.changed",
            "website.deployment.changed",
            "service_area.changed",
            "policy.changed",
            "domain.ownership.changed",
        }
    )

    def __init__(self, service: VerificationService) -> None:
        self.service = service
        self._handled_event_ids: set[str] = set()

    def handle(self, event: CanonicalEvent) -> tuple[VerificationRecord, ...]:
        if event.event_id in self._handled_event_ids:
            return ()
        if event.event_type not in self.DEPENDENCY_EVENTS:
            self._handled_event_ids.add(event.event_id)
            return ()
        dependency_id = event.payload.get("dependency_id")
        new_version = event.payload.get("new_version")
        if not isinstance(dependency_id, str) or not isinstance(new_version, int):
            raise ValueError("dependency change event requires dependency_id and integer new_version")
        changed = self.service.invalidate_dependency(
            event.tenant_id,
            event.company_id,
            dependency_id,
            new_version,
            at=event.occurred_at,
        )
        self._handled_event_ids.add(event.event_id)
        return changed
