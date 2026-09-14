from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import unittest

from tests.outbound_communications.test_outbound_safety import OutboundCommunicationSafetyTests

from businessbuilder.communications_compliance import CommunicationsCompliance, ComplianceDenied
from businessbuilder.communications_compliance.models import (
    AbuseSignalType, AbuseThresholds, AlertClass, KillSwitchScope, RolloutTier,
)
from businessbuilder.identity import Role
from businessbuilder.outbound_communications import (
    CommunicationDenied, CommunicationPurpose, ConsentState, ContactRelationship,
    DeliveryStatus, SuppressionState,
)


class ComplianceOperationsTests(unittest.TestCase):
    def setUp(self):
        self.fx = OutboundCommunicationSafetyTests(methodName="runTest")
        self.fx.setUp()
        self.operator = object()
        self.compliance = CommunicationsCompliance(
            repository=self.fx.repository,
            outbound_safety=self.fx.safety,
            principal_authority=self.fx.authority,
            authorization=self.fx.commercial.authorization,
            audit=self.fx.runtime.audit,
            clock=self.fx.clock,
            id_factory=self.fx.ids,
            unsubscribe_signing_key=b"sandbox-unsubscribe-signing-key-32bytes-minimum",
            abuse_thresholds=AbuseThresholds(warning=1, throttle=2, suspend=3,
                                             emergency=4, window_seconds=3600),
            operator_verifier=lambda value: "platform_operator_test" if value is self.operator else "",
        )
        self.compliance.classify_recipient(self.fx.recipient)
        self.compliance.record_canonical_consent(
            self.fx.recipient, source_event_ref=self.fx.event.event_id)
        self.compliance.configure_jurisdiction(
            self.fx.principal, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id, recipient_id=self.fx.recipient.recipient_id,
            tenant_jurisdiction="US", company_jurisdiction="US",
            recipient_jurisdiction="US", source_ref="company_profile_test")

    def tearDown(self):
        self.fx.tearDown()

    def _execute(self, key):
        envelope = self.fx._envelope(self.fx._submit(key=key))
        receipt = self.fx.broker.execute_provider_action(
            envelope, operation="send_preapproved_reply",
            secret_ref=self.fx.job_secret_ref)
        return envelope, receipt

    def test_valid_us_inbound_reply_stays_sandbox_and_records_decisions(self):
        _, receipt = self._execute("compliance-valid-inbound-001")
        self.assertEqual("succeeded", receipt.status.value)
        decisions = self.fx.repository.list_broker_records(
            "communication_compliance_decision", self.fx.tenant.tenant_id,
            self.fx.company_id)
        self.assertEqual({"admission", "execution"}, {d.stage for d in decisions})
        self.assertTrue(all(d.policy_version.endswith("us-federal.v1") for d in decisions))
        self.assertFalse(self.compliance.live_send_enabled)

    def test_unknown_jurisdiction_and_expired_or_withdrawn_consent_fail_closed(self):
        other = self.fx.safety.register_from_canonical_event(
            tenant_id=self.fx.tenant.tenant_id, company_id=self.fx.company_id,
            destination="unknown-jurisdiction@example.test",
            relationship=ContactRelationship.INBOUND_CUSTOMER,
            consent_state=ConsentState.CUSTOMER_INITIATED,
            consent_provenance="inbound_context", source_event_ref=self.fx.event.event_id)
        with self.assertRaises(ComplianceDenied):
            self.fx.safety.prepare_request(
                tenant_id=self.fx.tenant.tenant_id, company_id=self.fx.company_id,
                recipient_id=other.recipient_id,
                purpose=CommunicationPurpose.REPLY_TO_INBOUND,
                agent_role="role_inbox_assistant", capability="communications.email",
                provider_connection_id=self.fx.connection.connection_id,
                runtime_idempotency_key="unknown-jurisdiction-001", content="Sandbox reply",
                context_ref=self.fx.event.event_id)

        evidence = next(item for item in self.fx.repository.list_broker_records(
            "communication_compliance_consent", self.fx.tenant.tenant_id,
            self.fx.company_id) if item.recipient_id == self.fx.recipient.recipient_id)
        self.fx.repository.save_broker_record(
            "communication_compliance_consent", evidence.consent_evidence_id,
            evidence.tenant_id, evidence.company_id,
            replace(evidence, expires_at=self.fx.clock() - timedelta(seconds=1)))
        with self.assertRaises(ComplianceDenied):
            self.fx._submit(key="expired-consent-001")

    def test_unsubscribe_is_opaque_idempotent_and_blocks_future_sends(self):
        token = self.compliance.issue_unsubscribe_token(
            tenant_id=self.fx.tenant.tenant_id, company_id=self.fx.company_id,
            recipient_id=self.fx.recipient.recipient_id)
        self.assertNotIn(self.fx.tenant.tenant_id, token)
        self.assertNotIn(self.fx.recipient.recipient_id, token)
        self.assertFalse(self.compliance.unsubscribe(token + "forged"))
        self.assertTrue(self.compliance.unsubscribe(token))
        self.assertTrue(self.compliance.unsubscribe(token))
        current = self.fx.safety._recipient(self.fx.tenant.tenant_id,
                                            self.fx.company_id,
                                            self.fx.recipient.recipient_id)
        self.assertIs(SuppressionState.SUPPRESSED, current.suppression_state)
        self.assertIs(ConsentState.WITHDRAWN, current.consent_state)
        with self.assertRaises(ComplianceDenied):
            self.fx._submit(key="post-unsubscribe-001")

    def test_expired_unsubscribe_token_has_safe_failure(self):
        token = self.compliance.issue_unsubscribe_token(
            tenant_id=self.fx.tenant.tenant_id, company_id=self.fx.company_id,
            recipient_id=self.fx.recipient.recipient_id, lifetime=timedelta(seconds=1))
        self.fx.clock.now += timedelta(seconds=2)
        self.assertFalse(self.compliance.unsubscribe(token))
        current = self.fx.safety._recipient(self.fx.tenant.tenant_id,
                                            self.fx.company_id,
                                            self.fx.recipient.recipient_id)
        self.assertIs(SuppressionState.CLEAR, current.suppression_state)

    def test_verified_bounce_and_complaint_suppress_and_forgery_does_not_mutate(self):
        envelope, receipt = self._execute("callback-bounce-001")
        delivery = self.fx.repository.list_broker_records(
            "communication_delivery", self.fx.tenant.tenant_id, self.fx.company_id)[0]
        body, signature = self.fx.verifier.callback(
            "bounce_event_001", receipt.provider_request_id, DeliveryStatus.BOUNCED)
        with self.assertRaises(CommunicationDenied):
            self.fx.safety.handle_delivery_callback(
                provider_name=self.fx.provider.provider, body=body, signature="forged")
        self.fx.safety.handle_delivery_callback(
            provider_name=self.fx.provider.provider, body=body, signature=signature)
        replay = self.fx.safety.handle_delivery_callback(
            provider_name=self.fx.provider.provider, body=body, signature=signature)
        self.assertIs(DeliveryStatus.BOUNCED, replay.status)
        self.assertEqual(1, len([s for s in self.fx.repository.list_broker_records(
            "communication_compliance_abuse_signal", self.fx.tenant.tenant_id,
            self.fx.company_id) if s.signal_type is AbuseSignalType.HARD_BOUNCE]))

    def test_abuse_thresholds_are_explicit_audited_and_suspend(self):
        for index in range(3):
            self.compliance.record_abuse_signal(
                self.fx.tenant.tenant_id, self.fx.company_id,
                AbuseSignalType.COMPLAINT, f"complaint_signal_{index}")
        alerts = self.fx.repository.list_broker_records(
            "communication_compliance_alert", self.fx.tenant.tenant_id,
            self.fx.company_id)
        self.assertTrue(any(a.alert_class is AlertClass.COMPLAINT_SPIKE for a in alerts))
        switch = self.compliance._switch(KillSwitchScope.COMPANY, self.fx.company_id)
        self.assertTrue(switch.engaged)

    def test_company_kill_switch_rechecked_before_provider_and_authorized_restore(self):
        envelope = self.fx._envelope(self.fx._submit(key="queued-before-kill-001"))
        self.compliance.set_company_kill_switch(
            self.fx.principal, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id, engaged=True, reason_code="operator_pause")
        with self.assertRaises(CommunicationDenied):
            self.fx.broker.execute_provider_action(
                envelope, operation="send_preapproved_reply",
                secret_ref=self.fx.job_secret_ref)
        self.assertEqual(0, self.fx.provider.call_count)
        self.compliance.set_company_kill_switch(
            self.fx.principal, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id, engaged=False, reason_code="review_complete")
        self.fx.broker.execute_provider_action(
            envelope, operation="send_preapproved_reply", secret_ref=self.fx.job_secret_ref)
        self.assertEqual(1, self.fx.provider.call_count)

    def test_forged_role_support_and_agent_cannot_override_controls(self):
        forged = replace(self.fx.principal, role=Role.SUPPORT)
        with self.assertRaises(PermissionError):
            self.compliance.set_company_kill_switch(
                forged, tenant_id=self.fx.tenant.tenant_id,
                company_id=self.fx.company_id, engaged=False, reason_code="forged")
        with self.assertRaises(ComplianceDenied):
            self.compliance.set_platform_kill_switch(
                object(), scope=KillSwitchScope.GLOBAL, scope_id="global",
                engaged=False, reason_code="agent_attempt")

    def test_live_rollout_and_live_provider_are_compile_time_denied(self):
        for tier in (RolloutTier.INTERNAL_CANARY, RolloutTier.LIMITED_LIVE,
                     RolloutTier.GENERAL_LIVE):
            with self.assertRaises(ComplianceDenied):
                self.compliance.set_rollout(
                    self.fx.principal, tenant_id=self.fx.tenant.tenant_id,
                    company_id=self.fx.company_id, tier=tier)
        connection = replace(self.fx.connection, provider="live-email")
        self.fx.repository.save_broker_record(
            "provider_connection", connection.connection_id,
            connection.tenant_id, connection.company_id, connection)
        with self.assertRaises(ComplianceDenied):
            self.fx._submit(key="live-provider-denied-001")

    def test_rollout_allowlist_and_founder_approval_constraints_fail_closed(self):
        rollout = self.compliance._rollout(self.fx.tenant.tenant_id, self.fx.company_id)
        self.fx.repository.save_broker_record(
            "communication_compliance_rollout", rollout.rollout_id,
            rollout.tenant_id, rollout.company_id,
            replace(rollout, approved_recipient_domains=("internal.example.test",)))
        with self.assertRaises(ComplianceDenied):
            self.fx._submit(key="canary-domain-bypass-001")
        self.fx.repository.save_broker_record(
            "communication_compliance_rollout", rollout.rollout_id,
            rollout.tenant_id, rollout.company_id,
            replace(rollout, founder_approval_required=True))
        with self.assertRaises(ComplianceDenied):
            self.fx._submit(key="canary-approval-bypass-001")

    def test_retention_erasure_pseudonymizes_operational_pii_and_preserves_evidence(self):
        result = self.compliance.request_erasure(
            self.fx.principal, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id, recipient_id=self.fx.recipient.recipient_id,
            reason="recipient_erasure_request")
        self.assertTrue(result.operational_pii_removed)
        recipient = self.fx.safety._recipient(self.fx.tenant.tenant_id,
                                              self.fx.company_id,
                                              self.fx.recipient.recipient_id)
        self.assertTrue(recipient.normalized_destination.endswith("@redacted.example.test"))
        self.assertNotIn("recipient@example.test", recipient.normalized_destination)
        self.assertTrue(self.fx.repository.list_broker_records(
            "communication_compliance_consent", self.fx.tenant.tenant_id,
            self.fx.company_id))

    def test_retention_hold_blocks_erasure_and_cross_tenant_scope_fails(self):
        held = self.compliance.set_retention_hold(
            self.operator, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id, subject_type="recipient",
            subject_id=self.fx.recipient.recipient_id, hold=True)
        self.assertTrue(held.compliance_hold)
        result = self.compliance.request_erasure(
            self.fx.principal, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id, recipient_id=self.fx.recipient.recipient_id,
            reason="held_erasure_request")
        self.assertTrue(result.blocked_by_hold)
        with self.assertRaises(PermissionError):
            self.compliance.request_erasure(
                self.fx.principal, tenant_id="tenant_other",
                company_id=self.fx.company_id, recipient_id=self.fx.recipient.recipient_id,
                reason="cross_tenant_attempt")

    def test_retention_expiration_pseudonymizes_operational_data_but_preserves_evidence(self):
        retention = self.compliance._retention_for(
            self.fx.tenant.tenant_id, self.fx.company_id, "recipient",
            self.fx.recipient.recipient_id)
        self.fx.repository.save_broker_record(
            "communication_compliance_retention", retention.retention_record_id,
            retention.tenant_id, retention.company_id,
            replace(retention, expires_at=self.fx.clock() - timedelta(seconds=1)))
        result = self.compliance.enforce_retention(
            self.operator, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id)
        self.assertEqual(1, result["pseudonymized"])
        recipient = self.fx.safety._recipient(
            self.fx.tenant.tenant_id, self.fx.company_id,
            self.fx.recipient.recipient_id)
        self.assertTrue(recipient.normalized_destination.endswith("@redacted.example.test"))
        self.assertTrue(self.fx.repository.list_broker_records(
            "communication_compliance_consent", self.fx.tenant.tenant_id,
            self.fx.company_id))

    def test_consent_versioning_preserves_superseded_evidence(self):
        old = self.fx.repository.list_broker_records(
            "communication_compliance_consent", self.fx.tenant.tenant_id,
            self.fx.company_id)[0]
        new = self.compliance.capture_consent(
            self.fx.principal, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id, recipient_id=self.fx.recipient.recipient_id,
            purpose=CommunicationPurpose.REPLY_TO_INBOUND,
            consent_basis=ConsentState.EXPLICIT, source="customer_control",
            evidence_ref="consent_form_submission_001", supersedes_id=old.consent_evidence_id)
        preserved = self.fx.repository.get_broker_record(
            "communication_compliance_consent", old.tenant_id, old.company_id,
            old.consent_evidence_id)
        self.assertEqual(new.consent_evidence_id, preserved.superseded_by_id)
        self.assertEqual("communications-compliance.us-federal.v1", new.policy_version)

    def test_customer_status_and_operator_visibility_minimize_data(self):
        status = self.compliance.customer_status(
            self.fx.principal, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id)
        self.assertEqual("sandbox_only", status["mode"])
        self.assertFalse(status["live_send_enabled"])
        summary = self.compliance.operator_summary(
            self.operator, tenant_id=self.fx.tenant.tenant_id,
            company_id=self.fx.company_id)
        serialized = repr(summary).lower()
        self.assertNotIn("recipient@example.test", serialized)
        self.assertNotIn("secret", serialized)


if __name__ == "__main__":
    unittest.main()
