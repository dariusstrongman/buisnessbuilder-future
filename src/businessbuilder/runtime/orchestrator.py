from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable

from .approvals import ApprovalService
from .audit import AuditLog
from .budgets import BudgetExceeded, BudgetService
from .capabilities import CapabilityRegistry
from .events import LocalEventBus
from .ids import random_id
from .models import (
    ApprovalMode,
    ApprovalRecord,
    ApprovalState,
    CapabilityFailure,
    CapabilityRequest,
    Event,
    FailureKind,
    Job,
    JobStatus,
    Money,
    RetryPolicy,
    utc_now,
)
from .ports import (
    ApprovalPrincipalVerifier,
    CompanyStateReader,
    DenyAllApprovalPrincipalVerifier,
    VerificationPort,
)
from .storage import RuntimeRepository


class IllegalTransition(RuntimeError):
    pass


TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.QUEUED: {
        JobStatus.WAITING_DEPENDENCIES,
        JobStatus.WAITING_APPROVAL,
        JobStatus.RUNNABLE,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.WAITING_DEPENDENCIES: {
        JobStatus.WAITING_APPROVAL,
        JobStatus.RUNNABLE,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.WAITING_APPROVAL: {JobStatus.RUNNABLE, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.RUNNABLE: {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.RUNNING: {JobStatus.RUNNABLE, JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}


class JobOrchestrator:
    def __init__(
        self,
        *,
        repository: RuntimeRepository,
        registry: CapabilityRegistry,
        company_reader: CompanyStateReader,
        verification: VerificationPort,
        id_factory: Callable[[str], str] = random_id,
        clock: Callable[[], datetime] = utc_now,
        approval_principals: ApprovalPrincipalVerifier | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.company_reader = company_reader
        self.verification = verification
        self.id_factory = id_factory
        self.clock = clock
        self.audit = AuditLog(repository, id_factory, clock)
        self.events = LocalEventBus(repository, self.audit)
        self.approvals = ApprovalService(
            repository,
            self.audit,
            clock,
            approval_principals or DenyAllApprovalPrincipalVerifier(),
        )
        self.budgets = BudgetService(repository, self.audit)

    def create_job(
        self,
        *,
        tenant_id: str,
        company_id: str,
        capability: str,
        inputs: dict[str, Any],
        budget_ref: str,
        per_job_ceiling: Money,
        idempotency_key: str,
        correlation_id: str,
        capability_version: str = "v1",
        dependencies: tuple[str, ...] = (),
        approval_mode: ApprovalMode = ApprovalMode.AUTONOMOUS,
        approval_ids: tuple[str, ...] = (),
        retry_policy: RetryPolicy = RetryPolicy(),
        job_id: str | None = None,
        causation_id: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> Job:
        if len(idempotency_key) < 16:
            raise ValueError("idempotency_key must be at least 16 characters")
        existing = self.repository.get_job_by_idempotency(tenant_id, company_id, idempotency_key)
        if existing:
            return existing
        if not self.company_reader.company_exists(tenant_id, company_id):
            raise LookupError("company not found in tenant scope")
        self.registry.get(capability, capability_version)
        now = self.clock()
        job = Job(
            job_id=job_id or self.id_factory("job"),
            tenant_id=tenant_id,
            company_id=company_id,
            capability=capability,
            capability_version=capability_version,
            inputs=inputs,
            dependency_ids=dependencies,
            status=JobStatus.QUEUED,
            attempts=0,
            retry_policy=retry_policy,
            budget_ref=budget_ref,
            per_job_ceiling=per_job_ceiling,
            approval_mode=approval_mode,
            approval_ids=approval_ids,
            artifacts=(),
            failure=None,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            causation_id=causation_id,
            provenance=provenance or {"source": "runtime.command", "correlation_id": correlation_id},
            created_at=now,
            updated_at=now,
        )
        self.repository.save_job(job)
        self.audit.record(
            tenant_id=tenant_id,
            company_id=company_id,
            actor_type="system",
            actor_id="runtime_orchestrator",
            action="job.created",
            target_type="job",
            target_id=job.job_id,
            correlation_id=correlation_id,
            reason=f"Created job for provider-neutral capability {capability}",
            after=job.to_contract(),
        )
        self._emit(job, "job.requested", {"capability": capability})

        if approval_mode == ApprovalMode.PROHIBITED:
            job.failure = CapabilityFailure(
                "ACTION_PROHIBITED",
                "This action is prohibited by policy",
                False,
                FailureKind.PROHIBITED,
            )
            self._transition(job, JobStatus.FAILED, "Policy prohibits capability execution")
            self._emit(job, "job.failed", {"failure_kind": FailureKind.PROHIBITED.value})
            return job

        if approval_mode in {ApprovalMode.APPROVAL_REQUIRED, ApprovalMode.FOUNDER_ONLY} and not approval_ids:
            approval = ApprovalRecord(
                approval_id=self.id_factory("approval"),
                tenant_id=tenant_id,
                company_id=company_id,
                job_id=job.job_id,
                mode=approval_mode,
                subject_digest=job.subject_digest,
                required_role="founder" if approval_mode == ApprovalMode.FOUNDER_ONLY else "authorized_human",
            )
            self.approvals.request(approval, correlation_id)
            job.approval_ids = (approval.approval_id,)
            self.repository.save_job(job)

        return self.refresh(job)

    def refresh(self, job: Job) -> Job:
        if job.terminal or job.status == JobStatus.RUNNING:
            return job
        dependency_state = self._dependency_state(job)
        if dependency_state:
            job.failure = dependency_state
            if job.status != JobStatus.WAITING_DEPENDENCIES:
                self._transition(job, JobStatus.WAITING_DEPENDENCIES, dependency_state.safe_message)
            return job
        approval_state = self._approval_state(job)
        if approval_state:
            job.failure = approval_state
            if job.status != JobStatus.WAITING_APPROVAL:
                self._transition(job, JobStatus.WAITING_APPROVAL, approval_state.safe_message)
            return job
        job.failure = None
        if job.status != JobStatus.RUNNABLE:
            self._transition(job, JobStatus.RUNNABLE, "Dependencies and approvals satisfied")
        return job

    def run(self, tenant_id: str, company_id: str, job_id: str) -> Job:
        job = self._job(tenant_id, company_id, job_id)
        if job.terminal:
            return job
        job = self.refresh(job)
        if job.status != JobStatus.RUNNABLE:
            return job
        provider = self.registry.get(job.capability, job.capability_version)
        attempt = job.attempts + 1
        request = self._request(job, attempt)
        invocation_key = request.idempotency_key
        prior = self.repository.invocation(tenant_id, company_id, invocation_key)
        if prior and prior["state"] == "completed":
            return self._job(tenant_id, company_id, job_id)
        provider.validate_request(request)
        estimate = provider.estimate(request)
        try:
            if job.reserved_minor:
                if estimate.currency != job.per_job_ceiling.currency:
                    raise BudgetExceeded("queued estimate currency changed")
                if estimate.minor_units > job.reserved_minor:
                    raise BudgetExceeded("queued estimate exceeds reserved budget")
            else:
                self.budgets.reserve(job, estimate)
        except BudgetExceeded as exc:
            job.failure = CapabilityFailure("BUDGET_EXCEEDED", str(exc), False, FailureKind.BUDGET_BLOCKED)
            self._transition(job, JobStatus.FAILED, str(exc))
            self._emit(job, "budget.exhausted", {"reason": str(exc), "budget_ref": job.budget_ref})
            self._emit(job, "job.failed", {"failure_kind": FailureKind.BUDGET_BLOCKED.value})
            return job

        if not self.repository.begin_invocation(tenant_id, company_id, invocation_key, job_id):
            self.budgets.release(job, "Duplicate capability request suppressed")
            return self._job(tenant_id, company_id, job_id)
        job.attempts = attempt
        self._transition(job, JobStatus.RUNNING, f"Capability attempt {attempt} started")
        self._emit(job, "capability.requested", {"request_id": request.request_id, "attempt": attempt})
        self._emit(job, "job.started", {"attempt": attempt})
        try:
            result = provider.execute(request)
        except Exception as exc:
            result = None
            failure = CapabilityFailure("PROVIDER_EXCEPTION", str(exc)[:500], True, FailureKind.RETRYABLE)
        else:
            failure = result.failure

        if failure:
            self.repository.complete_invocation(tenant_id, company_id, invocation_key, result or failure)
            self.budgets.release(job, "Capability attempt failed before spend settlement")
            job.failure = failure
            self.audit.record(
                tenant_id=tenant_id,
                company_id=company_id,
                actor_type="capability",
                actor_id=job.capability.replace(".", "_"),
                action="capability.failed",
                target_type="job",
                target_id=job.job_id,
                correlation_id=job.correlation_id,
                reason=failure.safe_message,
                after={"attempt": attempt, "failure": asdict(failure)},
            )
            if failure.retryable and attempt < job.retry_policy.max_attempts:
                self._transition(job, JobStatus.RUNNABLE, "Retryable failure; bounded retry remains")
                self._emit(job, "job.attempt_failed", {"attempt": attempt, "retryable": True})
            else:
                self._transition(job, JobStatus.FAILED, "Capability failed with no retry remaining")
                self._emit(job, "job.failed", {"attempt": attempt, "failure_kind": failure.kind.value})
            return job

        assert result is not None
        for sequence, milestone in enumerate(result.progress, start=1):
            self._emit(
                job,
                "capability.progress",
                {"provider_ref": result.provider_ref, "milestone": milestone, "sequence": sequence},
            )
        try:
            self.budgets.settle(job, result.spend)
        except BudgetExceeded as exc:
            job.failure = CapabilityFailure("SPEND_OVERRUN", str(exc), False, FailureKind.BUDGET_BLOCKED)
            self.budgets.release(job, "Spend settlement refused closed")
            self._transition(job, JobStatus.FAILED, str(exc))
            self._emit(job, "job.failed", {"failure_kind": FailureKind.BUDGET_BLOCKED.value})
            return job
        job.provider_ref = result.provider_ref
        job.artifacts = result.artifacts
        self.repository.complete_invocation(tenant_id, company_id, invocation_key, result)
        self._transition(job, JobStatus.SUCCEEDED, "Capability completed and spend settled")
        self._emit(
            job,
            "capability.completed",
            {
                "provider_ref": result.provider_ref,
                "artifact_refs": [artifact.to_contract() for artifact in result.artifacts],
                "spend": result.spend.to_contract(),
                "progress": list(result.progress),
            },
        )
        self._emit(job, "job.completed", {"artifacts": [a.to_contract() for a in result.artifacts]})
        verification_payload = {
            "job_id": job.job_id,
            "artifact_refs": [artifact.to_contract() for artifact in result.artifacts],
        }
        self._emit(job, "verification.requested", verification_payload)
        self.verification.request_verification(
            tenant_id=tenant_id,
            company_id=company_id,
            job_id=job.job_id,
            artifact_refs=verification_payload["artifact_refs"],
            correlation_id=job.correlation_id,
        )
        return job

    def reserve_for_external_execution(self, job: Job) -> Money:
        """Reserve a runnable job before durable queue publication.

        The worker still owns invocation and every state transition. This method
        only closes the budget-before-queue gap for an external execution adapter.
        """
        job = self.refresh(job)
        if job.status is not JobStatus.RUNNABLE:
            raise PermissionError("only a Runtime-runnable job may be queued")
        if job.reserved_minor:
            return Money(job.per_job_ceiling.currency, job.reserved_minor)
        provider = self.registry.get(job.capability, job.capability_version)
        request = self._request(job, job.attempts + 1)
        provider.validate_request(request)
        estimate = provider.estimate(request)
        self.budgets.reserve(job, estimate)
        self.repository.save_job(job)
        return estimate

    def approve_job(
        self,
        *,
        tenant_id: str,
        company_id: str,
        job_id: str,
        approval_id: str,
        principal: object | None = None,
        actor_id: str | None = None,
        actor_role: str | None = None,
    ) -> Job:
        job = self._job(tenant_id, company_id, job_id)
        self.approvals.decide(
            tenant_id=tenant_id,
            company_id=company_id,
            approval_id=approval_id,
            decision=ApprovalState.GRANTED,
            principal=principal,
            correlation_id=job.correlation_id,
        )
        approval = self.repository.get_approval(tenant_id, company_id, approval_id)
        assert approval is not None
        event_type = "founder.approved" if approval.decided_by_role == "founder" else "approval.granted"
        self._emit(
            job,
            event_type,
            {"approval_id": approval_id, "actor_role": approval.decided_by_role},
        )
        return self.refresh(job)

    def cancel(self, tenant_id: str, company_id: str, job_id: str, actor_id: str = "runtime_operator") -> Job:
        job = self._job(tenant_id, company_id, job_id)
        if job.status == JobStatus.CANCELLED:
            return job
        if job.terminal:
            raise IllegalTransition(f"cannot cancel terminal job in {job.status.value}")
        if job.status == JobStatus.RUNNING and job.provider_ref:
            self.registry.get(job.capability, job.capability_version).cancel(job.provider_ref)
        self.budgets.release(job, "Job cancelled")
        job.failure = CapabilityFailure("CANCELLED", "Job was cancelled", False, FailureKind.CANCELLED)
        self._transition(job, JobStatus.CANCELLED, f"Cancelled by {actor_id}", actor_id=actor_id)
        self._emit(job, "job.cancelled", {"actor_id": actor_id})
        return job

    def transition_for_test(self, job: Job, target: JobStatus) -> Job:
        """Public state-machine assertion hook; performs the same guarded transition."""
        self._transition(job, target, "Explicit transition request")
        return job

    def _transition(self, job: Job, target: JobStatus, reason: str, actor_id: str = "runtime_orchestrator") -> None:
        if target == job.status:
            return
        if target not in TRANSITIONS[job.status]:
            raise IllegalTransition(f"illegal job transition {job.status.value} -> {target.value}")
        before = {"status": job.status.value, "version": job.version}
        job.status = target
        job.updated_at = self.clock()
        job.version += 1
        self.repository.save_job(job)
        self.audit.record(
            tenant_id=job.tenant_id,
            company_id=job.company_id,
            actor_type="system",
            actor_id=actor_id,
            action="job.state_transition",
            target_type="job",
            target_id=job.job_id,
            correlation_id=job.correlation_id,
            reason=reason,
            before=before,
            after={"status": target.value, "version": job.version},
        )

    def _dependency_state(self, job: Job) -> CapabilityFailure | None:
        for dependency_id in job.dependency_ids:
            dependency = self.repository.get_job(job.tenant_id, job.company_id, dependency_id)
            if dependency is None or dependency.status != JobStatus.SUCCEEDED:
                return CapabilityFailure(
                    "DEPENDENCY_BLOCKED",
                    f"Dependency {dependency_id} has not succeeded",
                    False,
                    FailureKind.DEPENDENCY_BLOCKED,
                )
        return None

    def _approval_state(self, job: Job) -> CapabilityFailure | None:
        if job.approval_mode == ApprovalMode.AUTONOMOUS:
            return None
        for approval_id in job.approval_ids:
            approval = self.repository.get_approval(job.tenant_id, job.company_id, approval_id)
            if approval is None or not self.approvals.effective(approval, job.subject_digest):
                return CapabilityFailure(
                    "APPROVAL_BLOCKED",
                    "A current digest-bound approval is required",
                    False,
                    FailureKind.APPROVAL_BLOCKED,
                )
        return None

    def _request(self, job: Job, attempt: int) -> CapabilityRequest:
        key = job.idempotency_key if attempt == 1 else f"{job.idempotency_key}:retry:{attempt}"
        inputs = dict(job.inputs)
        if isinstance(inputs.get("website_request"), dict):
            inputs["website_request"] = dict(inputs["website_request"], idempotency_key=key)
        return CapabilityRequest(
            request_id=f"request_{job.job_id}_{attempt}",
            tenant_id=job.tenant_id,
            company_id=job.company_id,
            job_id=job.job_id,
            capability=job.capability,
            capability_version=job.capability_version,
            idempotency_key=key,
            correlation_id=job.correlation_id,
            inputs=inputs,
            budget_ref=job.budget_ref,
        )

    def _emit(self, job: Job, event_type: str, payload: dict[str, Any]) -> Event:
        event = Event(
            event_id=self.id_factory("event"),
            tenant_id=job.tenant_id,
            company_id=job.company_id,
            correlation_id=job.correlation_id,
            causation_id=job.causation_id,
            type=event_type,
            occurred_at=self.clock(),
            payload={"job_id": job.job_id, **payload},
            source="businessbuilder.runtime",
        )
        self.events.publish(event)
        return event

    def _job(self, tenant_id: str, company_id: str, job_id: str) -> Job:
        job = self.repository.get_job(tenant_id, company_id, job_id)
        if not job:
            raise LookupError("job not found in tenant/company scope")
        return job
