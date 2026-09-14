from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Callable

from businessbuilder.ai_workforce import ActionRequest, PolicyDecision, RoleState, WorkforcePolicyService
from businessbuilder.commercial import EntitlementClass, EntitlementStatus, ProductCode
from businessbuilder.commercial.repository import CommercialRepository
from businessbuilder.company_brain import CompanyBrainService, LifecycleState, Scope
from businessbuilder.company_brain.errors import CompanyBrainError
from businessbuilder.identity import (
    AuthenticatedPrincipal,
    AuthorizationContext,
    AuthorizationPolicy,
    IdentityRepository,
    IdentityError,
    Permission,
    PrincipalContextAuthority,
)
from businessbuilder.runtime import ApprovalMode, ArtifactRef, Event, Job, JobStatus, Money, RetryPolicy
from businessbuilder.runtime.budgets import BudgetExceeded
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.runtime.storage import RuntimeRepository
from businessbuilder.access_broker.models import JobSecretRef

from .model_router import ModelRouter, NoEligibleModel
from .models import (
    AgentJobEnvelope,
    DeliveryState,
    ExecutionRecord,
    ExecutionState,
    ModelCandidate,
    ModelPolicy,
    QueueOutboxRecord,
    TriggerClass,
    assert_queue_payload_safe,
)
from .queue import QueuePort


ROLE_ENTITLEMENTS: dict[str, tuple[str, ...]] = {
    "role_intake_assistant": ("ai_workforce.execute",),
    "role_quote_drafting_assistant": ("ai_workforce.execute",),
    "role_inbox_assistant": ("ai_workforce.execute", "inbox.autonomous"),
    "role_review_followup_assistant": ("ai_workforce.execute", "outreach.scheduled"),
}

ACTIVE_COMPANY_STATES = frozenset(
    {LifecycleState.READY, LifecycleState.FULLY_SET, LifecycleState.OPERATING}
)


class AgentAdmissionDenied(PermissionError):
    pass


class SimulatedWorkerCrash(RuntimeError):
    """Test-only crash signal; production callers never request it."""


class EntitlementGate:
    def __init__(self, repository: CommercialRepository, *, clock: Callable[[], datetime]) -> None:
        self.repository = repository
        self.clock = clock

    def require(
        self,
        tenant_id: str,
        company_id: str,
        role_id: str,
        *,
        draining_envelope: AgentJobEnvelope | None = None,
    ) -> tuple[str, ...]:
        required = ROLE_ENTITLEMENTS.get(role_id)
        if not required:
            raise AgentAdmissionDenied("agent role has no managed-operation entitlement mapping")
        now = self.clock()
        eligible: dict[str, str] = {}
        for grant in self.repository.get_current_entitlement_grants(tenant_id, company_id):
            if grant.entitlement_class is not EntitlementClass.STROMATION_MANAGED:
                continue
            order = self.repository.get_order(tenant_id, company_id, grant.source_order_id)
            if not any(item.product_code is ProductCode.BUILD_AND_RUN for item in order.items):
                continue
            active = grant.status is EntitlementStatus.ACTIVE
            draining = (
                draining_envelope is not None
                and grant.status is EntitlementStatus.EXPIRING
                and grant.effective_until is not None
                and now < grant.effective_until
                and draining_envelope.created_at < grant.updated_at
            )
            if active or draining:
                eligible[grant.entitlement_code] = grant.grant_id
        missing = tuple(code for code in required if code not in eligible)
        if missing:
            raise AgentAdmissionDenied("active Build & Run entitlement does not include requested capability")
        return tuple(eligible[code] for code in required)


