"""Create one unpaid, exact-order sandbox Checkout for decline/expiry tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from urllib.parse import parse_qsl, urlencode, urlparse
from uuid import uuid4

from businessbuilder.commercial import CommercialService, RecordingCommercialEventSink
from businessbuilder.commercial.models import PaymentEligibility, TaxDisposition, TaxReviewState
from businessbuilder.commercial.operator_authority import CommercialOperatorAuthority
from businessbuilder.commercial.pricing import OfferCode
from businessbuilder.commercial.stripe_test import StripeTestPaymentProvider, _default_transport
from businessbuilder.identity import (
    AuthorizationContext, FakeDevAuthenticationProvider, IdentityService,
    SessionService,
)
from businessbuilder.postgres import PostgresCommercialRepository, PostgresIdentityRepository

from stripe_sandbox_pg_acceptance import _test_key


def main() -> None:
    dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
    if urlparse(dsn).hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("only disposable loopback PostgreSQL is permitted")
    schema = os.environ["BUSINESS_BUILDER_STRIPE_SANDBOX_SCHEMA"]
    anchor_ref = os.environ["BUSINESS_BUILDER_STRIPE_SANDBOX_REFERENCE_SESSION"]
    clock_customer_ref = os.environ.get("BUSINESS_BUILDER_STRIPE_SANDBOX_CLOCK_CUSTOMER_REF")
    if clock_customer_ref and not clock_customer_ref.startswith("cus_"):
        raise RuntimeError("test clock Customer reference invalid")
    identity_repo = PostgresIdentityRepository(dsn, schema=schema)
    commercial_repo = PostgresCommercialRepository(dsn, schema=schema)
    anchor = commercial_repo.get_checkout_by_provider_ref(anchor_ref)
    founder = identity_repo.get_user(anchor.user_id)
    context = AuthorizationContext(anchor.user_id, anchor.tenant_id, anchor.company_id)
    operator_ids = {
        history[-1].operator_user_id for history in commercial_repo.operator_grants.values()
        if history[-1].tenant_id == anchor.tenant_id
        and history[-1].company_id == anchor.company_id
    }
    if len(operator_ids) != 1:
        raise RuntimeError("isolated operator appointment not found")
    operator_user_id = next(iter(operator_ids))
    now = datetime.now(timezone.utc)
    ids = lambda kind: f"{kind}_sandbox_{uuid4().hex}"
    sessions = SessionService(
        identity_repo, provider=FakeDevAuthenticationProvider(), id_factory=ids,
        clock=lambda: now,
    )
    _, raw_operator_session = sessions.issue_for_user(
        operator_user_id, provider_name="cognito", provider_session_id=ids("test_provider_session")
    )
    authority = CommercialOperatorAuthority(
        identity_repo, commercial_repo,
        signing_key=b"isolated_sandbox_operator_test_signing_key_v1",
        clock=lambda: now,
        privileged_provisioner_verifier=lambda value: "platform_test_operator" if value == "isolated_test_control" else "",
    )
    grant = authority.provision(
        "isolated_test_control", grant_id=ids("operator_grant"),
        operator_user_id=operator_user_id, tenant_id=context.tenant_id,
        company_id=context.company_id, actions=frozenset({"payment.release"}),
        starts_at=now, ends_at=now + timedelta(minutes=30),
        reason_code="isolated_decline_expiry_test",
    )
    principal = authority.issue(
        raw_operator_session, tenant_id=context.tenant_id,
        company_id=context.company_id, grant_id=grant.grant_id,
    )
    identity = IdentityService(identity_repo, id_factory=ids, clock=lambda: now)
    commercial = CommercialService(
        commercial_repo, identity.authorization, RecordingCommercialEventSink(),
        id_factory=ids, clock=lambda: now, operator_authority=authority,
        allow_test_admission=True,
    )
    order = commercial.create_order(
        context, "product_version_build_business_v1", offer_code=OfferCode.BUSINESS,
    )
    if clock_customer_ref:
        order = commercial.select_fixed_offer(context, order.order_id, OfferCode.BUSINESS_RUN)
    commercial.release_payment(
        principal, tenant_id=context.tenant_id, company_id=context.company_id,
        order_id=order.order_id, eligibility=PaymentEligibility.PAY_NOW_ELIGIBLE,
        eligible_at=None, tax_disposition=TaxDisposition.TEST_MODE_UNDETERMINED,
        tax_review_state=TaxReviewState.TEST_MODE_UNDETERMINED,
        decision_ref=ids("operator_test_decision"), tax_review_ref=None,
        expires_at=now + timedelta(minutes=20),
    )
    checkout = commercial.create_checkout(context, order.order_id, ids("checkout_retry"))
    priced = commercial_repo.get_order(context.tenant_id, context.company_id, order.order_id)
    key = _test_key(os.environ["BUSINESS_BUILDER_STRIPE_TEST_SECRET_REF"])
    expected_account = os.environ["BUSINESS_BUILDER_STRIPE_TEST_ACCOUNT_ID"]
    account = _default_transport(
        "GET", "https://api.stripe.com/v1/account", None,
        {"Authorization": f"Bearer {key}"},
    )
    if account.get("id") != expected_account:
        raise RuntimeError("Stripe credential points to the wrong sandbox account")

    def sandbox_transport(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> dict:
        if clock_customer_ref and method == "POST" and url.endswith("/checkout/sessions"):
            fields = [(name, value) for name, value in parse_qsl((body or b"").decode())
                      if name != "customer_email"]
            fields.append(("customer", clock_customer_ref))
            body = urlencode(fields).encode("ascii")
        return _default_transport(method, url, body, headers)

    provider = StripeTestPaymentProvider(
        api_key=key, webhook_secret="whsec_" + "unused_local_checkout_probe_" * 2,
        transport=sandbox_transport,
    )
    provider_ref, redirect = provider.open_checkout(
        order=priced, idempotency_key=f"bb-sandbox-decline-{uuid4().hex}",
        success_url="https://example.com/success", cancel_url="https://example.com/cancel",
        customer_email=founder.email,
    )
    commercial_repo.save_checkout(replace(checkout, provider_ref=provider_ref, redirect_url=redirect))
    if any(grant.source_order_id == order.order_id for grant in commercial_repo.get_current_entitlement_grants(context.tenant_id, context.company_id)):
        raise RuntimeError("unpaid failure probe activated entitlement")
    print(json.dumps({"status": "clock_bundle_checkout_open" if clock_customer_ref else "failure_checkout_open", "schema": schema,
                      "order_id": order.order_id, "session_ref": provider_ref,
                      "entitlement_from_this_order": 0,
                      "first_due_minor": priced.total.minor_units if priced.total else None,
                      "clock_customer_ref": clock_customer_ref}))
    commercial_repo.close()
    identity_repo.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "failed", "failure_class": type(exc).__name__,
                          "safe_reason": str(exc)[:160] if type(exc) is RuntimeError else None}))
        raise SystemExit(1) from None
