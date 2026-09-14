from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import unittest

from tests.customer_api import test_customer_api as customer_api_tests

from businessbuilder.communications_compliance.models import (
    AlertClass, AlertSeverity, ConsentEvidence, ErasureRecord, OperationalAlert,
)
from businessbuilder.identity import Permission, Role
from businessbuilder.outbound_communications import (
    CommunicationPurpose, ConsentState, DestinationType,
)


class ComplianceApiStub:
    def __init__(self, fixture):
        self.fixture = fixture
        self.unsubscribed = 0
        self.paused = False
        self.consent = ConsentEvidence(
            "consent_api_001", fixture.tenant.tenant_id, fixture.company_id,
            "recipient_api_001", CommunicationPurpose.REPLY_TO_INBOUND,
            DestinationType.EMAIL, ConsentState.CUSTOMER_INITIATED,
            "canonical_event", fixture.now, "event_api_001", "evidence_api_001",
            "communications-compliance.us-federal.v1", "terms.v1",
            fixture.now + timedelta(days=30))
        self.alert = OperationalAlert(
            "alert_api_001", fixture.tenant.tenant_id, fixture.company_id,
            AlertClass.BOUNCE_SPIKE, AlertSeverity.WARNING,
            "hard_bounce_threshold_warning", "safe_source_ref", fixture.now)

    def unsubscribe(self, token):
        self.unsubscribed += 1
        return token == "valid-test-token"

    def customer_status(self, principal, **scope):
        del principal
        if (scope["tenant_id"], scope["company_id"]) != (
                self.fixture.tenant.tenant_id, self.fixture.company_id):
            raise LookupError
        return {"connection": "connected", "mode": "sandbox_only",
                "sending": "paused" if self.paused else "sandbox",
                "suppressed_recipients_present": True,
                "action_required": self.paused, "live_send_enabled": False}

    def set_company_kill_switch(self, principal, **scope):
        del principal
        self.paused = scope["engaged"]
        return None

    def list_alerts(self, principal, **scope):
        self.customer_status(principal, **scope)
        return (self.alert,)

    def list_consent(self, principal, **scope):
        self.customer_status(principal, tenant_id=scope["tenant_id"],
                             company_id=scope["company_id"])
        if scope["recipient_id"] != self.consent.recipient_id:
            raise LookupError
        return (self.consent,)

    def request_erasure(self, principal, **scope):
        self.customer_status(principal, tenant_id=scope["tenant_id"],
                             company_id=scope["company_id"])
        if scope["recipient_id"] != self.consent.recipient_id:
            raise LookupError
        return ErasureRecord(
            "erasure_api_001", scope["tenant_id"], scope["company_id"],
            scope["recipient_id"], "owner_api", scope["reason"], self.fixture.now,
            self.fixture.now, True, True, False)


class ComplianceCustomerApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = customer_api_tests.CustomerApiTests(methodName="runTest")
        self.fixture.setUp()
        self.stub = ComplianceApiStub(self.fixture)
        self.fixture.api.communications_compliance = self.stub
        self.fixture.api.live_canary_readiness = type("CanaryStatusStub", (), {
            "customer_status": lambda stub, principal, **scope: {
                "provider": "connected", "mode": "Sandbox testing",
                "status": "Canary review pending", "live_send_enabled": False,
            }
        })()

    def tearDown(self):
        self.fixture.tearDown()

    def request(self, *args, **kwargs):
        return self.fixture.request(*args, **kwargs)

    def test_public_unsubscribe_is_login_free_and_oracle_safe(self):
        for token in ("valid-test-token", "forged-test-token"):
            response = self.request("POST", "/api/v1/communications/unsubscribe",
                                    body={"token": token})
            self.assertEqual(202, response.status)
            self.assertEqual("accepted", response.body["status"])
            self.assertNotIn(token, repr(response.body))
        self.assertEqual(405, self.request(
            "GET", "/api/v1/communications/unsubscribe").status)

    def test_customer_safe_status_alert_consent_and_erasure(self):
        base = f"/api/v1/companies/{self.fixture.company_id}"
        paths = (
            f"{base}/communications/compliance-status",
            f"{base}/communications/alerts",
            f"{base}/recipients/{self.stub.consent.recipient_id}/consent",
        )
        responses = [self.request("GET", path, token=self.fixture.owner_token)
                     for path in paths]
        erasure = self.request(
            "POST", f"{base}/recipients/{self.stub.consent.recipient_id}/erasure",
            token=self.fixture.owner_token, body={"reason": "recipient_request"})
        self.assertTrue(all(r.status == 200 for r in responses))
        self.assertEqual(202, erasure.status)
        body = repr([r.body for r in responses] + [erasure.body]).lower()
        for forbidden in ("provider_payload", "secret", "risk_score", "destination"):
            self.assertNotIn(forbidden, body)

    def test_owner_admin_can_pause_member_and_support_cannot(self):
        base = f"/api/v1/companies/{self.fixture.company_id}/communications/send-state"
        _, admin_token = self.fixture._member(Role.ADMIN, "compliance-admin")
        _, member_token = self.fixture._member(Role.MEMBER, "compliance-member")
        self.assertEqual(200, self.request(
            "POST", base, token=admin_token,
            body={"paused": True, "reason_code": "manual_pause"}).status)
        self.assertEqual(403, self.request(
            "POST", base, token=member_token,
            body={"paused": False, "reason_code": "bypass_attempt"}).status)
        support, support_token = self.fixture._member(Role.SUPPORT, "compliance-support")
        grant = self.fixture.identity.grant_support_access(
            self.fixture.owner_context, support.user_id,
            frozenset({Permission.VIEW_COMPANY_STATE}), timedelta(minutes=5),
            "compliance status support", company_id=self.fixture.company_id)
        session = self.fixture.identity.start_support_session(
            support.user_id, grant.grant_id, "compliance status support")
        headers = {"X-Support-Impersonation-Session": session.impersonation_session_id}
        self.assertEqual(200, self.request("GET", base, token=support_token,
                                           headers=headers).status)
        self.assertEqual(403, self.request(
            "POST", base, token=support_token, headers=headers,
            body={"paused": False, "reason_code": "support_bypass"}).status)

    def test_client_cannot_enable_live_sending_or_cross_company_scope(self):
        base = f"/api/v1/companies/{self.fixture.company_id}/communications/send-state"
        response = self.request(
            "POST", base, token=self.fixture.owner_token,
            body={"paused": False, "reason_code": "client_attempt",
                  "live_send_enabled": True, "rollout_tier": "general_live"})
        self.assertEqual(400, response.status)
        other = self.request(
            "GET", "/api/v1/companies/company_other/communications/compliance-status",
            token=self.fixture.owner_token)
        self.assertEqual(404, other.status)

    def test_canary_readiness_is_read_only_customer_safe_status(self):
        path = (f"/api/v1/companies/{self.fixture.company_id}"
                "/communications/canary-readiness")
        response = self.request("GET", path, token=self.fixture.owner_token)
        self.assertEqual(200, response.status)
        self.assertEqual("Sandbox testing", response.body["communications"]["mode"])
        self.assertFalse(response.body["communications"]["live_send_enabled"])
        self.assertEqual(405, self.request(
            "POST", path, token=self.fixture.owner_token,
            body={"live_send_enabled": True}).status)


if __name__ == "__main__":
    unittest.main()
