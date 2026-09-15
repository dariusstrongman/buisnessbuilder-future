"""Bounded Stripe sandbox Checkout probe against disposable local PostgreSQL.

This intentionally creates unpaid test Checkout sessions only. It uses the
commercial authority flow and never grants an entitlement from a redirect.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from urllib.parse import urlparse
from uuid import uuid4

import boto3

from businessbuilder.commercial import (
    CommercialService, RecordingCommercialEventSink, seed_default_catalog,
)
from businessbuilder.commercial.models import PaymentEligibility, TaxDisposition, TaxReviewState
from businessbuilder.commercial.operator_authority import CommercialOperatorAuthority
from businessbuilder.commercial.pricing import OfferCode
from businessbuilder.commercial.stripe_test import StripeTestPaymentProvider
from businessbuilder.identity import (
    AuthorizationContext, FakeDevAuthenticationProvider, IdentityService,
    Role, SessionService,
)
from businessbuilder.postgres import PostgresCommercialRepository, PostgresIdentityRepository


def _test_key(secret_ref: str) -> str:
    value = boto3.client("secretsmanager", region_name="us-east-1").get_secret_value(
        SecretId=secret_ref
    )["SecretString"]
    if value.startswith("sk_test_"):
        key = value
    else:
        payload = json.loads(value)
        key = next((payload.get(name) for name in (
            "STRIPE_TEST_SECRET_KEY", "stripe_test_secret_key", "secret_key", "api_key"
        ) if payload.get(name)), None)
    if not isinstance(key, str) or not key.startswith("sk_test_") or len(key) < 16:
        raise RuntimeError("approved secret is not a Stripe test credential")
    return key


def main() -> None:
    dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
    if urlparse(dsn).hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("only a disposable loopback PostgreSQL database is permitted")
    secret_ref = os.environ["BUSINESS_BUILDER_STRIPE_TEST_SECRET_REF"]
    expected_account = os.environ["BUSINESS_BUILDER_STRIPE_TEST_ACCOUNT_ID"]
    key = _test_key(secret_ref)
    # No webhook is handled in this probe; a separate listener must supply a
    # real CLI-issued signing secret before a payment event can be accepted.
    provider = StripeTestPaymentProvider(
        api_key=key, webhook_secret="whsec_" + "unused_local_checkout_probe_" * 2,
    )
    account = provider._transport(
        "GET", "https://api.stripe.com/v1/account", None, provider._headers()
    )
    if account.get("id") != expected_account:
        raise RuntimeError("Stripe credential points to the wrong sandbox account")

    now = datetime.now(timezone.utc)
    ids = lambda kind: f"{kind}_sandbox_{uuid4().hex}"
    schema = f"bb_stripe_sandbox_{uuid4().hex[:12]}"
    identity_repo = PostgresIdentityRepository(dsn, schema=schema)
    commercial_repo = PostgresCommercialRepository(dsn, schema=schema)
    identity = IdentityService(identity_repo, id_factory=ids, clock=lambda: now)
    founder, _ = identity.register_founder("sandbox-founder@example.test", "Sandbox Founder")
    tenant, _, _ = identity.create_account(founder.user_id, "Sandbox Cleaning Company")
    company_id = ids("company")
    identity.attach_company(AuthorizationContext(founder.user_id, tenant.tenant_id), company_id)
    founder_context = AuthorizationContext(founder.user_id, tenant.tenant_id, company_id)
    operator = identity.register_user("sandbox-operator@example.test")
    identity_repo.save_user(replace(
        operator, authentication_provider="cognito", provider_subject_digest=ids("test_subject"),
        email_verified_at=now,
    ))
    invitation = identity.invite_member(founder_context, operator.user_id, Role.SUPPORT)
    identity.accept_membership(invitation.membership_id, operator.user_id)
    sessions = SessionService(
        identity_repo, provider=FakeDevAuthenticationProvider(), id_factory=ids,
        clock=lambda: now,
    )
    _, raw_operator_session = sessions.issue_for_user(
        operator.user_id, provider_name="cognito", provider_session_id=ids("test_provider_session")
    )
    seed_default_catalog(commercial_repo, effective_at=now)
    authority = CommercialOperatorAuthority(
        identity_repo, commercial_repo,
        signing_key=b"isolated_sandbox_operator_test_signing_key_v1",
        clock=lambda: now,
        privileged_provisioner_verifier=lambda value: "platform_test_operator" if value == "isolated_test_control" else "",
    )
    grant = authority.provision(
        "isolated_test_control", grant_id=ids("operator_grant"),
        operator_user_id=operator.user_id, tenant_id=tenant.tenant_id,
        company_id=company_id, actions=frozenset({"quote.publish", "payment.release"}),
        starts_at=now, ends_at=now + timedelta(minutes=30),
        reason_code="isolated_stripe_sandbox_acceptance",
    )
    principal = authority.issue(
        raw_operator_session, tenant_id=tenant.tenant_id, company_id=company_id,
        grant_id=grant.grant_id,
    )
    commercial = CommercialService(
        commercial_repo, identity.authorization, RecordingCommercialEventSink(),
        id_factory=ids, clock=lambda: now, operator_authority=authority,
        allow_test_admission=True,
    )

    def admit(order_id: str) -> None:
        commercial.release_payment(
            principal, tenant_id=tenant.tenant_id, company_id=company_id,
            order_id=order_id, eligibility=PaymentEligibility.PAY_NOW_ELIGIBLE,
            eligible_at=None, tax_disposition=TaxDisposition.TEST_MODE_UNDETERMINED,
            tax_review_state=TaxReviewState.TEST_MODE_UNDETERMINED,
            decision_ref=ids("operator_test_decision"), tax_review_ref=None,
            expires_at=now + timedelta(minutes=20),
        )

    fixed = commercial.create_order(
        founder_context, "product_version_build_business_v1",
        offer_code=OfferCode.BUSINESS,
    )
    admit(fixed.order_id)
    bundle = commercial.create_order(
        founder_context, "product_version_build_business_v1",
        offer_code=OfferCode.BUSINESS,
    )
    bundle = commercial.select_fixed_offer(founder_context, bundle.order_id, OfferCode.BUSINESS_RUN)
    admit(bundle.order_id)
    digest = "a" * 64  # synthetic cited scope digest for this isolated test company
    existing = commercial.create_existing_business_order(
        founder_context, audit_ref=ids("synthetic_existing_audit"),
        recommendation_digest=digest,
    )
    quote = commercial.create_existing_business_quote(
        tenant_id=tenant.tenant_id, company_id=company_id, order_id=existing.order_id,
        audit_ref=existing.existing_audit_ref, recommendation_digest=digest,
        upfront_minor=169500, expires_at=now + timedelta(days=2),
        operator_principal=principal,
    )
    commercial.approve_quote(founder_context, quote.quote_id, digest)
    admit(existing.order_id)

    output = []
    for label, order_id, expected_first_due in (
        ("build_business", fixed.order_id, 149500),
        ("build_business_run", bundle.order_id, 199500 + 29900),
        ("existing_business_run_quote", existing.order_id, 169500 + 29900),
    ):
        checkout = commercial.create_checkout(founder_context, order_id, ids("checkout_retry"))
        order = commercial_repo.get_order(tenant.tenant_id, company_id, order_id)
        if order.total is None or order.total.minor_units != expected_first_due:
            raise RuntimeError("commercial first charge differs from canonical pricing/quote")
        provider_ref, redirect = provider.open_checkout(
            order=order, idempotency_key=f"bb-sandbox-{uuid4().hex}",
            success_url="https://example.com/success", cancel_url="https://example.com/cancel",
            customer_email=founder.email,
        )
        commercial_repo.save_checkout(replace(checkout, provider_ref=provider_ref, redirect_url=redirect))
        output.append({
            "offer": label, "order_id": order_id, "session_ref": provider_ref,
            "first_due_minor": expected_first_due,
            "mode": "payment" if label == "build_business" else "subscription",
        })
    if commercial_repo.get_current_entitlement_grants(tenant.tenant_id, company_id):
        raise RuntimeError("unpaid Checkout unexpectedly activated entitlement")
    commercial_repo.close()
    identity_repo.close()
    restarted = PostgresCommercialRepository(dsn, schema=schema)
    for item in output:
        persisted = restarted.get_checkout_by_provider_ref(item["session_ref"])
        if persisted.order_id != item["order_id"]:
            raise RuntimeError("Stripe session did not survive PostgreSQL restart")
        if restarted.list_current_orders("tenant_other", company_id):
            raise RuntimeError("cross-tenant commercial lookup succeeded")
    restarted.close()
    print(json.dumps({
        "status": "three_unpaid_stripe_test_checkouts_created",
        "account_id": expected_account, "schema": schema,
        "postgres_restart": "passed", "tenant_isolation": "passed",
        "entitlements_active": 0, "sessions": output,
        "webhook_proof": "not_yet_run",
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # No provider body, key, webhook secret, or account details in output.
        print(json.dumps({"status": "failed", "failure_class": type(exc).__name__}))
        raise SystemExit(1) from None
