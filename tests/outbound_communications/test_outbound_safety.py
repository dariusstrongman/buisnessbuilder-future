from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import hmac
import json
import unittest

from tests.access_broker import test_broker as broker_tests

from businessbuilder.access_broker import CapabilityGrant, CredentialScope
from businessbuilder.outbound_communications import (
    CommunicationDenied, CommunicationPurpose, ConsentState, ContactRelationship,
    ContentEvidence, DeliveryStatus, OutboundCommunicationSafety, PolicyOutcome,
    SuppressionState, VerifiedDeliveryEvent,
)
from businessbuilder.runtime import ArtifactRef, Event, Money
from businessbuilder.agent_runtime import ModelPolicy, TriggerClass
from businessbuilder.company_brain import EntityRef, Provenance, RecordKind, Scope


class SignedDeliveryVerifier:
    provider = "test-email"

    def __init__(self, clock):
        self.clock = clock
        self.key = b"test-only-delivery-signing-key"

    def callback(self, event_id, provider_request_id, status):
        body = json.dumps({"event_id": event_id, "provider_request_id": provider_request_id,
                           "status": status.value}, sort_keys=True).encode()
        return body, hmac.new(self.key, body, sha256).hexdigest()

    def verify_delivery_callback(self, *, body, signature):
        if not hmac.compare_digest(hmac.new(self.key, body, sha256).hexdigest(), signature):
            raise PermissionError("bad delivery signature")
        value = json.loads(body)
        return VerifiedDeliveryEvent(value["event_id"], self.provider,
                                     value["provider_request_id"],
                                     DeliveryStatus(value["status"]), self.clock(),
                                     "verified_sandbox_callback")


