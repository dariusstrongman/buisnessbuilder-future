from __future__ import annotations

from datetime import datetime
from typing import Callable

from .audit import AuditLog
from .models import ApprovalMode, ApprovalRecord, ApprovalState
from .storage import RuntimeRepository


class ApprovalService:
    def __init__(
        self,
        repository: RuntimeRepository,
        audit: AuditLog,
        clock: Callable[[], datetime],
    ) -> None:
        self.repository = repository
        self.audit = audit
        self.clock = clock

    def request(self, approval: ApprovalRecord, correlation_id: str) -> ApprovalRecord:
        if approval.mode in {ApprovalMode.AUTONOMOUS, ApprovalMode.PROHIBITED}:
            raise ValueError(f"{approval.mode.value} does not create approval records")
        self.repository.save_approval(approval)
        self.audit.record(
            tenant_id=approval.tenant_id,
            company_id=approval.company_id,
            actor_type="system",
            actor_id="runtime_approvals",
            action="approval.requested",
            target_type="approval",
            target_id=approval.approval_id,
            correlation_id=correlation_id,
            reason="Approval required before job execution",
            after={"state": approval.state.value, "subject_digest": approval.subject_digest},
        )
        return approval

    def decide(
        self,
        *,
        tenant_id: str,
        company_id: str,
        approval_id: str,
        decision: ApprovalState,
        actor_id: str,
        actor_role: str,
        correlation_id: str,
    ) -> ApprovalRecord:
        approval = self.repository.get_approval(tenant_id, company_id, approval_id)
        if not approval:
            raise LookupError("approval not found in tenant/company scope")
        if approval.state != ApprovalState.REQUESTED:
            raise ValueError("approval has already been decided")
        if decision not in {ApprovalState.GRANTED, ApprovalState.DENIED}:
            raise ValueError("decision must be granted or denied")
        if approval.mode == ApprovalMode.FOUNDER_ONLY and actor_role != "founder":
            raise PermissionError("founder-only approval requires a founder decision")
        if approval.required_role != actor_role:
            raise PermissionError(f"approval requires role {approval.required_role}")
        before = {"state": approval.state.value}
        approval.state = decision
        approval.decided_by = actor_id
        approval.decided_by_role = actor_role
        approval.version += 1
        self.repository.save_approval(approval)
        self.audit.record(
            tenant_id=tenant_id,
            company_id=company_id,
            actor_type=actor_role,
            actor_id=actor_id,
            action=f"approval.{decision.value}",
            target_type="approval",
            target_id=approval_id,
            correlation_id=correlation_id,
            reason=f"Approval explicitly {decision.value}",
            before=before,
            after={"state": decision.value},
            approval_id=approval_id,
        )
        return approval

    def effective(self, approval: ApprovalRecord, subject_digest: str) -> bool:
        return approval.is_effective(self.clock(), subject_digest)
