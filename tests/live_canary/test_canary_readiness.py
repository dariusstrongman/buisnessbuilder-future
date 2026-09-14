from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import unittest

from tests.outbound_communications.test_compliance_operations import ComplianceOperationsTests

from businessbuilder.agent_runtime import ModelPolicy, TriggerClass
from businessbuilder.commercial import EntitlementStatus
from businessbuilder.communications_compliance.models import KillSwitchScope
from businessbuilder.live_canary import (
    CanaryDenied, DeterministicEmailProviderEmulator, LiveCanaryReadiness,
    ReconciliationResult,
)
from businessbuilder.live_canary.models import (
    CanaryAlertSeverity, CanaryPermitStatus, CanarySendReservation,
    ReadinessState, ReconciliationState, ReputationState,
)
from businessbuilder.outbound_communications import CommunicationPurpose, ContentEvidence
from businessbuilder.provider_connection.models import ProviderHealth
from businessbuilder.runtime import ArtifactRef, Money
from businessbuilder.access_broker.models import ReceiptStatus, stable_id


class LiveCanaryReadinessTests(unittest.TestCase):
    def setUp(self):
        self.ops = ComplianceOperationsTests(methodName="runTest")
        self.ops.setUp()
        self.fx = self.ops.fx
        self.operator = object()
        self.emulator = DeterministicEmailProviderEmulator(provider=self.fx.connection.provider)
        connection = replace(
            self.fx.connection,
            scopes_requested=frozenset({"mail.read", "mail.send"}),
            scopes_granted=frozenset({"mail.read", "mail.send"}),
        )
        self.fx.repository.save_broker_record(
            "provider_connection", connection.connection_id,
            connection.tenant_id, connection.company_id, connection)
        self.fx.connection = connection
        self.fx.repository.save_broker_record(
            "provider_health", connection.connection_id,
            connection.tenant_id, connection.company_id,
            ProviderHealth(connection.connection_id, connection.tenant_id,
                           connection.company_id, True, 1, 0, 0, 0, None,
                           None, self.fx.clock(), self.fx.clock()))
        self.canary = LiveCanaryReadiness(
            repository=self.fx.repository,
            identity_repository=self.fx.identity_repository,
            commercial_repository=self.fx.commercial_repository,
            compliance=self.ops.compliance,
            company_brain=self.fx.brain,
            clock=self.fx.clock,
            id_factory=self.fx.ids,
            permit_signing_key=b"sandbox-canary-permit-signing-key-32-bytes-minimum",
            provider_adapters={connection.provider: self.emulator},
            operator_verifier=lambda value: "platform_operator_test" if value is self.operator else "",
            founder_approval_verifier=lambda tenant, company, ref: (
                tenant == self.fx.tenant.tenant_id and company == self.fx.company_id
                and ref == "approval_founder_canary_001"),
        )
        self.sender = self.canary.record_sender_readiness(
            self.operator, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id,
            provider_connection_id=connection.connection_id,
            sender_address="canary-sender@example.test",
            domain_ownership_verified=True, spf_valid=True, dkim_valid=True,
            dmarc_present=True, alignment_valid=True, provider_verified=True,
            reputation=ReputationState.GOOD)
        self.canary.record_deliverability(
            self.operator, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id,
            provider_connection_id=connection.connection_id)

    def tearDown(self):
        self.ops.tearDown()

    def _permit(self, *, total=3, hourly=3, start=None, end=None,
                recipient_digests=None):
        start = start or self.fx.clock()
        end = end or start + timedelta(hours=1)
        return self.canary.conduct_simulated_ceremony(
            self.operator, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id,
            provider_connection_id=self.fx.connection.connection_id,
            sender_id=self.sender.sender_id,
            recipient_digests=recipient_digests or (self.fx.recipient.destination_digest,),
            recipient_domains=("example.test",),
            allowed_purposes=(CommunicationPurpose.REPLY_TO_INBOUND,),
            max_sends_total=total, max_sends_hour=hourly,
            starts_at=start, expires_at=end,
            monitoring_owner="operator_on_call_test",
            rollback_plan_ref="runbook_rollback_v1",
            kill_switch_test_ref="kill_switch_test_passed_001",
            approval_ref="approval_founder_canary_001")

    def _job(self, key, permit):
        if len(key) < 16:
            key = f"{key}-canary-proof"
        request = self.fx.safety.prepare_request(
            tenant_id=self.fx.tenant.tenant_id, company_id=self.fx.company_id,
            recipient_id=self.fx.recipient.recipient_id,
            purpose=CommunicationPurpose.REPLY_TO_INBOUND,
            agent_role="role_inbox_assistant", capability="communications.email",
            provider_connection_id=self.fx.connection.connection_id,
            runtime_idempotency_key=key,
            content="Thanks for contacting the canary sandbox inbox.",
            evidence=ContentEvidence(), context_ref=self.fx.event.event_id,
            canary_permit_ref=permit.permit_id)
        return self.fx.agent_runtime.submit(
            tenant_id=self.fx.tenant.tenant_id, company_id=self.fx.company_id,
            role_id="role_inbox_assistant", capability="communications.email",
            action="send_preapproved_reply", budget_ref="budget_broker",
            maximum_job_spend=Money("USD", 5), idempotency_key=key,
            correlation_id=self.fx.event.correlation_id,
            trigger_class=TriggerClass.INBOUND_EVENT, trigger_ref=self.fx.event.event_id,
            causation_id=self.fx.event.event_id,
            input_artifact_refs=(ArtifactRef("attachment", self.fx.artifact.artifact_id),),
            secret_refs=(self.fx.job_secret_ref,), communication_ref=request.communication_id,
            model_policy=ModelPolicy(quality_floor=70, requires_tools=True))

    def test_readiness_and_ceremony_are_status_only_live_gate_stays_off(self):
        result = self.canary.evaluate_eligibility(
            tenant_id=self.fx.tenant.tenant_id, company_id=self.fx.company_id,
            provider_connection_id=self.fx.connection.connection_id,
            sender_id=self.sender.sender_id, approval_ref="approval_founder_canary_001")
        self.assertIs(ReadinessState.CANARY_READY, result.state)
        permit = self._permit()
        self.assertTrue(permit.simulation_only)
        self.assertIs(CanaryPermitStatus.APPROVED_SIMULATION, permit.status)
        self.assertFalse(self.canary.live_send_enabled)
        self.assertFalse(self.ops.compliance.live_send_enabled)
        ceremony = self.fx.repository.get_broker_record(
            "live_canary_ceremony", permit.tenant_id, permit.company_id, permit.ceremony_id)
        self.assertFalse(ceremony.live_gate_changed)

    def test_live_adapter_and_untrusted_ceremony_attempts_fail_closed(self):
        with self.assertRaises(CanaryDenied):
            self.canary.conduct_simulated_ceremony(
                object(), tenant_id=self.fx.tenant.tenant_id, company_id=self.fx.company_id,
                provider_connection_id=self.fx.connection.connection_id,
                sender_id=self.sender.sender_id,
                recipient_digests=(self.fx.recipient.destination_digest,),
                recipient_domains=("example.test",),
                allowed_purposes=(CommunicationPurpose.REPLY_TO_INBOUND,),
                max_sends_total=1, max_sends_hour=1,
                starts_at=self.fx.clock(), expires_at=self.fx.clock()+timedelta(minutes=30),
                monitoring_owner="support", rollback_plan_ref="rollback",
                kill_switch_test_ref="test", approval_ref="approval_founder_canary_001")
        with self.assertRaises(CanaryDenied):
            self.canary.conduct_simulated_ceremony(
                self.operator, tenant_id=self.fx.tenant.tenant_id, company_id=self.fx.company_id,
                provider_connection_id=self.fx.connection.connection_id,
                sender_id=self.sender.sender_id, recipient_digests=("*",),
                recipient_domains=("*",),
                allowed_purposes=(CommunicationPurpose.MARKETING,),
                max_sends_total=100, max_sends_hour=100,
                starts_at=self.fx.clock(), expires_at=self.fx.clock()+timedelta(days=2),
                monitoring_owner="agent", rollback_plan_ref="none",
                kill_switch_test_ref="none", approval_ref="forged")
        with self.assertRaises(RuntimeError):
            self.emulator.send(destination="real@example.com")
        actions = {item["action"] for item in self.fx.repository.list_audit(
            self.fx.tenant.tenant_id, self.fx.company_id)}
        self.assertIn("canary.permit.denied", actions)

    def test_simulated_send_duplicate_and_reconciliation_are_idempotent(self):
        permit = self._permit()
        envelope = self.fx._envelope(self._job("canary-send-001", permit))
        first = self.fx.broker.execute_provider_action(
            envelope, operation="send_preapproved_reply", secret_ref=self.fx.job_secret_ref)
        second = self.fx.broker.execute_provider_action(
            envelope, operation="send_preapproved_reply", secret_ref=self.fx.job_secret_ref)
        self.assertEqual(first.receipt_id, second.receipt_id)
        self.assertEqual(1, self.fx.provider.call_count)
        uncertain = replace(first, status=ReceiptStatus.IN_PROGRESS,
                            response_classification="provider_result_uncertain",
                            reconciliation_status="pending", completed_at=None)
        self.fx.repository.complete_provider_receipt(uncertain)
        self.emulator.set_result(first.provider_request_id,
            ReconciliationResult(ReconciliationState.RESOLVED,
                                 "reconciled_accepted", "sandbox_message_ref"))
        task = self.canary.reconcile_uncertain(
            tenant_id=first.tenant_id, company_id=first.company_id,
            receipt_id=first.receipt_id, adapter_name=first.provider)
        self.assertIs(ReconciliationState.RESOLVED, task.state)
        persisted = self.fx.repository.get_provider_receipt(
            first.tenant_id, first.company_id, first.provider,
            first.operation, first.idempotency_key)
        self.assertEqual("resolved", persisted.reconciliation_status)

    def test_forged_expired_and_cross_scope_permits_are_denied(self):
        permit = self._permit()
        forged = replace(permit, company_id="company_other")
        self.fx.repository.save_broker_record("live_canary_permit", forged.permit_id,
                                              permit.tenant_id, permit.company_id, forged)
        with self.assertRaises(PermissionError):
            self._job("forged-permit-001", permit)
        valid = self._permit()
        self.fx.clock.now += timedelta(hours=2)
        with self.assertRaises(PermissionError):
            self._job("expired-permit-001", valid)
        persisted = self.fx.repository.get_broker_record(
            "live_canary_permit", valid.tenant_id, valid.company_id, valid.permit_id)
        self.assertIs(CanaryPermitStatus.EXPIRED, persisted.status)

    def test_domain_auth_provider_health_and_entitlement_failures_block(self):
        bad_sender = self.canary.record_sender_readiness(
            self.operator, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id,
            provider_connection_id=self.fx.connection.connection_id,
            sender_address="bad-sender@example.test",
            domain_ownership_verified=True, spf_valid=False, dkim_valid=True,
            dmarc_present=True, alignment_valid=True, provider_verified=True,
            reputation=ReputationState.GOOD)
        result = self.canary.evaluate_eligibility(
            tenant_id=self.fx.tenant.tenant_id, company_id=self.fx.company_id,
            provider_connection_id=self.fx.connection.connection_id,
            sender_id=bad_sender.sender_id, approval_ref="approval_founder_canary_001")
        self.assertIs(ReadinessState.NOT_READY, result.state)
        self.assertIn("domain_authentication", {g[0] for g in result.gates if not g[1]})
        self.fx._set_entitlement(EntitlementStatus.SUSPENDED)
        result = self.canary.evaluate_eligibility(
            tenant_id=self.fx.tenant.tenant_id, company_id=self.fx.company_id,
            provider_connection_id=self.fx.connection.connection_id,
            sender_id=self.sender.sender_id, approval_ref="approval_founder_canary_001")
        self.assertIn("build_and_run_entitlement", {g[0] for g in result.gates if not g[1]})

    def test_allowlist_send_caps_and_atomic_concurrency_default_deny(self):
        permit = self._permit(total=1, hourly=1)
        other = replace(self.fx.recipient, recipient_id="recipient_other",
                        normalized_destination="unexpected@example.test")
        valid_job = self._job("allowlist-source-001", permit)
        valid_request = self.fx.repository.get_broker_record(
            "communication_request", permit.tenant_id, permit.company_id,
            self.fx._envelope(valid_job).communication_ref)
        with self.assertRaises(CanaryDenied):
            self.canary.validate_simulated_send(
                replace(valid_request, recipient_id=other.recipient_id), other,
                stage="admission")
        now = self.fx.clock()
        values = [CanarySendReservation(
            f"reservation_{i}", permit.permit_id, permit.tenant_id, permit.company_id,
            f"communication_{i}", f"idempotency_{i}", now) for i in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda value: self.fx.repository.reserve_canary_send(
                value, 1, 1), values))
        self.assertEqual(1, sum(1 for _, allowed, _ in results if allowed))

    def test_all_kill_switch_scopes_stop_queued_work_before_provider(self):
        scopes = (
            (KillSwitchScope.GLOBAL, "global", "platform"),
            (KillSwitchScope.TENANT, self.fx.tenant.tenant_id, "platform"),
            (KillSwitchScope.COMPANY, self.fx.company_id, "company"),
            (KillSwitchScope.PROVIDER_CONNECTION, self.fx.connection.connection_id, "platform"),
            (KillSwitchScope.CHANNEL, "email", "platform"),
        )
        queued = [(scope, scope_id, authority,
                   self.fx._envelope(self._job(f"kill-scope-{index}-proof", self._permit())))
                  for index, (scope, scope_id, authority) in enumerate(scopes)]
        for index, (scope, scope_id, authority, envelope) in enumerate(queued):
            with self.subTest(scope=scope.value):
                if authority == "company":
                    self.ops.compliance.set_company_kill_switch(
                        self.fx.principal, tenant_id=self.fx.tenant.tenant_id,
                        company_id=self.fx.company_id, engaged=True, reason_code="canary_test")
                else:
                    self.ops.compliance.set_platform_kill_switch(
                        self.ops.operator, scope=scope, scope_id=scope_id,
                        tenant_id=self.fx.tenant.tenant_id,
                        company_id=self.fx.company_id, engaged=True,
                        reason_code="canary_test")
                with self.assertRaises(Exception):
                    self.fx.broker.execute_provider_action(
                        envelope, operation="send_preapproved_reply",
                        secret_ref=self.fx.job_secret_ref)
                if authority == "company":
                    self.ops.compliance.set_company_kill_switch(
                        self.fx.principal, tenant_id=self.fx.tenant.tenant_id,
                        company_id=self.fx.company_id, engaged=False, reason_code="reset")
                else:
                    self.ops.compliance.set_platform_kill_switch(
                        self.ops.operator, scope=scope, scope_id=scope_id,
                        tenant_id=self.fx.tenant.tenant_id,
                        company_id=self.fx.company_id, engaged=False,
                        reason_code="reset")
        self.assertEqual(0, self.fx.provider.call_count)

    def test_rollback_revokes_and_cannot_silently_renew(self):
        permit = self._permit()
        revoked = self.canary.revoke_permit(
            self.operator, tenant_id=permit.tenant_id, company_id=permit.company_id,
            permit_id=permit.permit_id, reason="simulation_rollback")
        self.assertIs(CanaryPermitStatus.REVOKED, revoked.status)
        with self.assertRaises(PermissionError):
            self._job("revoked-permit-001", revoked)
        with self.assertRaises(CanaryDenied):
            self.canary.revoke_permit(
                object(), tenant_id=permit.tenant_id, company_id=permit.company_id,
                permit_id=permit.permit_id, reason="support_override")

    def test_customer_status_is_safe_and_alerts_omit_recipient_address(self):
        status = self.canary.customer_status(
            self.fx.principal, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id)
        self.assertEqual(False, status["live_send_enabled"])
        self.assertEqual("Sandbox testing", status["mode"])
        self.assertNotIn(self.fx.recipient.normalized_destination, repr(status))
        permit = self._permit()
        other = replace(self.fx.recipient, recipient_id="recipient_alert",
                        normalized_destination="alert-target@example.test")
        valid_job = self._job("alert-source-canary-001", permit)
        valid_request = self.fx.repository.get_broker_record(
            "communication_request", permit.tenant_id, permit.company_id,
            self.fx._envelope(valid_job).communication_ref)
        with self.assertRaises(CanaryDenied):
            self.canary.validate_simulated_send(
                replace(valid_request, recipient_id=other.recipient_id), other,
                stage="admission")
        alerts = self.fx.repository.list_broker_records(
            "live_canary_alert", permit.tenant_id, permit.company_id)
        self.assertTrue(any(a.severity is CanaryAlertSeverity.CRITICAL for a in alerts))
        self.assertNotIn(other.normalized_destination, repr(alerts))


if __name__ == "__main__":
    unittest.main()