class OutboundCommunicationSafetyTests(broker_tests.BrokerTests):
    def setUp(self):
        super().setUp()
        self.verifier = SignedDeliveryVerifier(self.clock)
        self.safety = OutboundCommunicationSafety(
            repository=self.repository, agent_runtime=self.agent_runtime,
            principal_authority=self.authority, authorization=self.commercial.authorization,
            audit=self.runtime.audit, clock=self.clock, id_factory=self.ids,
            delivery_verifiers={self.provider.provider: self.verifier},
            company_brain=self.brain,
        )
        owner_ref = EntityRef("user", self.owner.user_id)
        self.brain.record_fact(
            Scope(self.tenant.tenant_id, self.company_id),
            record_id="pricing_policy_001", kind=RecordKind.POLICY,
            data={"fixture": "authorized pricing reference"},
            provenance=(Provenance("test", self.clock().isoformat(), owner_ref,
                                   source_ref="test://pricing-policy"),),
            owner_ref=owner_ref)
        self.broker.outbound_safety = self.safety
        self.recipient = self.safety.register_from_canonical_event(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            destination="recipient@example.test",
            relationship=ContactRelationship.INBOUND_CUSTOMER,
            consent_state=ConsentState.CUSTOMER_INITIATED,
            consent_provenance="inbound_context", source_event_ref=self.event.event_id,
        )

    def _submit(self, *, key="broker-provider-action-0001",
                expires_in=timedelta(minutes=15),
                purpose=CommunicationPurpose.REPLY_TO_INBOUND,
                content="Thanks for contacting our sandbox inbox.",
                evidence=ContentEvidence(), context_ref=None,
                role_id="role_inbox_assistant",
                action="send_preapproved_reply"):
        request = self.safety.prepare_request(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            recipient_id=self.recipient.recipient_id, purpose=purpose,
            agent_role=role_id, capability="communications.email",
            provider_connection_id=self.connection.connection_id,
            runtime_idempotency_key=key, content=content, evidence=evidence,
            context_ref=context_ref or self.event.event_id, expires_in=expires_in,
        )
        return self.agent_runtime.submit(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            role_id=role_id, capability="communications.email", action=action,
            budget_ref="budget_broker", maximum_job_spend=Money("USD", 5),
            idempotency_key=key, correlation_id=self.event.correlation_id,
            trigger_class=TriggerClass.INBOUND_EVENT, trigger_ref=self.event.event_id,
            causation_id=self.event.event_id,
            input_artifact_refs=(ArtifactRef("attachment", self.artifact.artifact_id),),
            secret_refs=(self.job_secret_ref,),
            communication_ref=request.communication_id,
            model_policy=ModelPolicy(quality_floor=70, requires_tools=True),
            expires_in=expires_in,
        )

    def test_runtime_to_policy_to_provider_receipt_and_delivery(self):
        job = self._submit()
        envelope = self._envelope(job)
        self.assertNotIn("recipient@example.test", envelope.to_json())
        receipt = self.broker.execute_provider_action(
            envelope, operation="send_preapproved_reply", secret_ref=self.job_secret_ref)
        self.assertEqual(self.recipient.recipient_id, receipt.recipient_ref)
        self.assertEqual("reply_to_inbound", receipt.communication_purpose)
        self.assertEqual("passed", receipt.content_policy_result)
        deliveries = self.repository.list_broker_records(
            "communication_delivery", self.tenant.tenant_id, self.company_id)
        self.assertEqual(1, len(deliveries))
        self.assertIs(DeliveryStatus.ACCEPTED, deliveries[0].status)
        self.assertEqual(1, self.provider.call_count)

    def test_duplicate_runtime_delivery_and_provider_action_are_suppressed(self):
        job = self._submit()
        envelope = self._envelope(job)
        first = self.broker.execute_provider_action(
            envelope, operation="send_preapproved_reply", secret_ref=self.job_secret_ref)
        second = self.broker.execute_provider_action(
            envelope, operation="send_preapproved_reply", secret_ref=self.job_secret_ref)
        self.assertEqual(first.receipt_id, second.receipt_id)
        self.assertEqual(1, self.provider.call_count)
        self.assertEqual(1, len(self.repository.list_broker_records(
            "communication_delivery", self.tenant.tenant_id, self.company_id)))

    def test_opt_out_is_durable_idempotent_and_reenable_is_explicit(self):
        changed = self.safety.opt_out(
            self.principal, tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            recipient_id=self.recipient.recipient_id, event_id="optout_event_0001")
        replay = self.safety.opt_out(
            self.principal, tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            recipient_id=self.recipient.recipient_id, event_id="optout_event_0001")
        self.assertEqual(changed, replay)
        self.assertIs(ConsentState.WITHDRAWN, replay.consent_state)
        self.assertIs(SuppressionState.SUPPRESSED, replay.suppression_state)
        envelope = self._envelope(self._submit(key="opted-out-send-0001"))
        with self.assertRaises(CommunicationDenied):
            self.broker.execute_provider_action(
                envelope, operation="send_preapproved_reply", secret_ref=self.job_secret_ref)
        restored = self.safety.explicitly_reenable(
            self.principal, tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            recipient_id=self.recipient.recipient_id,
            consent_provenance="explicit_reconsent_record")
        self.assertIs(ConsentState.EXPLICIT, restored.consent_state)
        self.assertIs(SuppressionState.CLEAR, restored.suppression_state)

    def test_unknown_consent_fails_closed_before_provider_access(self):
        unknown = replace(self.recipient, consent_state=ConsentState.UNKNOWN,
                          consent_at=None, updated_at=self.clock())
        self.repository.save_broker_record(
            "communication_recipient", unknown.recipient_id,
            unknown.tenant_id, unknown.company_id, unknown)
        envelope = self._envelope(self._submit(key="unknown-consent-0001"))
        with self.assertRaises(CommunicationDenied):
            self.broker.execute_provider_action(
                envelope, operation="send_preapproved_reply",
                secret_ref=self.job_secret_ref)
        self.assertEqual(0, self.provider.call_count)

    def test_content_policy_denies_secrets_guarantees_discounts_and_templates(self):
        cases = (
            ("Your api_key=not-a-real-value", ContentEvidence()),
            ("This result is guaranteed", ContentEvidence()),
            ("Claim your 25% off discount", ContentEvidence()),
            ("Hello {{first_name}}", ContentEvidence()),
            ("A normal sentence", ContentEvidence(unsupported_claims=True)),
            ("A normal sentence", ContentEvidence(cross_scope_data=True)),
        )
        for index, (content, evidence) in enumerate(cases):
            with self.subTest(index=index):
                envelope = self._envelope(self._submit(
                    key=f"content-denial-{index:04d}", content=content, evidence=evidence))
                with self.assertRaises(CommunicationDenied):
                    self.broker.execute_provider_action(
                        envelope, operation="send_preapproved_reply", secret_ref=self.job_secret_ref)
        self.assertEqual(0, self.provider.call_count)

    def test_unknown_context_malformed_destination_bulk_and_missing_approval_deny(self):
        with self.assertRaises(ValueError):
            CommunicationPurpose("unknown")
        with self.assertRaises(ValueError):
            self.safety.register_from_canonical_event(
                tenant_id=self.tenant.tenant_id, company_id=self.company_id,
                destination="Victim <victim@example.test>\r\nBcc:x@example.test",
                relationship=ContactRelationship.INBOUND_CUSTOMER,
                consent_state=ConsentState.CUSTOMER_INITIATED,
                consent_provenance="inbound_context", source_event_ref=self.event.event_id)
        with self.assertRaises(CommunicationDenied):
            self.safety.prepare_request(
                tenant_id=self.tenant.tenant_id, company_id=self.company_id,
                recipient_id=self.recipient.recipient_id,
                purpose=CommunicationPurpose.REVIEW_REQUEST,
                agent_role="role_review_followup_assistant",
                capability="communications.email",
                provider_connection_id=self.connection.connection_id,
                runtime_idempotency_key="missing-completion-0001", content="Please review us",
                context_ref=self.event.event_id)
        envelope = self._envelope(self._submit(key="bulk-send-denial-0001"))
        request = self.repository.get_broker_record(
            "communication_request", self.tenant.tenant_id, self.company_id,
            envelope.communication_ref)
        self.repository.save_broker_record(
            "communication_request", request.communication_id, request.tenant_id,
            request.company_id, replace(request, bulk_count=2))
        with self.assertRaises(CommunicationDenied):
            self.broker.execute_provider_action(
                envelope, operation="send_preapproved_reply", secret_ref=self.job_secret_ref)

        explicit = replace(self.recipient, consent_state=ConsentState.EXPLICIT,
                           consent_at=self.clock(), updated_at=self.clock())
        self.repository.save_broker_record("communication_recipient", explicit.recipient_id,
                                           explicit.tenant_id, explicit.company_id, explicit)
        marketing = self.safety.prepare_request(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            recipient_id=explicit.recipient_id, purpose=CommunicationPurpose.MARKETING,
            agent_role="role_inbox_assistant", capability="communications.email",
            provider_connection_id=self.connection.connection_id,
            runtime_idempotency_key="approval-required-0001", content="A quiet update. Unsubscribe.",
            evidence=ContentEvidence(required_disclosure_present=True))
        job = self.agent_runtime.submit(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            role_id="role_inbox_assistant", capability="communications.email",
            action="send_preapproved_reply", budget_ref="budget_broker",
            maximum_job_spend=Money("USD", 5), idempotency_key="approval-required-0001",
            correlation_id=self.event.correlation_id, trigger_class=TriggerClass.INBOUND_EVENT,
            trigger_ref=self.event.event_id, secret_refs=(self.job_secret_ref,),
            communication_ref=marketing.communication_id)
        with self.assertRaises(CommunicationDenied):
            self.broker.execute_provider_action(self._envelope(job),
                operation="send_preapproved_reply", secret_ref=self.job_secret_ref)

    def test_transactional_rate_limit_prevents_concurrency_bypass(self):
        limits = {purpose: {
            "company_minute": 20, "company_hour": 20, "company_day": 20,
            "recipient_minute": 1, "recipient_hour": 20, "recipient_day": 20,
            "burst": 20,
        } for purpose in CommunicationPurpose}
        self.safety.limits = limits
        first = self._envelope(self._submit(key="rate-first-0000001"))
        self.broker.execute_provider_action(
            first, operation="send_preapproved_reply", secret_ref=self.job_secret_ref)
        second = self._envelope(self._submit(key="rate-second-000001"))
        with self.assertRaises(CommunicationDenied):
            self.broker.execute_provider_action(
                second, operation="send_preapproved_reply", secret_ref=self.job_secret_ref)
        self.assertEqual(1, self.provider.call_count)

    def test_signed_delivery_callback_replay_and_bounce_suppression(self):
        envelope = self._envelope(self._submit())
        receipt = self.broker.execute_provider_action(
            envelope, operation="send_preapproved_reply", secret_ref=self.job_secret_ref)
        body, signature = self.verifier.callback(
            "delivery_event_bounce_001", receipt.provider_request_id, DeliveryStatus.BOUNCED)
        with self.assertRaises(PermissionError):
            self.safety.handle_delivery_callback(
                provider_name=self.provider.provider, body=body, signature="forged")
        first = self.safety.handle_delivery_callback(
            provider_name=self.provider.provider, body=body, signature=signature)
        replay = self.safety.handle_delivery_callback(
            provider_name=self.provider.provider, body=body, signature=signature)
        self.assertEqual(first, replay)
        self.assertIs(DeliveryStatus.BOUNCED, first.status)
        self.assertIs(SuppressionState.SUPPRESSED,
                      self.safety.get_recipient(self.principal,
                          tenant_id=self.tenant.tenant_id, company_id=self.company_id,
                          recipient_id=self.recipient.recipient_id).suppression_state)

    def test_cross_tenant_and_same_tenant_cross_company_are_non_enumerating(self):
        self.assertIsNone(self.repository.get_broker_record(
            "communication_recipient", "tenant_other", self.company_id,
            self.recipient.recipient_id))
        self.assertIsNone(self.repository.get_broker_record(
            "communication_recipient", self.tenant.tenant_id, "company_other",
            self.recipient.recipient_id))
        request = self.safety.prepare_request(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            recipient_id=self.recipient.recipient_id,
            purpose=CommunicationPurpose.REPLY_TO_INBOUND,
            agent_role="role_inbox_assistant", capability="communications.email",
            provider_connection_id=self.connection.connection_id,
            runtime_idempotency_key="scope-request-000001", content="Safe reply",
            context_ref=self.event.event_id)
        self.assertIsNone(self.repository.get_broker_record(
            "communication_request", "tenant_other", self.company_id,
            request.communication_id))

    def test_quote_and_review_paths_require_canonical_business_context(self):
        quote_event = Event("event_quote_request_001", self.tenant.tenant_id, self.company_id,
            "correlation-quote", None, "integration.quote.requested", self.clock(), {}, "test")
        completed = Event("event_service_completed_001", self.tenant.tenant_id, self.company_id,
            "correlation-review", None, "service.completed", self.clock(), {}, "test")
        self.runtime.events.publish(quote_event); self.runtime.events.publish(completed)
        quote_recipient = replace(self.recipient, relationship=ContactRelationship.QUOTE_REQUESTER,
                                  source=quote_event.event_id, updated_at=self.clock())
        self.repository.save_broker_record("communication_recipient", quote_recipient.recipient_id,
                                           quote_recipient.tenant_id, quote_recipient.company_id,
                                           quote_recipient)
        quote = self.safety.prepare_request(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            recipient_id=quote_recipient.recipient_id,
            purpose=CommunicationPurpose.QUOTE_RESPONSE,
            agent_role="role_inbox_assistant", capability="communications.email",
            provider_connection_id=self.connection.connection_id,
            runtime_idempotency_key="quote-response-000001",
            content="The authorized sandbox quote is $25.",
            evidence=ContentEvidence(pricing_authorized=True,
                                     company_fact_refs=("pricing_policy_001",)),
            context_ref=quote_event.event_id)
        self.assertEqual(frozenset(), quote.content_flags)

        review_recipient = replace(quote_recipient,
            relationship=ContactRelationship.SERVICE_RECIPIENT,
            consent_state=ConsentState.SERVICE_FOLLOWUP, source=completed.event_id,
            updated_at=self.clock())
        self.repository.save_broker_record("communication_recipient", review_recipient.recipient_id,
                                           review_recipient.tenant_id, review_recipient.company_id,
                                           review_recipient)
        review = self.safety.prepare_request(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            recipient_id=review_recipient.recipient_id,
            purpose=CommunicationPurpose.REVIEW_REQUEST,
            agent_role="role_review_followup_assistant", capability="communications.email",
            provider_connection_id=self.connection.connection_id,
            runtime_idempotency_key="review-request-00001",
            content="Please share an honest review.", context_ref=completed.event_id)
        self.assertEqual(frozenset(), review.content_flags)

    def test_denial_audit_and_persistence_contain_no_body_or_destination(self):
        envelope = self._envelope(self._submit(
            key="audit-denial-000001", content="This is guaranteed"))
        with self.assertRaises(CommunicationDenied):
            self.broker.execute_provider_action(
                envelope, operation="send_preapproved_reply", secret_ref=self.job_secret_ref)
        decisions = self.repository.list_broker_records(
            "communication_policy_decision", self.tenant.tenant_id, self.company_id)
        self.assertTrue(any(item.outcome is PolicyOutcome.DENIED for item in decisions))
        audit = repr(self.repository.list_audit(self.tenant.tenant_id, self.company_id))
        dump = "\n".join(self.repository.connection.iterdump())
        self.assertNotIn("This is guaranteed", audit + dump)
        self.assertNotIn("recipient@example.test", audit)


if __name__ == "__main__":
    unittest.main()
