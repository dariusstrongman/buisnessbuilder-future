from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from .models import digest, iso
from .storage import RuntimeRepository


class AuditLog:
    def __init__(self, repository: RuntimeRepository, id_factory: Callable[[str], str], clock: Callable[[], datetime]):
        self.repository = repository
        self.id_factory = id_factory
        self.clock = clock

    def record(
        self,
        *,
        tenant_id: str,
        company_id: str,
        actor_type: str,
        actor_id: str,
        action: str,
        target_type: str,
        target_id: str,
        correlation_id: str,
        reason: str,
        before: Any = None,
        after: Any = None,
        approval_id: str | None = None,
        permission: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        value = {
            "schema_version": "audit-event.v1",
            "audit_event_id": self.id_factory("audit"),
            "tenant_id": tenant_id,
            "company_id": company_id,
            "actor_ref": {"type": actor_type, "id": actor_id, "version": 1},
            "action": action,
            "target_ref": {"type": target_type, "id": target_id, "version": 1},
            "occurred_at": iso(self.clock()),
            "before_digest": digest(before) if before is not None else None,
            "after_digest": digest(after) if after is not None else None,
            "reason": reason,
            "correlation_id": correlation_id,
            "approval_ref": (
                {"type": "approval", "id": approval_id, "version": 1} if approval_id else None
            ),
            "evidence_refs": [],
            "append_only": True,
        }
        if permission is not None:
            value["permission"] = permission
        if source is not None:
            value["source"] = source
        self.repository.append_audit(value)
        return value


def to_audit_contract(record: dict[str, Any]) -> dict[str, Any]:
    """Remove runtime-only tenant scope for released AuditEvent v1 validation."""
    return {
        key: value
        for key, value in record.items()
        if key not in {"tenant_id", "permission", "source"}
    }
