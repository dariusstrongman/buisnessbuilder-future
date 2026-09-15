from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from uuid import uuid4

import pytest

from businessbuilder.commercial import CommercialService, RecordingCommercialEventSink, seed_default_catalog
from businessbuilder.commercial.models import EntitlementStatus, PaymentEligibility, TaxDisposition
from businessbuilder.commercial.pricing import OfferCode
from businessbuilder.commercial.stripe_webhooks import StripeWebhookIngress
from businessbuilder.commercial.stripe_test import StripeTestPaymentProvider
from businessbuilder.identity import AuthorizationContext, IdentityService, InMemoryIdentityRepository
from businessbuilder.identity import FakeDevAuthenticationProvider, SessionService
from businessbuilder.customer_api.bootstrap import create_postgres_customer_api
from businessbuilder.postgres import PostgresCommercialRepository, PostgresIdentityRepository
from businessbuilder.runtime.ids import DeterministicIds
from tests.commercial.test_supervised_checkout import NOW, provider, session_object, signed_event


@pytest.mark.skipif(not os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"), reason="isolated PostgreSQL required")
def test_signed_payment_restart_idempotency_and_tenant_scope():
    dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
    prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
    schema = f"{prefix}_checkout_{uuid4().hex[:10]}"
    ids = DeterministicIds()
    identity_repo = InMemoryIdentityRepository()
    identity = IdentityService(identity_repo, id_factory=ids, clock=lambda: NOW)
    user, _ = identity.register_founder("pg-checkout-founder@example.test", "Checkout Founder")
    tenant, _, _ = identity.create_account(user.user_id, "Checkout Organization")
    company_id = "company_pg_checkout"
    identity.attach_company(AuthorizationContext(user.user_id, tenant.tenant_id), company_id)
    context = AuthorizationContext(user.user_id, tenant.tenant_id, company_id)
    repo = PostgresCommercialRepository(dsn, schema=schema)
    seed_default_catalog(repo, effective_at=NOW)
    service = CommercialService(repo, identity.authorization, RecordingCommercialEventSink(),
                                id_factory=ids, clock=lambda: NOW)
    order = service.create_order(context, "product_version_build_business_v1", offer_code=OfferCode.BUSINESS)
    service.record_payment_readiness(
        tenant_id=context.tenant_id, company_id=company_id, order_id=order.order_id,
        eligibility=PaymentEligibility.PAY_NOW_ELIGIBLE, eligible_at=None,
        tax_disposition=TaxDisposition.NON_TAXABLE, review_ref="isolated_test_review_0001",
    )
    checkout = service.create_checkout(context, order.order_id, "isolated-test-retry-key-0001")
    repo.save_checkout(replace(checkout, provider_ref="cs_test_fixture_0001",
                               redirect_url="https://checkout.stripe.com/c/pay/fixture"))
    repo.close()

    repo = PostgresCommercialRepository(dsn, schema=schema)
    service = CommercialService(repo, identity.authorization, RecordingCommercialEventSink(),
                                id_factory=ids, clock=lambda: NOW)
    adapter, _ = provider()
    ingress = StripeWebhookIngress(repo, service, adapter)
    signature, raw = signed_event("checkout.session.completed", session_object(order))
    assert ingress.handle(signature, raw) == 2
    assert repo.get_current_entitlement_grants(context.tenant_id, company_id)
    assert not repo.get_current_entitlement_grants("tenant_wrong", company_id)
    repo.close()

    repo = PostgresCommercialRepository(dsn, schema=schema)
    service = CommercialService(repo, identity.authorization, RecordingCommercialEventSink(),
                                id_factory=ids, clock=lambda: NOW)
    ingress = StripeWebhookIngress(repo, service, adapter)
    history = repo.order_history(context.tenant_id, company_id, order.order_id)
    assert ingress.handle(signature, raw) == 0
    assert repo.order_history(context.tenant_id, company_id, order.order_id) == history
    assert not repo.get_current_entitlement_grants("tenant_wrong", company_id)
    assert repo.get_current_entitlement_grants(context.tenant_id, company_id)
    repo.close()


@pytest.mark.skipif(not os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"), reason="isolated PostgreSQL required")
def test_running_business_audit_persists_without_a_fixed_payment_order():
    dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
    prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
    schema = f"{prefix}_existing_{uuid4().hex[:10]}"
    ids = DeterministicIds()
    identity_repo = PostgresIdentityRepository(dsn, schema=schema)
    identity = IdentityService(identity_repo, id_factory=ids, clock=lambda: NOW)
    founder = identity.register_user("pg-existing-founder@example.test")
    fake_auth = FakeDevAuthenticationProvider()
    fake_auth.register(founder.user_id, founder.email, "existing-test-session")
    sessions = SessionService(identity_repo, fake_auth, id_factory=ids, clock=lambda: NOW)
    _, token = sessions.sign_in(founder.email, "existing-test-session")
    identity_repo.close()
    application = create_postgres_customer_api(
        signing_key=b"isolated-checkout-principal-signing-key-v1",
        dsn=dsn, schema=schema, clock=lambda: NOW,
    )
    headers = {"Authorization": f"Bearer {token}"}
    started = application.handle(
        method="POST", path="/api/v1/pilots/residential-cleaning/intakes",
        headers=headers, query={}, request_id="req_existing_pg", correlation_id="corr_existing_pg",
        body={"idempotency_key": "postgres-existing-cleaning-0001", "intake": {
            "starting_point": "running",
            "idea": "Improve a working residential cleaning business in Denton, Texas.",
            "founder_display_name": "Existing Founder", "organization_name": "Existing Cleaning Organization",
            "company_name": "Existing Cleaning", "country": "US", "region": "TX", "locality": "Denton",
            "service_radius_miles": 12, "weekly_hours": 35, "startup_budget_minor": 300000,
            "working_preferences": {},
        }},
    )
    assert started.status == 201
    company_id = started.body["journey"]["company"]["company_id"]
    approval = application.handle(
        method="POST", path=f"/api/v1/companies/{company_id}/residential-cleaning-pilot/approve",
        headers=headers, query={}, request_id="req_existing_pg", correlation_id="corr_existing_pg",
        body={"approval_id": started.body["journey"]["scope_commit"]["approval_id"]},
    )
    assert approval.status == 200 and approval.body["journey"]["order"] is None
    audit_path = f"/api/v1/companies/{company_id}/residential-cleaning-pilot/existing-business-audit"
    captured = application.handle(
        method="POST", path=audit_path, headers=headers, query={},
        request_id="req_existing_pg", correlation_id="corr_existing_pg",
        body={"systems": [{"system": "inbox", "assessment": "improve",
                           "issue": "Current replies are manually routed.", "provider_reference": "inbox_ref_0001"}]},
    )
    assert captured.status == 201
    application.close()
    restarted = create_postgres_customer_api(
        signing_key=b"isolated-checkout-principal-signing-key-v1",
        dsn=dsn, schema=schema, clock=lambda: NOW,
    )
    projected = restarted.handle(
        method="GET", path=f"/api/v1/companies/{company_id}/residential-cleaning-pilot",
        headers=headers, query={}, body=None, request_id="req_existing_pg", correlation_id="corr_existing_pg",
    )
    assert projected.status == 200
    assert projected.body["journey"]["existing_business_audit"]["data"]["systems"][0]["system"] == "inbox"
    assert projected.body["journey"]["order"] is None
    assert not projected.body["journey"]["verification"]["ready"]
    restarted.close()