class AgentRuntimeService:
    """Runtime-owned admission and delivery boundary for bounded agent jobs."""

    def __init__(
        self,
        *,
        runtime: JobOrchestrator,
        repository: RuntimeRepository,
        identity_repository: IdentityRepository,
        principal_authority: PrincipalContextAuthority,
        commercial_repository: CommercialRepository,
        company_brain: CompanyBrainService,
        workforce: WorkforcePolicyService,
        model_router: ModelRouter,
        model_candidates: Callable[[str], tuple[ModelCandidate, ...]],
        clock: Callable[[], datetime],
        id_factory: Callable[[str], str],
    ) -> None:
        self.runtime = runtime
        self.repository = repository
        self.identity_repository = identity_repository
        self.principal_authority = principal_authority
        self.authorization = AuthorizationPolicy(identity_repository)
        self.commercial_repository = commercial_repository
        self.company_brain = company_brain
        self.workforce = workforce
        self.model_router = model_router
        self.model_candidates = model_candidates
        self.clock = clock
        self.id_factory = id_factory
        self.entitlements = EntitlementGate(commercial_repository, clock=clock)

    def submit(
        self,
        *,
        tenant_id: str,
        company_id: str,
        role_id: str,
        capability: str,
        action: str,
        budget_ref: str,
        maximum_job_spend: Money,
        idempotency_key: str,
        correlation_id: str,
        trigger_class: TriggerClass,
        trigger_ref: str,
        principal: AuthenticatedPrincipal | None = None,
        causation_id: str | None = None,
        input_artifact_refs: tuple[ArtifactRef, ...] = (),
        secret_refs: tuple[JobSecretRef, ...] = (),
        context_flags: frozenset[str] = frozenset(),
        model_policy: ModelPolicy = ModelPolicy(quality_floor=70),
        expires_in: timedelta = timedelta(minutes=15),
        retry_policy: RetryPolicy = RetryPolicy(),
    ) -> Job:
        if len(idempotency_key) < 16:
            raise ValueError("idempotency_key must be at least 16 characters")
        if expires_in <= timedelta(0) or expires_in > timedelta(hours=24):
            raise ValueError("agent job expiry must be within 24 hours")
        self._assert_scope_active(tenant_id, company_id)
        actor_id = self._authorize_trigger(
            tenant_id, company_id, trigger_class, trigger_ref, principal
        )
        try:
            entitlement_refs = self.entitlements.require(tenant_id, company_id, role_id)
        except AgentAdmissionDenied as exc:
            self._audit_denial(
                tenant_id, company_id, actor_id, role_id, capability,
                "build_and_run_entitlement_denied", correlation_id,
            )
            raise
        role = self.workforce.repository.current_definition(tenant_id, company_id, role_id)
        if role is None or role.state is not RoleState.ACTIVE:
            self._audit_denial(
                tenant_id, company_id, actor_id, role_id, capability,
                "workforce_role_inactive", correlation_id,
            )
            raise AgentAdmissionDenied("AI Workforce role is not active in this company scope")
        evaluation = self.workforce.evaluate(
            ActionRequest(
                request_id=f"request_{_stable(idempotency_key)}",
                idempotency_key=f"policy_{idempotency_key}",
                tenant_id=tenant_id,
                company_id=company_id,
                role_id=role_id,
                role_version=role.version,
                capability=capability,
                action=action,
                estimated_minor=maximum_job_spend.minor_units,
                currency=maximum_job_spend.currency,
                context_flags=context_flags,
            )
        )
        if evaluation.decision not in {PolicyDecision.ALLOW, PolicyDecision.APPROVAL_REQUIRED}:
            self._audit_denial(
                tenant_id, company_id, actor_id, role_id, capability,
                evaluation.reason_code, correlation_id,
            )
            raise AgentAdmissionDenied("AI Workforce policy denied managed execution")
        try:
            selection = self.model_router.select(
                self.model_candidates(capability),
                model_policy,
                capability=capability,
                remaining_budget=maximum_job_spend,
            )
        except NoEligibleModel:
            self._audit_denial(
                tenant_id, company_id, actor_id, role_id, capability,
                "model_policy_denied", correlation_id,
            )
            raise
        if selection.estimated_cost.minor_units > maximum_job_spend.minor_units:
            raise AgentAdmissionDenied("eligible model exceeds maximum job spend")
        approval_mode = (
            ApprovalMode.FOUNDER_ONLY
            if evaluation.decision is PolicyDecision.APPROVAL_REQUIRED
            else ApprovalMode.AUTONOMOUS
        )
        now = self.clock()
        try:
            with self.repository.transaction():
                job = self.runtime.create_job(
                tenant_id=tenant_id,
                company_id=company_id,
                capability=capability,
                inputs={
                    "objective": f"Bounded {role.name} action: {action}",
                    "agent_role": role_id,
                    "agent_role_version": role.version,
                    "action": action,
                    "policy_evaluation_id": evaluation.evaluation_id,
                    "model_provider": selection.provider,
                    "model": selection.model,
                    "model_estimated_minor": selection.estimated_cost.minor_units,
                    "input_artifact_refs": [item.to_contract() for item in input_artifact_refs],
                    "secret_refs": [item.to_contract() for item in secret_refs],
                    "trigger_ref": trigger_ref,
                },
                budget_ref=budget_ref,
                per_job_ceiling=maximum_job_spend,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                causation_id=causation_id,
                approval_mode=approval_mode,
                retry_policy=retry_policy,
                provenance={"source": "businessbuilder.agent_runtime", "trigger": trigger_class.value},
            )
                existing = self.repository.get_agent_envelope(tenant_id, company_id, job.job_id)
                if existing:
                    return job
                if job.status is JobStatus.WAITING_APPROVAL:
                    self._audit_admission(job, actor_id, role_id, capability, entitlement_refs, evaluation, None)
                    return job
                reserved = self.runtime.reserve_for_external_execution(job)
                envelope = self._envelope(
                    job, role_id, role.version, action, entitlement_refs, evaluation,
                    selection, model_policy, input_artifact_refs, trigger_class,
                    trigger_ref, actor_id, now, expires_in, reserved, secret_refs,
                )
                self.repository.save_agent_admission(
                    envelope,
                    ExecutionRecord(
                        tenant_id, company_id, job.job_id, envelope.envelope_digest,
                        ExecutionState.QUEUED, 0, now, now,
                    ),
                    QueueOutboxRecord(
                        f"queue_{_stable(job.job_id)}", f"queue:{job.idempotency_key}",
                        tenant_id, company_id, job.job_id, envelope.envelope_digest,
                        DeliveryState.PENDING, 0, now, now,
                    ),
                )
                self._audit_admission(
                    job, actor_id, role_id, capability, entitlement_refs, evaluation, selection
                )
                return job
        except BudgetExceeded:
            self._audit_denial(
                tenant_id, company_id, actor_id, role_id, capability,
                "runtime_budget_denied", correlation_id,
            )
            raise

    def revalidate(self, envelope: AgentJobEnvelope) -> None:
        self._assert_scope_active(envelope.tenant_id, envelope.company_id)
        persisted = self.repository.get_agent_envelope(
            envelope.tenant_id, envelope.company_id, envelope.job_id
        )
        if persisted is None or persisted.envelope_digest != envelope.envelope_digest:
            raise AgentAdmissionDenied("queue envelope is forged or stale")
        if self.clock() >= envelope.expires_at:
            raise AgentAdmissionDenied("agent job envelope expired")
        job = self.repository.get_job(envelope.tenant_id, envelope.company_id, envelope.job_id)
        if (
            job is None
            or job.company_id != envelope.company_id
            or job.tenant_id != envelope.tenant_id
            or job.capability != envelope.capability
            or job.idempotency_key != envelope.idempotency_key
        ):
            raise AgentAdmissionDenied("queue envelope does not match the canonical Runtime job")
        refs = self.entitlements.require(
            envelope.tenant_id,
            envelope.company_id,
            envelope.agent_role,
            draining_envelope=envelope,
        )
        if refs != envelope.entitlement_refs:
            raise AgentAdmissionDenied("entitlement authority changed after queue admission")
        role = self.workforce.repository.current_definition(
            envelope.tenant_id, envelope.company_id, envelope.agent_role
        )
        if (
            role is None
            or role.state is not RoleState.ACTIVE
            or role.version != envelope.agent_role_version
            or role.definition_digest != envelope.policy_definition_digest
        ):
            raise AgentAdmissionDenied("AI Workforce policy authority changed after queue admission")
        if envelope.trigger_class is TriggerClass.CUSTOMER_REQUEST:
            self.authorization.require(
                AuthorizationContext(envelope.triggered_by, envelope.tenant_id, envelope.company_id),
                Permission.INTERACT_AI_WORKFORCE,
                at=self.clock(),
            )
        for approval_id in envelope.approval_refs:
            approval = self.repository.get_approval(
                envelope.tenant_id, envelope.company_id, approval_id
            )
            if approval is None or not approval.is_effective(self.clock(), job.subject_digest):
                raise AgentAdmissionDenied("required Runtime approval is no longer effective")

    def cancel_ineligible(self, tenant_id: str, company_id: str) -> int:
        cancelled = 0
        for execution in self.repository.list_agent_executions(tenant_id, company_id):
            if execution.state not in {ExecutionState.QUEUED, ExecutionState.RETRYABLE}:
                continue
            envelope = self.repository.get_agent_envelope(tenant_id, company_id, execution.job_id)
            if envelope is None:
                continue
            try:
                self.revalidate(envelope)
            except (AgentAdmissionDenied, PermissionError, LookupError):
                job = self.runtime.cancel(tenant_id, company_id, execution.job_id, actor_id="agent_entitlement_gate")
                self.repository.save_agent_execution(
                    replace(execution, state=ExecutionState.CANCELLED, updated_at=self.clock(), outcome=job.status.value)
                )
                cancelled += 1
        return cancelled

    def _assert_scope_active(self, tenant_id: str, company_id: str) -> None:
        tenant = self.identity_repository.get_tenant(tenant_id)
        organization = self.identity_repository.get_organization_by_tenant(tenant_id)
        if tenant.status.value != "active" or organization.status.value != "active":
            raise AgentAdmissionDenied("tenant or organization is suspended")
        if company_id not in organization.company_ids:
            raise AgentAdmissionDenied("company is outside tenant")
        company = self.company_brain.get_company(Scope(tenant_id, company_id))
        if company.lifecycle not in ACTIVE_COMPANY_STATES:
            raise AgentAdmissionDenied("company is not active for managed operations")

    def _authorize_trigger(self, tenant_id, company_id, trigger_class, trigger_ref, principal):
        if trigger_class is TriggerClass.CUSTOMER_REQUEST:
            verified = self.principal_authority.verify(
                principal, tenant_id=tenant_id, company_id=company_id
            )
            self.authorization.require(
                AuthorizationContext(verified.user_id, tenant_id, company_id),
                Permission.INTERACT_AI_WORKFORCE,
                at=self.clock(),
            )
            return verified.user_id
        events = self.repository.list_events(tenant_id, company_id)
        event = next((item for item in events if item["event_id"] == trigger_ref), None)
        if event is None:
            raise AgentAdmissionDenied("internal trigger is not a persisted canonical event")
        return event["source"]

    def _envelope(
        self, job, role_id, role_version, action, entitlement_refs, evaluation,
        selection, model_policy, input_artifact_refs, trigger_class, trigger_ref,
        actor_id, now, expires_in, reserved,
        secret_refs,
    ):
        return AgentJobEnvelope(
            job.tenant_id, job.company_id, job.job_id, job.correlation_id,
            job.causation_id, job.capability, job.capability_version, action,
            role_id, role_version, entitlement_refs,
            (Permission.INTERACT_AI_WORKFORCE.value, f"{job.capability}:{action}"),
            job.approval_ids, reserved, job.per_job_ceiling, model_policy, selection,
            input_artifact_refs, 1, job.retry_policy.max_attempts, job.idempotency_key,
            trigger_class, trigger_ref, actor_id, evaluation.evaluation_id,
            evaluation.definition_digest or "", now, now + expires_in,
            secret_refs,
        )

    def _audit_admission(self, job, actor_id, role_id, capability, entitlement_refs, evaluation, selection):
        self.runtime.audit.record(
            tenant_id=job.tenant_id,
            company_id=job.company_id,
            actor_type="user" if actor_id.startswith("user_") else "event",
            actor_id=actor_id,
            action="agent.execution.admitted",
            target_type="job",
            target_id=job.job_id,
            correlation_id=job.correlation_id,
            reason="Runtime admitted bounded AI Workforce execution",
            details={
                "trigger_class": job.provenance.get("trigger"),
                "trigger_ref": job.inputs.get("trigger_ref"),
                "agent_role": role_id,
                "capability": capability,
                "entitlement_refs": list(entitlement_refs),
                "approval_refs": list(job.approval_ids),
                "maximum_job_spend": job.per_job_ceiling.to_contract(),
                "reserved_budget_minor": job.reserved_minor,
                "policy_evaluation_id": evaluation.evaluation_id,
                "provider": selection.provider if selection else None,
                "model": selection.model if selection else None,
                "status": job.status.value,
            },
            after={"policy_evaluation_id": evaluation.evaluation_id, "status": job.status.value},
        )

    def _audit_denial(self, tenant_id, company_id, actor_id, role_id, capability, reason, correlation_id):
        self.runtime.audit.record(
            tenant_id=tenant_id,
            company_id=company_id,
            actor_type="user",
            actor_id=actor_id,
            action="authorization.denied",
            target_type="agent_role",
            target_id=role_id,
            correlation_id=correlation_id,
            reason=reason,
            details={"capability": capability, "source": "businessbuilder.agent_runtime"},
            after={"capability": capability},
        )


