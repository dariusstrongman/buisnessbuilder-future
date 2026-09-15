"""Running-cleaning admission path: operator scope, real quote, founder approval."""

from dataclasses import replace
from datetime import timedelta
from http import HTTPStatus

from businessbuilder.commercial.models import PaymentEligibility, TaxDisposition, TaxReviewState
from businessbuilder.commercial.operator_authority import CommercialOperatorAuthority
from businessbuilder.identity import AuthorizationContext, Role
import test_journey


def test_running_cleaning_operator_quote_and_delay_projection():
    fixture = test_journey.ResidentialCleaningJourneyTests(methodName="test_running_business_never_becomes_a_fixed_charge_without_an_audit_quote")
    fixture.setUp()
    try:
        intake = fixture._intake("Operator Audited Cleaning")
        intake["starting_point"] = "running"
        started = fixture.request(
            "POST", "/api/v1/pilots/residential-cleaning/intakes",
            token=fixture.founder_token,
            body={"idempotency_key": "existing-operator-intake-0001", "intake": intake},
        )
        assert started.status is HTTPStatus.CREATED
        company_id = started.body["journey"]["company"]["company_id"]
        approved = fixture.request(
            "POST", f"/api/v1/companies/{company_id}/residential-cleaning-pilot/approve",
            token=fixture.founder_token,
            body={"approval_id": started.body["journey"]["scope_commit"]["approval_id"]},
        )
        assert approved.status is HTTPStatus.OK and approved.body["journey"]["order"] is None
        inventory = {"systems": [
            {"system": "website", "assessment": "keep", "issue": "Current form reaches founder.", "provider_reference": "site_0001"},
            {"system": "crm", "assessment": "missing", "issue": "No durable lead pipeline exists.", "provider_reference": None},
        ]}
        captured = fixture.request(
            "POST", f"/api/v1/companies/{company_id}/residential-cleaning-pilot/existing-business-audit",
            token=fixture.founder_token, body=inventory,
        )
        assert captured.status is HTTPStatus.CREATED
        tenant_id = fixture.identity_repository.list_user_memberships(fixture.founder.user_id)[0].tenant_id
        operator = fixture.identity.register_user("test-operator@example.test")
        fixture.identity_repository.save_user(replace(
            operator, authentication_provider="cognito",
            provider_subject_digest="test_only_subject", email_verified_at=fixture.now,
        ))
        invited = fixture.identity.invite_member(
            AuthorizationContext(fixture.founder.user_id, tenant_id, company_id),
            operator.user_id, Role.SUPPORT,
        )
        fixture.identity.accept_membership(invited.membership_id, operator.user_id)
        _, operator_token = fixture.sessions.issue_for_user(
            operator.user_id, provider_name="cognito", provider_session_id="test_only_session",
        )
        authority = CommercialOperatorAuthority(
            fixture.identity_repository, fixture.commercial_repository,
            signing_key=b"test_only_operator_signing_key_32_bytes",
            clock=lambda: fixture.now,
            privileged_provisioner_verifier=lambda value: "test_platform_provisioner" if value == "test_privileged" else "",
        )
        grant = authority.provision(
            "test_privileged", grant_id="grant_existing_cleaning_test", operator_user_id=operator.user_id,
            tenant_id=tenant_id, company_id=company_id,
            actions=frozenset({"existing_scope.publish", "quote.publish", "payment.release"}),
            starts_at=fixture.now, ends_at=fixture.now + timedelta(hours=1),
            reason_code="supervised_test_review",
        )
        fixture.commercial.operator_authority = authority
        fixture.api.commercial_operator_authority = authority
        scoped = fixture.request(
            "POST", f"/api/v1/operator/companies/{company_id}/residential-cleaning/existing-scope",
            token=operator_token, body={
                "grant_id": grant.grant_id,
                "findings": [
                    {"system": "website", "decision": "keep", "reason": "Existing form needs an independent lead-delivery test."},
                    {"system": "crm", "decision": "add", "reason": "A durable lead pipeline is needed before managed follow-up."},
                ],
                "citation_ids": ["sba_launch"],
            },
        )
        assert scoped.status is HTTPStatus.CREATED, scoped.body
        unpriced = fixture.request(
            "POST", f"/api/v1/companies/{company_id}/residential-cleaning-pilot/existing-order",
            token=fixture.founder_token, body={},
        )
        assert unpriced.status is HTTPStatus.CREATED, unpriced.body
        assert unpriced.body["order"]["priced"] is False
        order_id = unpriced.body["order"]["order_id"]
        quote = fixture.request(
            "POST", f"/api/v1/operator/companies/{company_id}/orders/{order_id}/quote",
            token=operator_token, body={
                "grant_id": grant.grant_id, "upfront_minor": 169500,
                "expires_at": (fixture.now + timedelta(days=3)).isoformat(),
            },
        )
        assert quote.status is HTTPStatus.CREATED, quote.body
        assert quote.body["quote"]["upfront"]["minor_units"] == 169500
        founder_accept = fixture.api.handle(
            method="POST", path=f"/api/v1/orders/{order_id}/quote/approve",
            headers={"Authorization": f"Bearer {fixture.founder_token}"},
            query={"company_id": [company_id]},
            body={"quote_id": quote.body["quote"]["quote_id"],
                  "recommendation_digest": quote.body["quote"]["recommendation_digest"]},
            request_id="test_quote_approval", correlation_id="test_quote_approval",
        )
        assert founder_accept.status is HTTPStatus.OK, founder_accept.body
        delayed = fixture.request(
            "POST", f"/api/v1/operator/companies/{company_id}/orders/{order_id}/release",
            token=operator_token, body={
                "grant_id": grant.grant_id, "eligibility": PaymentEligibility.PAYMENT_DELAY_REQUIRED.value,
                "eligible_at": (fixture.now + timedelta(minutes=30)).isoformat(),
                "tax_disposition": TaxDisposition.MANUAL_REVIEW.value,
                "tax_review_state": TaxReviewState.TAX_REVIEW_REQUIRED.value,
                "decision_ref": "waiting_period_technical_test",
                "expires_at": (fixture.now + timedelta(minutes=45)).isoformat(),
            },
        )
        assert delayed.status is HTTPStatus.OK, delayed.body
        assert delayed.body["order"]["payment_eligibility"] == "PAYMENT_DELAY_REQUIRED"
        projected = fixture.request(
            "GET", f"/api/v1/companies/{company_id}/residential-cleaning-pilot",
            token=fixture.founder_token,
        )
        assert projected.status is HTTPStatus.OK, projected.body
        assert projected.body["journey"]["order"]["order_id"] == order_id
        assert projected.body["journey"]["existing_business_scope"] is not None
        assert projected.body["journey"]["verification"]["ready"] is False
    finally:
        fixture.tearDown()
