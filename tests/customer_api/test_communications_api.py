from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import unittest

from tests.customer_api import test_customer_api as customer_api_tests

from businessbuilder.identity import Permission, Role
from businessbuilder.outbound_communications import (
    CommunicationPurpose, ConsentState, ContactRelationship, DeliveryRecord,
    DeliveryStatus, DestinationType, RecipientRecord, SuppressionState,
)


class CommunicationApiStub:
    def __init__(self, fixture):
        self.fixture = fixture
        now = fixture.now
        self.recipient = RecipientRecord(
            "recipient_api_001", fixture.tenant.tenant_id, fixture.company_id,
            "customer@example.test", DestinationType.EMAIL, "event_inbound_api_001",
            ContactRelationship.INBOUND_CUSTOMER, ConsentState.CUSTOMER_INITIATED,
            "canonical_inbound", now, SuppressionState.CLEAR, None, None, None, (), now, now)
        self.delivery = DeliveryRecord(
            "delivery_api_001", fixture.tenant.tenant_id, fixture.company_id,
            "communication_api_001", self.recipient.recipient_id, "sandbox-email",
            "connection_api_001", "receipt_api_001", "provider_request_api_001",
            CommunicationPurpose.REPLY_TO_INBOUND, DeliveryStatus.DELIVERED,
            "sandbox_delivered", "sandbox_object_001", "callback_verified", now, now)

    def get_recipient(self, principal, **scope):
        del principal
        if (scope["tenant_id"], scope["company_id"], scope["recipient_id"]) != (
            self.recipient.tenant_id, self.recipient.company_id, self.recipient.recipient_id):
            raise LookupError
        return self.recipient

    def opt_out(self, principal, **scope):
        self.get_recipient(principal, tenant_id=scope["tenant_id"],
                           company_id=scope["company_id"], recipient_id=scope["recipient_id"])
        self.recipient = replace(self.recipient, consent_state=ConsentState.WITHDRAWN,
                                 suppression_state=SuppressionState.SUPPRESSED,
                                 suppression_reason=scope["reason"],
                                 opt_out_at=self.fixture.now, updated_at=self.fixture.now)
        return self.recipient

    def explicitly_reenable(self, principal, **scope):
        self.get_recipient(principal, tenant_id=scope["tenant_id"],
                           company_id=scope["company_id"], recipient_id=scope["recipient_id"])
        self.recipient = replace(self.recipient, consent_state=ConsentState.EXPLICIT,
                                 consent_provenance=scope["consent_provenance"],
                                 suppression_state=SuppressionState.CLEAR,
                                 suppression_reason=None, opt_out_at=None)
        return self.recipient

    def policy_status(self, principal, **scope):
        del principal, scope
        return {"version": "outbound-email.v1", "channel": "email",
                "sandbox_only": True, "bulk_enabled": False,
                "purposes": ("reply_to_inbound", "quote_response", "review_request")}

    def list_deliveries(self, principal, **scope):
        del principal
        return (self.delivery,) if (scope["tenant_id"], scope["company_id"]) == (
            self.delivery.tenant_id, self.delivery.company_id) else ()

    def get_delivery(self, principal, **scope):
        del principal
        if scope["delivery_id"] != self.delivery.delivery_id:
            raise LookupError
        return self.delivery


class CommunicationsCustomerApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = customer_api_tests.CustomerApiTests(methodName="runTest")
        self.fixture.setUp()
        self.stub = CommunicationApiStub(self.fixture)
        self.fixture.api.outbound_communications = self.stub

    def tearDown(self):
        self.fixture.tearDown()

    def request(self, *args, **kwargs):
        return self.fixture.request(*args, **kwargs)

    def test_customer_safe_status_policy_history_and_delivery_routes(self):
        base = f"/api/v1/companies/{self.fixture.company_id}"
        responses = (
            self.request("GET", f"{base}/recipients/{self.stub.recipient.recipient_id}", token=self.fixture.owner_token),
            self.request("GET", f"{base}/recipients/{self.stub.recipient.recipient_id}/suppression", token=self.fixture.owner_token),
            self.request("GET", f"{base}/communication-policy", token=self.fixture.owner_token),
            self.request("GET", f"{base}/communications", token=self.fixture.owner_token),
            self.request("GET", f"{base}/deliveries/{self.stub.delivery.delivery_id}", token=self.fixture.owner_token),
        )
        self.assertTrue(all(item.status == 200 for item in responses))
        serialized = repr([item.body for item in responses]).lower()
        for forbidden in ("secret_ref", "arn:", "provider_request", "risk_flags", "customer@example.test"):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("c***@example.test", serialized)

    def test_owner_admin_manage_member_denied_and_methods_are_strict(self):
        base = f"/api/v1/companies/{self.fixture.company_id}/recipients/{self.stub.recipient.recipient_id}"
        _, admin_token = self.fixture._member(Role.ADMIN, "communications-admin")
        _, member_token = self.fixture._member(Role.MEMBER, "communications-member")
        self.assertEqual(200, self.request("POST", f"{base}/opt-out", token=admin_token,
            body={"event_id": "optout_api_001"}).status)
        self.assertEqual(403, self.request("POST", f"{base}/re-enable", token=member_token,
            body={"consent_provenance": "explicit_reconsent"}).status)
        self.assertEqual(200, self.request("POST", f"{base}/re-enable", token=self.fixture.owner_token,
            body={"consent_provenance": "explicit_reconsent"}).status)
        response = self.request("DELETE", base, token=self.fixture.owner_token)
        self.assertEqual(405, response.status)
        self.assertEqual(("GET",), response.allow)
        self.assertTrue(any(
            event.action == "authorization.denied"
            and event.target_id == "communications.manage"
            for event in self.fixture.identity_repository.list_audit(self.fixture.tenant.tenant_id)
        ))

    def test_support_may_view_only_with_scoped_session_and_never_manage(self):
        support, token = self.fixture._member(Role.SUPPORT, "communications-support")
        grant = self.fixture.identity.grant_support_access(
            self.fixture.owner_context, support.user_id,
            frozenset({Permission.VIEW_COMPANY_STATE}),
            timedelta(minutes=5), "communications support view",
            company_id=self.fixture.company_id)
        session = self.fixture.identity.start_support_session(
            support.user_id, grant.grant_id, "communications support session")
        headers = {"X-Support-Impersonation-Session": session.impersonation_session_id}
        base = f"/api/v1/companies/{self.fixture.company_id}/recipients/{self.stub.recipient.recipient_id}"
        self.assertEqual(200, self.request("GET", base, token=token, headers=headers).status)
        self.assertEqual(403, self.request("POST", f"{base}/opt-out", token=token,
            headers=headers, body={"event_id": "support_optout_001"}).status)

    def test_wrong_company_and_direct_object_reference_do_not_enumerate(self):
        path = f"/api/v1/companies/company_other/recipients/{self.stub.recipient.recipient_id}"
        response = self.request("GET", path, token=self.fixture.owner_token)
        self.assertEqual(404, response.status)
        missing = self.request("GET",
            f"/api/v1/companies/{self.fixture.company_id}/recipients/recipient_missing_001",
            token=self.fixture.owner_token)
        self.assertEqual(404, missing.status)


if __name__ == "__main__":
    unittest.main()
