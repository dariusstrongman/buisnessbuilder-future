from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json

import pytest

from businessbuilder.commercial import CommercialService, InMemoryCommercialRepository, RecordingCommercialEventSink, seed_default_catalog
from businessbuilder.commercial.models import CheckoutStatus, EntitlementClass, EntitlementStatus, PaymentEligibility, TaxDisposition
from businessbuilder.commercial.pricing import FOUNDING_PRICES, OfferCode, PriceKind
from businessbuilder.commercial.repository import CommercialConflict
from businessbuilder.commercial.stripe_test import StripeTestPaymentProvider
from businessbuilder.commercial.stripe_webhooks import StripeWebhookIngress
from businessbuilder.identity import AuthorizationContext, IdentityService, InMemoryIdentityRepository
from businessbuilder.runtime.ids import DeterministicIds


NOW = datetime(2026, 9, 15, 5, 0, tzinfo=timezone.utc)


def setup():
    ids = DeterministicIds()
    identity_repo = InMemoryIdentityRepository()
    identity = IdentityService(identity_repo, id_factory=ids, clock=lambda: NOW)
    user, _ = identity.register_founder("checkout-founder@example.test", "Checkout Founder")
    tenant, _, _ = identity.create_account(user.user_id, "Checkout Organization")
    company_id = "company_checkout"
    identity.attach_company(AuthorizationContext(user.user_id, tenant.tenant_id), company_id)
    context = AuthorizationContext(user.user_id, tenant.tenant_id, company_id)
    repository = InMemoryCommercialRepository()
    seed_default_catalog(repository, effective_at=NOW)
    service = CommercialService(repository, identity.authorization, RecordingCommercialEventSink(),
                                id_factory=ids, clock=lambda: NOW, allow_test_admission=True)
    return context, repository, service


def provider(subscription: bool = False):
    calls = []

    def transport(method, url, body, headers):
        calls.append((method, url, body, headers))
        if method == "GET":
            return {"id": "sub_fixture_0001", "current_period_start": int(NOW.timestamp()),
                    "current_period_end": int((NOW + timedelta(days=30)).timestamp())}
        return {"id": "cs_test_fixture_0001", "url": "https://checkout.stripe.com/c/pay/fixture"}

    adapter = StripeTestPaymentProvider(
        api_key="sk_test_" + "x" * 16, webhook_secret="whsec_" + "y" * 16,
        transport=transport, clock=lambda: NOW,
    )
    return adapter, calls


def signed_event(event_type: str, value: dict, *, ref: str = "evt_fixture_0001"):
    raw = json.dumps({"id": ref, "type": event_type, "created": int(NOW.timestamp()),
                      "livemode": False, "data": {"object": value}}, separators=(",", ":")).encode()
    stamp = int(NOW.timestamp())
    digest = hmac.new(("whsec_" + "y" * 16).encode(), str(stamp).encode() + b"." + raw, sha256).hexdigest()
    return f"t={stamp},v1={digest}", raw


def ready_order(context, repo, service, offer=OfferCode.BUSINESS):
    order = service.create_order(context, "product_version_build_business_v1", offer_code=OfferCode.BUSINESS)
    if offer is OfferCode.BUSINESS_RUN:
        order = service.select_fixed_offer(context, order.order_id, offer)
    order = service.record_payment_readiness(
        tenant_id=context.tenant_id, company_id=context.company_id, order_id=order.order_id,
        eligibility=PaymentEligibility.PAY_NOW_ELIGIBLE, eligible_at=None,
        tax_disposition=TaxDisposition.NON_TAXABLE, review_ref="test_supervised_review_0001",
    )
    return order


def linked_checkout(context, repo, service, order):
    checkout = service.create_checkout(context, order.order_id, "checkout-retry-key-0001")
    checkout = replace(checkout, provider_ref="cs_test_fixture_0001",
                       redirect_url="https://checkout.stripe.com/c/pay/fixture")
    repo.save_checkout(checkout)
    return checkout