class AgentOutboxDispatcher:
    def __init__(self, repository, queue: QueuePort, *, dispatcher_id: str, clock, lease=timedelta(seconds=30)):
        self.repository = repository
        self.queue = queue
        self.dispatcher_id = dispatcher_id
        self.clock = clock
        self.lease = lease

    def dispatch_pending(self, *, limit: int = 100) -> int:
        count = 0
        while count < limit:
            claimed = self.repository.claim_agent_outbox(
                self.dispatcher_id, at=self.clock(), lease=self.lease, limit=1
            )
            if not claimed:
                break
            message = claimed[0]
            envelope = self.repository.get_agent_envelope(
                message.tenant_id, message.company_id, message.job_id
            )
            if envelope is None or envelope.envelope_digest != message.envelope_digest:
                self.repository.release_agent_outbox(
                    message.message_id, self.dispatcher_id,
                    retry_at=self.clock() + timedelta(seconds=5), error="EnvelopeMismatch",
                )
                raise RuntimeError("outbox envelope is missing or inconsistent")
            payload = envelope.to_payload()
            assert_queue_payload_safe(payload)
            try:
                self.queue.send(message.message_id, payload)
            except Exception as exc:
                self.repository.release_agent_outbox(
                    message.message_id, self.dispatcher_id,
                    retry_at=self.clock() + timedelta(seconds=5), error=type(exc).__name__,
                )
                raise
            self.repository.acknowledge_agent_outbox(
                message.message_id, self.dispatcher_id, at=self.clock()
            )
            count += 1
        return count


