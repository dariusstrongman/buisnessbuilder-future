from __future__ import annotations

from datetime import datetime
from typing import Callable

from .audit import AuditLog
from .models import ApprovalMode, ApprovalRecord, ApprovalState
from .ports import ApprovalPrincipalVerifier
from .storage import RuntimeRepository


class ApprovalService:
    def __init__(
        self,
        repository: RuntimeRepository,
        audit: AuditLog,
        clock: Callable[[], datetime],
        principal_verifier: ApprovalPrincipalVerifier,
    ) -> None:
        self.repository = repository
        self.audit = audit
        self.clock = clock
        self.principal_verifier = principal_verifier

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
        principal: object,
        correlation_id: str,
    ) -> ApprovalRecord:
        approval = self.repository.get_approval(tenant_id, company_id, approval_id)
        if not approval:
            self._denied(
                principal, tenant_id, company_id, approval_id, correlation_id,
                "approval is outside tenant/company scope",
            )
            raise PermissionError("approval authorization denied")
        if approval.state != ApprovalState.REQUESTED:
            raise ValueError("approval has already been decided")
        if decision not in {ApprovalState.GRANTED, ApprovalState.DENIED}:
            raise ValueError("decision must be granted or denied")
        try:
            actor = self.principal_verifier.verify_approval(
                principal,
                tenant_id=tenant_id,
                company_id=company_id,
                required_role=approval.required_role,
                at=self.clock(),
            )
        except (PermissionError, LookupError, ValueError) as exc:
            reason = str(exc) or "principal verification failed"
            self._denied(
                principal, tenant_id, company_id, approval_id, correlation_id, reason
            )
            raise PermissionError("approval authorization denied") from None
        before = {"state": approval.state.value}
        approval.state = decision
        approval.decided_by = actor.actor_id
        approval.decided_by_role = actor.actor_role
        approval.version += 1
        self.repository.save_approval(approval)
        self.audit.record(
            tenant_id=tenant_id,
            company_id=company_id,
            actor_type=actor.actor_role,
            actor_id=actor.actor_id,
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

    def _denied(
        self,
        principal: object,
        tenant_id: str,
        company_id: str,
        approval_id: str,
        correlation_id: str,
        reason: str,
    ) -> None:
        principal_id = getattr(principal, "principal_id", None)
        self.audit.record(
            tenant_id=tenant_id,
            company_id=company_id,
            actor_type="untrusted_principal",
            actor_id=principal_id if isinstance(principal_id, str) else "unknown",
            action="authorization.denied",
            target_type="approval",
            target_id=approval_id,
            correlation_id=correlation_id,
            reason=reason,
            permission="runtime.approval.decide",
            source="businessbuilder.runtime.approvals",
        )

    def effective(self, approval: ApprovalRecord, subject_digest: str) -> bool:
        return approval.is_effective(self.clock(), subject_digest)