def session_object(order, *, subscription=None, recipient_order=None, paid=True,
                   customer_email="checkout-founder@example.test"):
    return {"id": "cs_test_fixture_0001", "client_reference_id": recipient_order or order.order_id,
            "payment_status": "paid" if paid else "unpaid", "currency": "usd",
            "amount_subtotal": order.total.minor_units, "amount_total": order.total.minor_units,
            "payment_intent": "pi_fixture_0001" if not subscription else None,
            "subscription": subscription, "customer_details": {"email": customer_email},
            "metadata": {"bb_order_id": order.order_id}}


def test_canonical_prices_and_quote_floor():
    assert FOUNDING_PRICES[OfferCode.WEBSITE].upfront_minor == 79500
    assert FOUNDING_PRICES[OfferCode.BUSINESS].upfront_minor == 149500
    assert FOUNDING_PRICES[OfferCode.BUSINESS_RUN].upfront_minor == 199500
    assert FOUNDING_PRICES[OfferCode.BUSINESS_RUN].monthly_minor == 29900
    assert FOUNDING_PRICES[OfferCode.EXISTING_RUN].kind is PriceKind.QUOTE_REQUIRED
    assert FOUNDING_PRICES[OfferCode.EXISTING_RUN].upfront_minor is None


def test_browser_completion_is_not_payment_authority_and_duplicate_webhook_is_safe():
    context, repo, service = setup()
    order = ready_order(context, repo, service)
    linked_checkout(context, repo, service, order)
    assert not repo.get_current_entitlement_grants(context.tenant_id, context.company_id)
    adapter, _ = provider()
    ingress = StripeWebhookIngress(repo, service, adapter)
    signature, raw = signed_event("checkout.session.completed", session_object(order, paid=False))
    assert ingress.handle(signature, raw) == 1
    assert not repo.get_current_entitlement_grants(context.tenant_id, context.company_id)
    signature, raw = signed_event("checkout.session.async_payment_succeeded", session_object(order), ref="evt_fixture_0002")
    assert ingress.handle(signature, raw) == 2
    history_count = len(repo.order_history(context.tenant_id, context.company_id, order.order_id))
    assert ingress.handle(signature, raw) == 0
    assert len(repo.order_history(context.tenant_id, context.company_id, order.order_id)) == history_count
    assert repo.get_current_entitlement_grants(context.tenant_id, context.company_id)


def test_forged_replay_wrong_owner_and_wrong_amount_fail_closed():
    context, repo, service = setup()
    order = ready_order(context, repo, service)
    linked_checkout(context, repo, service, order)
    adapter, _ = provider()
    ingress = StripeWebhookIngress(repo, service, adapter)
    signature, raw = signed_event("checkout.session.completed", session_object(order))
    with pytest.raises(ValueError):
        ingress.handle("t=0,v1=deadbeef", raw)
    with pytest.raises(ValueError):
        ingress.handle(signature, raw + b" ")
    forged = session_object(order, recipient_order="order_other_tenant")
    signature, raw = signed_event("checkout.session.completed", forged, ref="evt_fixture_0003")
    with pytest.raises(CommercialConflict):
        ingress.handle(signature, raw)
    wrong = session_object(order)
    wrong["amount_subtotal"] += 10000
    signature, raw = signed_event("checkout.session.completed", wrong, ref="evt_fixture_0004")
    with pytest.raises(CommercialConflict):
        ingress.handle(signature, raw)
    wrong_customer = session_object(order, customer_email="another-founder@example.test")
    signature, raw = signed_event("checkout.session.completed", wrong_customer, ref="evt_fixture_wrong_customer")
    with pytest.raises(CommercialConflict):
        ingress.handle(signature, raw)
    wrong_metadata = session_object(order)
    wrong_metadata["metadata"]["bb_order_id"] = "order_other_tenant"
    signature, raw = signed_event("checkout.session.completed", wrong_metadata, ref="evt_fixture_wrong_metadata")
    with pytest.raises(CommercialConflict):
        ingress.handle(signature, raw)
    assert not repo.get_current_entitlement_grants(context.tenant_id, context.company_id)


