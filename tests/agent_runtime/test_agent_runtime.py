from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from businessbuilder.agent_runtime import (
    AgentAdmissionDenied,
    AgentJobEnvelope,
    AgentOutboxDispatcher,
    AgentRuntimeService,
    DeterministicAgentCapability,
    ExecutionState,
    InMemoryQueue,
    ModelCandidate,
    ModelPolicy,
    ModelRouter,
    NoEligibleModel,
    RuntimeSchedule,
    RuntimeScheduler,
    SharedAgentWorker,
    SimulatedWorkerCrash,
    TriggerClass,
)
from businessbuilder.ai_workforce import (
    InMemoryWorkforceRepository,
    RoleState,
    WorkforcePolicyService,
    safe_role_definitions,
)
from businessbuilder.commercial import (
    Amount,
    BillingPeriod,
    CommercialService,
    EntitlementClass,
    EntitlementStatus,
    InMemoryCommercialRepository,
    NormalizedBillingEvent,
    ProductCode,
    RecordingCommercialEventSink,
    SubscriptionStatus,
    seed_default_catalog,
)
from businessbuilder.company_brain import (
    Company,
    CompanyBrainService,
    EntityRef,
    LifecycleState,
    NotFoundError,
    Provenance,
    Scope,
    SQLiteCompanyBrainRepository,
)
from businessbuilder.identity import (
    AuthorizationContext,
    FakeDevAuthenticationProvider,
    IdentityService,
    PrincipalContextAuthority,
    SessionService,
    SQLiteIdentityRepository,
)
from businessbuilder.integration import (
    CompanyBrainRuntimeAdapter,
    IdentityApprovalPrincipalVerifier,
)
from businessbuilder.runtime import (
    ArtifactRef,
    Budget,
    CapabilityRegistry,
    Event,
    JobStatus,
    Money,
    RetryPolicy,
    SQLiteRuntimeRepository,
)
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.runtime.budgets import BudgetExceeded
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.runtime.ports import RecordingVerificationPort


