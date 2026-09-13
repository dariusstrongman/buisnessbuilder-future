from __future__ import annotations

from .audit import AuditLog
from .models import Budget, Job, Money
from .storage import RuntimeRepository


class BudgetExceeded(RuntimeError):
    pass


class BudgetService:
    def __init__(self, repository: RuntimeRepository, audit: AuditLog) -> None:
        self.repository = repository
        self.audit = audit

    def create(self, budget: Budget, correlation_id: str) -> Budget:
        self.repository.save_budget(budget)
        self.audit.record(
            tenant_id=budget.tenant_id,
            company_id=budget.company_id,
            actor_type="system",
            actor_id="runtime_budget",
            action="budget.created",
            target_type="budget",
            target_id=budget.budget_id,
            correlation_id=correlation_id,
            reason="Created local fail-closed company budget",
            after={"ceiling": budget.ceiling.to_contract()},
        )
        return budget

    def reserve(self, job: Job, estimate: Money) -> None:
        budget = self._budget(job)
        self._same_currency(budget.ceiling, estimate)
        if budget.state != "active":
            raise BudgetExceeded("budget is not active")
        if estimate.minor_units > job.per_job_ceiling.minor_units:
            raise BudgetExceeded("estimate exceeds per-job ceiling")
        if estimate.minor_units > budget.remaining_minor:
            raise BudgetExceeded("estimate exceeds remaining company budget")
        before = {"reserved_minor": budget.reserved_minor, "remaining_minor": budget.remaining_minor}
        budget.reserved_minor += estimate.minor_units
        budget.version += 1
        job.reserved_minor = estimate.minor_units
        self.repository.save_budget(budget)
        self.repository.save_reservation(job, budget.budget_id, estimate.currency, estimate.minor_units, 0, "reserved")
        self.audit.record(
            tenant_id=job.tenant_id,
            company_id=job.company_id,
            actor_type="system",
            actor_id="runtime_budget",
            action="budget.reserved",
            target_type="job",
            target_id=job.job_id,
            correlation_id=job.correlation_id,
            reason="Reserved estimated spend before capability execution",
            before=before,
            after={"reserved_minor": budget.reserved_minor, "remaining_minor": budget.remaining_minor},
        )

    def settle(self, job: Job, actual: Money) -> None:
        budget = self._budget(job)
        self._same_currency(budget.ceiling, actual)
        if actual.minor_units > job.reserved_minor:
            raise BudgetExceeded("actual spend exceeds provider reservation")
        before = {"reserved_minor": budget.reserved_minor, "settled_minor": budget.settled_minor}
        budget.reserved_minor -= job.reserved_minor
        budget.settled_minor += actual.minor_units
        budget.state = "exhausted" if budget.remaining_minor == 0 else budget.state
        budget.version += 1
        job.settled_minor = actual.minor_units
        reserved = job.reserved_minor
        job.reserved_minor = 0
        self.repository.save_budget(budget)
        self.repository.save_reservation(job, budget.budget_id, actual.currency, reserved, actual.minor_units, "settled")
        self.audit.record(
            tenant_id=job.tenant_id,
            company_id=job.company_id,
            actor_type="system",
            actor_id="runtime_budget",
            action="budget.settled",
            target_type="job",
            target_id=job.job_id,
            correlation_id=job.correlation_id,
            reason="Settled actual capability spend and released unused reservation",
            before=before,
            after={"reserved_minor": budget.reserved_minor, "settled_minor": budget.settled_minor},
        )

    def release(self, job: Job, reason: str) -> None:
        if job.reserved_minor == 0:
            return
        budget = self._budget(job)
        before = budget.reserved_minor
        released = job.reserved_minor
        budget.reserved_minor -= released
        budget.version += 1
        job.reserved_minor = 0
        self.repository.save_budget(budget)
        self.repository.save_reservation(job, budget.budget_id, budget.ceiling.currency, released, 0, "released")
        self.audit.record(
            tenant_id=job.tenant_id,
            company_id=job.company_id,
            actor_type="system",
            actor_id="runtime_budget",
            action="budget.released",
            target_type="job",
            target_id=job.job_id,
            correlation_id=job.correlation_id,
            reason=reason,
            before={"reserved_minor": before},
            after={"reserved_minor": budget.reserved_minor},
        )

    def _budget(self, job: Job) -> Budget:
        budget = self.repository.get_budget(job.tenant_id, job.company_id, job.budget_ref)
        if not budget:
            raise BudgetExceeded("budget not found in tenant/company scope")
        return budget

    @staticmethod
    def _same_currency(left: Money, right: Money) -> None:
        if left.currency != right.currency:
            raise BudgetExceeded("currency mismatch")