def test_bundle_requires_paid_upfront_and_retains_owned_state_after_cancel():
    context, repo, service = setup()
    order = ready_order(context, repo, service, OfferCode.BUSINESS_RUN)
    assert order.total.minor_units == 199500 + 29900
    linked_checkout(context, repo, service, order)
    adapter, _ = provider(subscription=True)
    ingress = StripeWebhookIngress(repo, service, adapter)
    signature, raw = signed_event("checkout.session.completed", session_object(order, subscription="sub_fixture_0001"))
    assert ingress.handle(signature, raw) == 3
    grants = repo.get_current_entitlement_grants(context.tenant_id, context.company_id)
    assert any(item.entitlement_class is EntitlementClass.STROMATION_MANAGED for item in grants)
    assert any(item.entitlement_class is EntitlementClass.CUSTOMER_OWNED for item in grants)
    signature, raw = signed_event("customer.subscription.deleted", {"id": "sub_fixture_0001"}, ref="evt_fixture_cancel")
    ingress.handle(signature, raw)
    grants = repo.get_current_entitlement_grants(context.tenant_id, context.company_id)
    assert all(item.status is EntitlementStatus.EXPIRED for item in grants if item.source_subscription_id and item.entitlement_class is EntitlementClass.STROMATION_MANAGED)
    assert all(item.status is EntitlementStatus.ACTIVE for item in grants if item.entitlement_class is EntitlementClass.CUSTOMER_OWNED)


def test_out_of_band_invoice_paid_cannot_reactivate_suspended_managed_work():
    context, repo, service = setup()
    order = ready_order(context, repo, service, OfferCode.BUSINESS_RUN)
    linked_checkout(context, repo, service, order)
    adapter, _ = provider(subscription=True)
    ingress = StripeWebhookIngress(repo, service, adapter)
    signature, raw = signed_event(
        "checkout.session.completed", session_object(order, subscription="sub_fixture_0001")
    )
    assert ingress.handle(signature, raw) == 3
    signature, raw = signed_event(
        "invoice.payment_failed", {"subscription": "sub_fixture_0001"},
        ref="evt_fixture_renewal_failed",
    )
    assert ingress.handle(signature, raw) == 1
    history = repo.entitlement_grants.copy()
    signature, raw = signed_event(
        "invoice.paid", {"subscription": "sub_fixture_0001", "currency": "usd",
                         "amount_paid": 29900, "paid_out_of_band": True},
        ref="evt_fixture_out_of_band",
    )
    assert ingress.handle(signature, raw) == 0
    assert repo.entitlement_grants == history
    signature, raw = signed_event(
        "invoice.payment_succeeded", {"subscription": "sub_fixture_0001",
                                      "billing_reason": "subscription_cycle",
                                      "currency": "usd", "amount_paid": 100},
        ref="evt_fixture_wrong_renewal_amount",
    )
    with pytest.raises(CommercialConflict):
        ingress.handle(signature, raw)
    assert repo.entitlement_grants == history


def test_quote_requires_audit_digest_founder_approval_and_supervised_eligibility():
    context, repo, service = setup()
    digest = "a" * 64
    order = service.create_existing_business_order(context, audit_ref="existing_audit_0001",
                                                    recommendation_digest=digest)
    assert order.offer_code == OfferCode.EXISTING_RUN.value and order.total is None
    quote = service.create_existing_business_quote(
        tenant_id=context.tenant_id, company_id=context.company_id, order_id=order.order_id,
        audit_ref="existing_audit_0001", recommendation_digest=digest,
        upfront_minor=169500, expires_at=NOW + timedelta(days=7),
    )
    with pytest.raises(CommercialConflict):
        service.approve_quote(context, quote.quote_id, "b" * 64)
    service.approve_quote(context, quote.quote_id, digest)
    with pytest.raises(CommercialConflict):
        service.create_checkout(context, order.order_id, "checkout-retry-key-0001")
    service.record_payment_readiness(
        tenant_id=context.tenant_id, company_id=context.company_id, order_id=order.order_id,
        eligibility=PaymentEligibility.PAYMENT_DELAY_REQUIRED,
        eligible_at=NOW + timedelta(days=2), tax_disposition=TaxDisposition.MANUAL_REVIEW,
        review_ref="test_delay_review_0001",
    )
    with pytest.raises(CommercialConflict):
        service.create_checkout(context, order.order_id, "checkout-retry-key-0001")