NOW = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.clock = MutableClock()
        self.ids = DeterministicIds()
        self.identity_repository = SQLiteIdentityRepository(str(root / "identity.sqlite"))
        self.identity = IdentityService(
            self.identity_repository, id_factory=self.ids, clock=self.clock
        )
        self.owner, _ = self.identity.register_founder(
            "agent-owner@example.test", "Agent Runtime Owner"
        )
        self.tenant, self.organization, _ = self.identity.create_account(
            self.owner.user_id, "Agent Runtime Organization"
        )
        self.company_id = "company_agent_runtime"
        self.identity.attach_company(
            AuthorizationContext(self.owner.user_id, self.tenant.tenant_id),
            self.company_id,
        )
        provider = FakeDevAuthenticationProvider()
        provider.register(self.owner.user_id, self.owner.email, "test-session-proof")
        sessions = SessionService(
            self.identity_repository, provider, id_factory=self.ids, clock=self.clock
        )
        _, token = sessions.sign_in(self.owner.email, "test-session-proof")
        self.authority = PrincipalContextAuthority(
            self.identity_repository,
            clock=self.clock,
            signing_key=b"agent-runtime-test-signing-key-32-bytes",
        )
        self.principal = self.authority.issue(
            token, tenant_id=self.tenant.tenant_id, company_id=self.company_id
        )

        self.brain_repository = SQLiteCompanyBrainRepository(str(root / "brain.sqlite"))
        self.brain_repository.migrate()
        self.brain = CompanyBrainService(self.brain_repository)
        owner_ref = EntityRef("user", self.owner.user_id)
        self.brain.create_company(
            Company(
                Scope(self.tenant.tenant_id, self.company_id),
                "Agent Runtime Test Company",
                "mobile_service",
                {"country": "US", "region": "TX"},
                (owner_ref,),
                lifecycle=LifecycleState.OPERATING,
                provenance=(
                    Provenance(
                        "test", NOW.isoformat().replace("+00:00", "Z"), owner_ref,
                        source_ref="test://agent-runtime",
                    ),
                ),
            )
        )

        self.commercial_repository = InMemoryCommercialRepository()
        seed_default_catalog(self.commercial_repository, effective_at=NOW)
        self.commercial = CommercialService(
            self.commercial_repository,
            self.identity.authorization,
            RecordingCommercialEventSink(),
            id_factory=self.ids,
            clock=self.clock,
        )
        self.context = AuthorizationContext(
            self.owner.user_id, self.tenant.tenant_id, self.company_id
        )
        self.order, self.subscription = self._activate_build_and_run()

        self.workforce_repository = InMemoryWorkforceRepository()
        for role in safe_role_definitions(
            self.tenant.tenant_id, self.company_id, created_at=NOW
        ):
            self.workforce_repository.add_definition(role)
        self.workforce = WorkforcePolicyService(
            self.workforce_repository, clock=self.clock, id_factory=self.ids
        )

        self.runtime_repository = SQLiteRuntimeRepository(str(root / "runtime.sqlite"))
        self.registry = CapabilityRegistry()
        self.email_capability = DeterministicAgentCapability("communications.email")
        self.quote_capability = DeterministicAgentCapability("customer.quote")
        self.intake_capability = DeterministicAgentCapability("customer.intake")
        for capability in (
            self.email_capability, self.quote_capability, self.intake_capability
        ):
            self.registry.register(capability)
        self.runtime = JobOrchestrator(
            repository=self.runtime_repository,
            registry=self.registry,
            company_reader=CompanyBrainRuntimeAdapter(self.brain),
            verification=RecordingVerificationPort(),
            id_factory=self.ids,
            clock=self.clock,
            approval_principals=IdentityApprovalPrincipalVerifier(self.authority),
        )
        self.runtime.budgets.create(
            Budget(
                "budget_agent_runtime", self.tenant.tenant_id, self.company_id,
                Money("USD", 100),
            ),
            "correlation-agent-budget",
        )
        self.service = AgentRuntimeService(
            runtime=self.runtime,
            repository=self.runtime_repository,
            identity_repository=self.identity_repository,
            principal_authority=self.authority,
            commercial_repository=self.commercial_repository,
            company_brain=self.brain,
            workforce=self.workforce,
            model_router=ModelRouter(),
            model_candidates=self._models,
            clock=self.clock,
            id_factory=self.ids,
        )
        self.queue = InMemoryQueue(max_receive_count=2)
        self.dispatcher = AgentOutboxDispatcher(
            self.runtime_repository, self.queue,
            dispatcher_id="dispatcher-test", clock=self.clock,
        )
        self.worker = SharedAgentWorker(
            service=self.service, queue=self.queue,
            worker_id="shared-worker-test", clock=self.clock,
        )

    def tearDown(self) -> None:
        self.runtime_repository.close()
        self.brain_repository.close()
        self.identity_repository.close()
        self.temp.cleanup()

    def _activate_build_and_run(self):
        version = "product_version_build_and_run_v1"
        order = self.commercial.create_order(
            self.context, version, amount=Amount("USD", 1000)
        )
        checkout = self.commercial.create_checkout(
            self.context, order.order_id, "agent-runtime-checkout"
        )
        payment = NormalizedBillingEvent(
            "billing_event_agent_payment", "billing.payment.succeeded",
            self.tenant.tenant_id, self.owner.user_id, self.company_id,
            self.clock.now, "fixture_pay", "provider_event_agent_payment",
            "correlation-agent-payment", order_id=order.order_id,
            checkout_intent_id=checkout.checkout_intent_id,
            payment_provider_ref="opaque_agent_payment", amount=order.total,
        )
        self.commercial.handle_billing_event(payment)
        period = BillingPeriod(self.clock.now, self.clock.now + timedelta(days=30))
        recurring = NormalizedBillingEvent(
            "billing_event_agent_subscription", "billing.subscription.created",
            self.tenant.tenant_id, self.owner.user_id, self.company_id,
            self.clock.now, "fixture_pay", "provider_event_agent_subscription",
            "correlation-agent-subscription", order_id=order.order_id,
            subscription_provider_ref="opaque_agent_subscription",
            subscription_status=SubscriptionStatus.ACTIVE,
            current_period=period,
        )
        self.commercial.handle_billing_event(recurring)
        return order, self.commercial_repository.get_subscription_by_provider_ref(
            self.tenant.tenant_id, self.company_id, "opaque_agent_subscription"
        )

    def _models(self, capability: str):
        eligible_cost = 3 if capability == "communications.email" else 0
        return (
            ModelCandidate(
                "cheap-but-unsafe", "low-quality", frozenset({capability}), 40,
                True, True, True, 10, Money("USD", 1),
            ),
            ModelCandidate(
                "deterministic-test", "bounded-v1", frozenset({capability, "tools"}), 90,
                True, True, True, 25, Money("USD", eligible_cost),
            ),
        )

    def _inbound_event(self, event_id="event_inbound_lead_test"):
        event = Event(
            event_id, self.tenant.tenant_id, self.company_id,
            "correlation-inbound-lead", None, "integration.lead.received",
            self.clock.now, {"artifact_ref": "artifact_lead_test"},
            "businessbuilder.integration.test",
        )
        self.runtime.events.publish(event)
        return event

    def _submit_inbox(self, *, event=None, key="agent-runtime-inbox-0001", retry_policy=RetryPolicy()):
        event = event or self._inbound_event()
        return self.service.submit(
            tenant_id=self.tenant.tenant_id,
            company_id=self.company_id,
            role_id="role_inbox_assistant",
            capability="communications.email",
            action="classify_message",
            budget_ref="budget_agent_runtime",
            maximum_job_spend=Money("USD", 5),
            idempotency_key=key,
            correlation_id=event.correlation_id,
            trigger_class=TriggerClass.INBOUND_EVENT,
            trigger_ref=event.event_id,
            causation_id=event.event_id,
            input_artifact_refs=(ArtifactRef("lead", "artifact_lead_test"),),
            retry_policy=retry_policy,
        )

    def _set_managed_status(self, status: EntitlementStatus, *, effective_until=None):
        for grant in self.commercial_repository.get_current_entitlement_grants(
            self.tenant.tenant_id, self.company_id
        ):
            if grant.entitlement_class is EntitlementClass.STROMATION_MANAGED:
                self.commercial_repository.append_entitlement_grant(
                    replace(
                        grant, status=status, effective_until=effective_until,
                        updated_at=self.clock.now, version=grant.version + 1,
                    )
                )

    def test_active_entitlement_runs_inbox_job_through_queue(self) -> None:
        job = self._submit_inbox()
        envelope = self.runtime_repository.get_agent_envelope(
            self.tenant.tenant_id, self.company_id, job.job_id
        )
        self.assertEqual(JobStatus.RUNNABLE, job.status)
        self.assertEqual(3, envelope.reserved_budget.minor_units)
        self.assertEqual("role_inbox_assistant", envelope.agent_role)
        self.assertEqual(("ai_workforce.interact", "communications.email:classify_message"), envelope.permission_scope)
        self.assertNotIn("token", envelope.to_json().lower())
        self.assertEqual(1, self.dispatcher.dispatch_pending())
        self.assertEqual("succeeded", self.worker.process_one())
        completed = self.runtime_repository.get_agent_execution(
            self.tenant.tenant_id, self.company_id, job.job_id
        )
        self.assertIs(ExecutionState.SUCCEEDED, completed.state)
        self.assertEqual(3, completed.actual_cost.minor_units)
        actions = {
            item["action"] for item in self.runtime_repository.list_audit(
                self.tenant.tenant_id, self.company_id
            )
        }
        self.assertTrue({"agent.execution.admitted", "agent.execution.started", "agent.execution.completed"} <= actions)
        completed_audit = next(
            item for item in self.runtime_repository.list_audit(
                self.tenant.tenant_id, self.company_id
            ) if item["action"] == "agent.execution.completed"
        )
        self.assertTrue({
            "agent_role", "capability", "attempts", "provider", "model",
            "actual_cost", "outcome", "emitted_event_ids", "artifact_refs",
        } <= set(completed_audit["details"]))
        events = self.runtime_repository.list_events(self.tenant.tenant_id, self.company_id)
        self.assertEqual(1, len([item for item in events if item["type"] == "agent.execution.completed"]))

    def test_job_reservation_and_queue_outbox_roll_back_together(self) -> None:
        original = self.runtime_repository.save_agent_admission

        def fail_after_insert(envelope, execution, outbox):
            original(envelope, execution, outbox)
            raise RuntimeError("simulated rollback before outbox commit")

        self.runtime_repository.save_agent_admission = fail_after_insert
        with self.assertRaises(RuntimeError):
            self._submit_inbox(key="agent-runtime-rollback-0001")
        self.assertIsNone(
            self.runtime_repository.get_job_by_idempotency(
                self.tenant.tenant_id, self.company_id, "agent-runtime-rollback-0001"
            )
        )
        self.assertEqual((), self.runtime_repository.list_agent_executions(
            self.tenant.tenant_id, self.company_id
        ))

    def test_expired_suspended_and_missing_plan_capability_deny_new_jobs(self) -> None:
        role = self.workforce_repository.current_definition(
            self.tenant.tenant_id, self.company_id, "role_inbox_assistant"
        )
        self.workforce_repository.replace_definition(replace(role, state=RoleState.PAUSED))
        with self.assertRaises(AgentAdmissionDenied):
            self._submit_inbox(key="agent-runtime-paused-0001")
        self.workforce_repository.replace_definition(replace(role, state=RoleState.ACTIVE))

        self._set_managed_status(EntitlementStatus.EXPIRED)
        with self.assertRaises(AgentAdmissionDenied):
            self._submit_inbox(key="agent-runtime-expired-0001")

    def test_suspended_entitlement_cancels_queued_job_and_preserves_owned_state(self) -> None:
        job = self._submit_inbox()
        company_before = self.brain.get_company(Scope(self.tenant.tenant_id, self.company_id))
        owned_before = tuple(
            item for item in self.commercial_repository.get_current_entitlement_grants(
                self.tenant.tenant_id, self.company_id
            ) if item.entitlement_class is EntitlementClass.CUSTOMER_OWNED
        )
        self._set_managed_status(EntitlementStatus.SUSPENDED)
        self.assertEqual(1, self.service.cancel_ineligible(self.tenant.tenant_id, self.company_id))
        self.assertEqual(
            JobStatus.CANCELLED,
            self.runtime_repository.get_job(self.tenant.tenant_id, self.company_id, job.job_id).status,
        )
        self.assertEqual(company_before, self.brain.get_company(Scope(self.tenant.tenant_id, self.company_id)))
        owned_after = tuple(
            item for item in self.commercial_repository.get_current_entitlement_grants(
                self.tenant.tenant_id, self.company_id
            ) if item.entitlement_class is EntitlementClass.CUSTOMER_OWNED
        )
        self.assertEqual(owned_before, owned_after)

    def test_suspended_entitlement_denies_new_job_immediately(self) -> None:
        self._set_managed_status(EntitlementStatus.SUSPENDED)
        with self.assertRaises(AgentAdmissionDenied):
            self._submit_inbox(key="agent-runtime-suspended-new-1")

    def test_capability_missing_from_plan_denies_and_expiring_only_drains_existing(self) -> None:
        grants = self.commercial_repository.get_current_entitlement_grants(
            self.tenant.tenant_id, self.company_id
        )
        inbox = next(item for item in grants if item.entitlement_code == "inbox.autonomous")
        self.commercial_repository.append_entitlement_grant(
            replace(
                inbox, status=EntitlementStatus.EXPIRED,
                updated_at=self.clock.now, version=inbox.version + 1,
            )
        )
        with self.assertRaises(AgentAdmissionDenied):
            self._submit_inbox(key="agent-runtime-plan-missing-1")

        # Restore the exact grant, admit work, then schedule cancellation.
        current = next(
            item for item in self.commercial_repository.get_current_entitlement_grants(
                self.tenant.tenant_id, self.company_id
            ) if item.entitlement_code == "inbox.autonomous"
        )
        self.commercial_repository.append_entitlement_grant(
            replace(
                current, status=EntitlementStatus.ACTIVE,
                updated_at=self.clock.now, version=current.version + 1,
            )
        )
        job = self._submit_inbox(key="agent-runtime-expiring-drain-1")
        self.clock.now += timedelta(seconds=1)
        self._set_managed_status(
            EntitlementStatus.EXPIRING,
            effective_until=self.clock.now + timedelta(minutes=5),
        )
        with self.assertRaises(AgentAdmissionDenied):
            self._submit_inbox(
                event=self._inbound_event("event_inbound_after_expiring"),
                key="agent-runtime-expiring-new-1",
            )
        self.dispatcher.dispatch_pending()
        self.assertEqual("succeeded", self.worker.process_one())
        self.assertEqual(
            JobStatus.SUCCEEDED,
            self.runtime_repository.get_job(
                self.tenant.tenant_id, self.company_id, job.job_id
            ).status,
        )

    def test_ai_policy_capability_and_budget_denials(self) -> None:
        event = self._inbound_event()
        with self.assertRaises(AgentAdmissionDenied):
            self.service.submit(
                tenant_id=self.tenant.tenant_id, company_id=self.company_id,
                role_id="role_quote_drafting_assistant", capability="communications.email",
                action="classify_message", budget_ref="budget_agent_runtime",
                maximum_job_spend=Money("USD", 1), idempotency_key="agent-runtime-policy-deny-1",
                correlation_id=event.correlation_id, trigger_class=TriggerClass.INBOUND_EVENT,
                trigger_ref=event.event_id,
            )
        # Exhaust the Runtime-owned company budget, then prove admission rolls back.
        budget = self.runtime_repository.get_budget(
            self.tenant.tenant_id, self.company_id, "budget_agent_runtime"
        )
        budget.settled_minor = 100
        self.runtime_repository.save_budget(budget)
        with self.assertRaises(BudgetExceeded):
            self._submit_inbox(key="agent-runtime-budget-deny-2")
        self.assertIsNone(
            self.runtime_repository.get_job_by_idempotency(
                self.tenant.tenant_id, self.company_id, "agent-runtime-budget-deny-2"
            )
        )

    def test_model_router_filters_before_cost_ranking(self) -> None:
        selected = ModelRouter().select(
            self._models("communications.email"), ModelPolicy(quality_floor=80),
            capability="communications.email", remaining_budget=Money("USD", 5),
        )
        self.assertEqual("deterministic-test", selected.provider)
        with self.assertRaises(NoEligibleModel):
            ModelRouter().select(
                self._models("communications.email"), ModelPolicy(quality_floor=95),
                capability="communications.email", remaining_budget=Money("USD", 5),
            )

    def test_forged_tenant_company_and_envelope_are_rejected(self) -> None:
        job = self._submit_inbox()
        self.dispatcher.dispatch_pending()
        message = self.queue.messages[0]
        forged = dict(message.payload)
        forged["tenant_id"] = "tenant_other"
        message.payload = forged
        self.assertEqual("denied", self.worker.process_one())
        self.assertEqual(0, self.email_capability.total_calls)
        self.assertIsNone(
            self.runtime_repository.get_job("tenant_other", self.company_id, job.job_id)
        )
        with self.assertRaises(NotFoundError):
            self.brain.get_company(Scope("tenant_other", self.company_id))
        self.assertEqual((), self.commercial_repository.get_current_entitlement_grants(
            "tenant_other", self.company_id
        ))
        self.assertIsNone(self.runtime_repository.get_budget(
            "tenant_other", self.company_id, "budget_agent_runtime"
        ))
        self.assertEqual([], self.runtime_repository.list_events(
            "tenant_other", self.company_id
        ))
        self.assertEqual([], self.runtime_repository.list_audit(
            "tenant_other", self.company_id
        ))

        payload = self.runtime_repository.get_agent_envelope(
            self.tenant.tenant_id, self.company_id, job.job_id
        ).to_payload()
        payload["company_id"] = "company_other"
        self.queue.send(f"queue_{'0' * 24}", payload)
        self.assertEqual("denied", self.worker.process_one())

    def test_duplicate_delivery_and_crash_after_execution_are_idempotent(self) -> None:
        job = self._submit_inbox()
        self.dispatcher.dispatch_pending()
        with self.assertRaises(SimulatedWorkerCrash):
            self.worker.process_one(crash_point="after_execution")
        self.assertEqual(1, self.email_capability.total_calls)
        self.queue.redeliver_all()
        self.assertEqual("duplicate", self.worker.process_one())
        self.assertEqual(1, self.email_capability.total_calls)
        events = self.runtime_repository.list_events(self.tenant.tenant_id, self.company_id)
        self.assertEqual(1, len([item for item in events if item["type"] == "agent.execution.completed" and item["payload"]["job_id"] == job.job_id]))

    def test_crash_before_execution_recovers_after_lease(self) -> None:
        self._submit_inbox()
        self.dispatcher.dispatch_pending()
        with self.assertRaises(SimulatedWorkerCrash):
            self.worker.process_one(crash_point="before_execution")
        self.assertEqual(0, self.email_capability.total_calls)
        self.clock.now += timedelta(seconds=61)
        self.queue.redeliver_all()
        self.assertEqual("succeeded", self.worker.process_one())
        self.assertEqual(1, self.email_capability.total_calls)

    def test_retry_and_dlq_are_bounded(self) -> None:
        failing = DeterministicAgentCapability("communications.email", failure_attempts=10)
        self.registry._capabilities[("communications.email", "v1")] = failing
        self._submit_inbox(retry_policy=RetryPolicy(max_attempts=2))
        self.dispatcher.dispatch_pending()
        self.assertEqual("retry", self.worker.process_one())
        self.queue.redeliver_all()
        self.assertEqual("failed", self.worker.process_one())
        self.queue.redeliver_all()
        self.assertEqual("empty", self.worker.process_one())
        self.assertEqual(1, len(self.queue.dead_letters))
        self.assertEqual(0, self.runtime_repository.get_budget(
            self.tenant.tenant_id, self.company_id, "budget_agent_runtime"
        ).reserved_minor)

    def test_missing_founder_approval_never_reaches_queue(self) -> None:
        event = self._inbound_event()
        job = self.service.submit(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            role_id="role_quote_drafting_assistant", capability="customer.quote",
            action="draft_exception_quote", budget_ref="budget_agent_runtime",
            maximum_job_spend=Money("USD", 0), idempotency_key="agent-runtime-approval-0001",
            correlation_id=event.correlation_id, trigger_class=TriggerClass.INBOUND_EVENT,
            trigger_ref=event.event_id,
        )
        self.assertEqual(JobStatus.WAITING_APPROVAL, job.status)
        self.assertIsNone(self.runtime_repository.get_agent_envelope(
            self.tenant.tenant_id, self.company_id, job.job_id
        ))
        self.runtime.approve_job(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            job_id=job.job_id, approval_id=job.approval_ids[0], principal=self.principal,
        )
        queued = self.service.submit(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            role_id="role_quote_drafting_assistant", capability="customer.quote",
            action="draft_exception_quote", budget_ref="budget_agent_runtime",
            maximum_job_spend=Money("USD", 0), idempotency_key="agent-runtime-approval-0001",
            correlation_id=event.correlation_id, trigger_class=TriggerClass.INBOUND_EVENT,
            trigger_ref=event.event_id,
        )
        self.assertEqual(JobStatus.RUNNABLE, queued.status)
        self.assertIsNotNone(self.runtime_repository.get_agent_envelope(
            self.tenant.tenant_id, self.company_id, job.job_id
        ))

    def test_authenticated_customer_trigger_uses_trusted_principal(self) -> None:
        job = self.service.submit(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            role_id="role_intake_assistant", capability="customer.intake",
            action="classify_lead", budget_ref="budget_agent_runtime",
            maximum_job_spend=Money("USD", 0), idempotency_key="agent-runtime-customer-0001",
            correlation_id="correlation-customer-agent", trigger_class=TriggerClass.CUSTOMER_REQUEST,
            trigger_ref="request-customer-agent", principal=self.principal,
        )
        self.assertEqual(JobStatus.RUNNABLE, job.status)
        with self.assertRaises(PermissionError):
            self.service.submit(
                tenant_id=self.tenant.tenant_id, company_id=self.company_id,
                role_id="role_intake_assistant", capability="customer.intake",
                action="classify_lead", budget_ref="budget_agent_runtime",
                maximum_job_spend=Money("USD", 0), idempotency_key="agent-runtime-forged-principal",
                correlation_id="correlation-forged", trigger_class=TriggerClass.CUSTOMER_REQUEST,
                trigger_ref="request-forged", principal={"role": "owner"},
            )

    def test_scheduler_emits_once_per_due_occurrence_without_busy_loop(self) -> None:
        scheduler = RuntimeScheduler(
            self.runtime_repository, self.runtime.events, clock=self.clock
        )
        scheduler.save(
            RuntimeSchedule(
                "schedule_inbox_test", self.tenant.tenant_id, self.company_id,
                "role_inbox_assistant", "communications.email", "classify_message",
                300, self.clock.now,
            )
        )
        first = scheduler.tick()
        self.assertEqual(1, len(first))
        self.assertEqual((), scheduler.tick())
        self.clock.now += timedelta(minutes=5)
        self.assertEqual(1, len(scheduler.tick()))


if __name__ == "__main__":
    unittest.main()