class SharedAgentWorker:
    def __init__(self, *, service: AgentRuntimeService, queue: QueuePort, worker_id: str, clock, lease=timedelta(seconds=60)):
        self.service = service
        self.queue = queue
        self.worker_id = worker_id
        self.clock = clock
        self.lease = lease

    def process_one(self, *, crash_point: str | None = None, wait_seconds: int = 0) -> str:
        delivery = self.queue.receive(
            wait_seconds=wait_seconds,
            visibility_timeout=int(self.lease.total_seconds()),
        )
        if delivery is None:
            return "empty"
        try:
            envelope = AgentJobEnvelope.from_payload(delivery.payload)
            expected_message = f"queue_{_stable(envelope.job_id)}"
            if delivery.message_id != expected_message:
                raise AgentAdmissionDenied("queue message identity does not match envelope")
            self.service.revalidate(envelope)
            execution = self.service.repository.lease_agent_execution(
                envelope.tenant_id, envelope.company_id, envelope.job_id,
                envelope.envelope_digest, self.worker_id, at=self.clock(), lease=self.lease,
            )
            if execution is None:
                persisted = self.service.repository.get_agent_execution(
                    envelope.tenant_id, envelope.company_id, envelope.job_id
                )
                if persisted and persisted.state in {ExecutionState.SUCCEEDED, ExecutionState.CANCELLED}:
                    self.queue.acknowledge(delivery.receipt_handle)
                    return "duplicate"
                self.queue.release(delivery.receipt_handle, delay_seconds=1)
                return "leased"
            if crash_point == "before_execution":
                raise SimulatedWorkerCrash("simulated crash before execution")
            self.queue.renew(delivery.receipt_handle, visibility_timeout=int(self.lease.total_seconds()))
            started = self.clock()
            self.service.runtime.audit.record(
                tenant_id=envelope.tenant_id, company_id=envelope.company_id,
                actor_type="system", actor_id=self.worker_id,
                action="agent.execution.started", target_type="job", target_id=envelope.job_id,
                correlation_id=envelope.correlation_id, reason="Shared worker started one bounded job",
                details={
                    "agent_role": envelope.agent_role, "capability": envelope.capability,
                    "attempt": execution.attempts, "provider": envelope.model_selection.provider,
                    "model": envelope.model_selection.model, "entitlement_refs": list(envelope.entitlement_refs),
                    "approval_refs": list(envelope.approval_refs),
                    "reserved_budget": envelope.reserved_budget.to_contract(),
                },
                after={"attempt": execution.attempts, "job_id": envelope.job_id},
            )
            job = self.service.runtime.run(envelope.tenant_id, envelope.company_id, envelope.job_id)
            ended = self.clock()
            if job.status is JobStatus.SUCCEEDED:
                event_id = f"event_{_stable('agent-result:' + job.job_id)}"
                self.service.runtime.events.publish(
                    Event(
                        event_id, job.tenant_id, job.company_id, job.correlation_id,
                        job.job_id, "agent.execution.completed", ended,
                        {
                            "job_id": job.job_id, "agent_role": envelope.agent_role,
                            "capability": envelope.capability,
                            "artifact_refs": [item.to_contract() for item in job.artifacts],
                            "cost": {"currency": job.per_job_ceiling.currency, "minor_units": job.settled_minor},
                        },
                        "businessbuilder.runtime",
                    )
                )
                completed = replace(
                    execution, state=ExecutionState.SUCCEEDED, updated_at=ended,
                    lease_owner=None, lease_until=None,
                    provider=envelope.model_selection.provider,
                    model=envelope.model_selection.model,
                    started_at=started, ended_at=ended,
                    actual_cost=Money(job.per_job_ceiling.currency, job.settled_minor),
                    outcome="succeeded", emitted_event_ids=(event_id,), artifact_refs=job.artifacts,
                )
                self.service.repository.save_agent_execution(completed)
                self.service.runtime.audit.record(
                    tenant_id=job.tenant_id, company_id=job.company_id,
                    actor_type="system", actor_id=self.worker_id,
                    action="agent.execution.completed", target_type="job", target_id=job.job_id,
                    correlation_id=job.correlation_id, reason="Bounded agent job completed",
                    details={
                        "agent_role": envelope.agent_role, "capability": envelope.capability,
                        "attempts": job.attempts, "provider": completed.provider, "model": completed.model,
                        "actual_cost": completed.actual_cost.to_contract(), "outcome": completed.outcome,
                        "emitted_event_ids": list(completed.emitted_event_ids),
                        "artifact_refs": [item.to_contract() for item in completed.artifact_refs],
                    },
                    after={"outcome": completed.outcome, "event_ids": list(completed.emitted_event_ids)},
                )
                if crash_point == "after_execution":
                    raise SimulatedWorkerCrash("simulated crash after execution before acknowledgment")
                self.queue.acknowledge(delivery.receipt_handle)
                return "succeeded"
            retryable = job.status is JobStatus.RUNNABLE
            self.service.repository.save_agent_execution(
                replace(
                    execution,
                    state=ExecutionState.RETRYABLE if retryable else ExecutionState.FAILED,
                    updated_at=ended,
                    lease_owner=None,
                    lease_until=None,
                    started_at=started,
                    ended_at=ended,
                    outcome=job.status.value,
                    failure_classification=job.failure.kind.value if job.failure else "unknown",
                )
            )
            self.queue.release(delivery.receipt_handle, delay_seconds=1)
            return "retry" if retryable else "failed"
        except SimulatedWorkerCrash:
            raise
        except (
            AgentAdmissionDenied,
            PermissionError,
            LookupError,
            ValueError,
            IdentityError,
            CompanyBrainError,
        ) as exc:
            if "envelope" in locals():
                persisted = self.service.repository.get_agent_envelope(
                    envelope.tenant_id, envelope.company_id, envelope.job_id
                )
                if persisted and persisted.envelope_digest == envelope.envelope_digest:
                    self.service.runtime.audit.record(
                        tenant_id=envelope.tenant_id,
                        company_id=envelope.company_id,
                        actor_type="system",
                        actor_id=self.worker_id,
                        action="authorization.denied",
                        target_type="job",
                        target_id=envelope.job_id,
                        correlation_id=envelope.correlation_id,
                        reason=type(exc).__name__,
                        permission="agent.execute",
                        source="businessbuilder.agent_runtime.worker",
                    )
                    job = self.service.repository.get_job(
                        envelope.tenant_id, envelope.company_id, envelope.job_id
                    )
                    if job and not job.terminal:
                        self.service.runtime.cancel(
                            envelope.tenant_id, envelope.company_id, envelope.job_id,
                            actor_id="agent_execution_gate",
                        )
                    execution = self.service.repository.get_agent_execution(
                        envelope.tenant_id, envelope.company_id, envelope.job_id
                    )
                    if execution and execution.state not in {
                        ExecutionState.SUCCEEDED,
                        ExecutionState.CANCELLED,
                    }:
                        self.service.repository.save_agent_execution(
                            replace(
                                execution,
                                state=ExecutionState.CANCELLED,
                                updated_at=self.clock(),
                                lease_owner=None,
                                lease_until=None,
                                outcome="denied",
                                failure_classification=type(exc).__name__,
                            )
                        )
            self.queue.acknowledge(delivery.receipt_handle)
            return "denied"


def _stable(value: str) -> str:
    return sha256(value.encode()).hexdigest()[:24]