def test_approved_existing_business_quote_uses_distinct_first_charge_and_monthly_price():
    context, repo, service = setup()
    digest = "a" * 64
    order = service.create_existing_business_order(
        context, audit_ref="existing_audit_0002", recommendation_digest=digest,
    )
    quote = service.create_existing_business_quote(
        tenant_id=context.tenant_id, company_id=context.company_id, order_id=order.order_id,
        audit_ref="existing_audit_0002", recommendation_digest=digest,
        upfront_minor=169500, expires_at=NOW + timedelta(days=7),
    )
    service.approve_quote(context, quote.quote_id, digest)
    approved_order = repo.get_order(context.tenant_id, context.company_id, order.order_id)
    assert approved_order.total.minor_units == 169500 + 29900
    assert approved_order.items[0].unit_amount.minor_units == 169500
    assert approved_order.items[1].unit_amount.minor_units == 29900
    service.record_payment_readiness(
        tenant_id=context.tenant_id, company_id=context.company_id, order_id=order.order_id,
        eligibility=PaymentEligibility.PAY_NOW_ELIGIBLE, eligible_at=None,
        tax_disposition=TaxDisposition.NON_TAXABLE, review_ref="isolated_test_review_quote_0002",
    )
    checkout = service.create_checkout(context, order.order_id, "checkout-existing-retry-key-0002")
    assert checkout.order_id == order.order_id


def test_refund_webhook_pauses_managed_work_without_erasing_customer_owned_state():
    context, repo, service = setup()
    order = ready_order(context, repo, service)
    linked_checkout(context, repo, service, order)
    adapter, _ = provider()
    ingress = StripeWebhookIngress(repo, service, adapter)
    signature, raw = signed_event("checkout.session.completed", session_object(order))
    ingress.handle(signature, raw)
    signature, raw = signed_event("refund.created", {
        "id": "re_fixture_0001", "payment_intent": "pi_fixture_0001", "amount": 149500,
    }, ref="evt_fixture_refund")
    assert ingress.handle(signature, raw) == 1
    grants = repo.get_current_entitlement_grants(context.tenant_id, context.company_id)
    assert all(item.status is EntitlementStatus.SUSPENDED for item in grants if item.entitlement_class is EntitlementClass.STROMATION_MANAGED)
    assert all(item.status is EntitlementStatus.ACTIVE for item in grants if item.entitlement_class is EntitlementClass.CUSTOMER_OWNED)
    assert ingress.handle(signature, raw) == 0


def test_out_of_order_subscription_cancellation_retries_after_creation():
    context, repo, service = setup()
    order = ready_order(context, repo, service, OfferCode.BUSINESS_RUN)
    linked_checkout(context, repo, service, order)
    adapter, _ = provider(subscription=True)
    ingress = StripeWebhookIngress(repo, service, adapter)
    cancel_signature, cancel_raw = signed_event(
        "customer.subscription.deleted", {"id": "sub_fixture_0001"}, ref="evt_fixture_early_cancel",
    )
    with pytest.raises(LookupError):
        ingress.handle(cancel_signature, cancel_raw)
    pay_signature, pay_raw = signed_event("checkout.session.completed", session_object(order, subscription="sub_fixture_0001"))
    ingress.handle(pay_signature, pay_raw)
    assert ingress.handle(cancel_signature, cancel_raw) == 1
    assert ingress.handle(cancel_signature, cancel_raw) == 0
    grants = repo.get_current_entitlement_grants(context.tenant_id, context.company_id)
    assert all(item.status is EntitlementStatus.EXPIRED for item in grants if item.source_subscription_id and item.entitlement_class is EntitlementClass.STROMATION_MANAGED)


def test_stripe_test_adapter_uses_server_order_items_and_rejects_live_credentials():
    context, repo, service = setup()
    order = ready_order(context, repo, service, OfferCode.BUSINESS_RUN)
    adapter, calls = provider()
    provider_ref, url = adapter.open_checkout(
        order=order, idempotency_key="bb:order:retry", success_url="https://pilot.example.test/success",
        cancel_url="https://pilot.example.test/cancel", customer_email="checkout-founder@example.test",
    )
    assert provider_ref.startswith("cs_test_") and url.startswith("https://checkout.stripe.com/")
    body = calls[0][2].decode()
    assert "mode=subscription" in body and "199500" in body and "29900" in body
    with pytest.raises(ValueError):
        StripeTestPaymentProvider(api_key="sk_live_" + "x" * 16, webhook_secret="whsec_" + "y" * 16)
