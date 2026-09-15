"""Adversarial admission tests; all provider identities and amounts are test-only."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from businessbuilder.commercial import (
    CommercialService, InMemoryCommercialRepository, RecordingCommercialEventSink,
    seed_default_catalog,
)
from businessbuilder.commercial.models import PaymentEligibility, TaxDisposition, TaxReviewState
from businessbuilder.commercial.operator_authority import CommercialOperatorAuthority
from businessbuilder.commercial.pricing import OfferCode
from businessbuilder.commercial.repository import CommercialConflict
from businessbuilder.identity import (
    AuthorizationContext, AuthorizationDenied, FakeDevAuthenticationProvider,
    IdentityService, InMemoryIdentityRepository, Role, SessionService,
)
from businessbuilder.runtime.ids import DeterministicIds


NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def setup(repository=None):
    ids = DeterministicIds()
    identity_repo = InMemoryIdentityRepository()
    identity = IdentityService(identity_repo, id_factory=ids, clock=lambda: NOW)
    founder, _ = identity.register_founder("admission-founder@example.test", "Admission Founder")
    tenant, _, _ = identity.create_account(founder.user_id, "Admission Organization")
    identity.attach_company(AuthorizationContext(founder.user_id, tenant.tenant_id), "company_admission")
    context = AuthorizationContext(founder.user_id, tenant.tenant_id, "company_admission")
    operator = identity.register_user("admission-operator@example.test")
    identity_repo.save_user(replace(
        operator, authentication_provider="cognito", provider_subject_digest="test_subject_digest",
        email_verified_at=NOW,
    ))
    invitation = identity.invite_member(context, operator.user_id, Role.SUPPORT)
    identity.accept_membership(invitation.membership_id, operator.user_id)
    sessions = SessionService(
        identity_repo, provider=FakeDevAuthenticationProvider(), id_factory=ids,
        clock=lambda: NOW,
    )
    session, raw_session = sessions.issue_for_user(
        operator.user_id, provider_name="cognito", provider_session_id="test_provider_session"
    )
    repository = repository or InMemoryCommercialRepository()
    seed_default_catalog(repository, effective_at=NOW)
    appointment = CommercialOperatorAuthority(
        identity_repo, repository, signing_key=b"test_only_operator_signing_material_32bytes",
        clock=lambda: NOW,
        privileged_provisioner_verifier=lambda value: "platform_operator" if value == "privileged_test_control" else "",
    )
    grant = appointment.provision(
        "privileged_test_control", grant_id="grant_admission_test", operator_user_id=operator.user_id,
        tenant_id=tenant.tenant_id, company_id=context.company_id,
        actions=frozenset({"payment.release", "quote.publish"}),
        starts_at=NOW, ends_at=NOW + timedelta(hours=1), reason_code="supervised_test_scope",
    )
    principal = appointment.issue(
        raw_session, tenant_id=tenant.tenant_id, company_id=context.company_id,
        grant_id=grant.grant_id,
    )
    commercial = CommercialService(
        repository, identity.authorization, RecordingCommercialEventSink(),
        id_factory=ids, clock=lambda: NOW, operator_authority=appointment,
        tax_authority_verifier=lambda state, disposition, reference: reference == "test_tax_boundary_only",
    )
    return context, identity_repo, repository, appointment, principal, commercial, session


def fixed_order(context, commercial):
    return commercial.create_order(
        context, "product_version_build_business_v1", offer_code=OfferCode.BUSINESS,
    )


def release(context, principal, commercial, order_id, *, eligibility=PaymentEligibility.PAY_NOW_ELIGIBLE):
    return commercial.release_payment(
        principal, tenant_id=context.tenant_id, company_id=context.company_id,
        order_id=order_id, eligibility=eligibility,
        eligible_at=NOW + timedelta(minutes=30) if eligibility is PaymentEligibility.PAYMENT_DELAY_REQUIRED else None,
        tax_disposition=TaxDisposition.PROVIDER_CALCULATED,
        tax_review_state=TaxReviewState.PROVIDER_CALCULATED,
        decision_ref="supervised_operator_test_release",
        tax_review_ref="test_tax_boundary_only",
        expires_at=NOW + timedelta(minutes=45),
    )


def test_checkout_requires_current_nonfounder_digest_bound_admission():
    context, _, repo, _, principal, commercial, _ = setup()
    order = fixed_order(context, commercial)
    with pytest.raises(CommercialConflict):
        commercial.create_checkout(context, order.order_id, "admission_retry_key")
    admitted = release(context, principal, commercial, order.order_id)
    assert admitted.admission_id
    assert release(context, principal, commercial, order.order_id).admission_id == admitted.admission_id
    assert repo.get_admission(context.tenant_id, context.company_id, admitted.admission_id).operator_user_id != context.actor_user_id
    assert commercial.create_checkout(context, order.order_id, "admission_retry_key").order_id == order.order_id


def test_payment_delay_and_tax_review_fail_closed():
    context, _, _, _, principal, commercial, _ = setup()
    order = fixed_order(context, commercial)
    release(context, principal, commercial, order.order_id,
            eligibility=PaymentEligibility.PAYMENT_DELAY_REQUIRED)
    with pytest.raises(CommercialConflict):
        commercial.create_checkout(context, order.order_id, "admission_retry_key")
    context, _, _, _, principal, commercial, _ = setup()
    order = fixed_order(context, commercial)
    with pytest.raises(CommercialConflict):
        commercial.release_payment(
            principal, tenant_id=context.tenant_id, company_id=context.company_id,
            order_id=order.order_id, eligibility=PaymentEligibility.PAY_NOW_ELIGIBLE,
            eligible_at=None, tax_disposition=TaxDisposition.MANUAL_REVIEW,
            tax_review_state=TaxReviewState.TAX_REVIEW_REQUIRED,
            decision_ref="review_pending", tax_review_ref=None,
            expires_at=NOW + timedelta(minutes=30),
        )


def test_forged_founder_role_scope_and_revoked_session_denied():
    context, identity_repo, repo, appointment, principal, commercial, session = setup()
    order = fixed_order(context, commercial)
    forged = replace(principal, operator_user_id=context.actor_user_id)
    for claimed in (forged, replace(principal, tenant_id="tenant_other"), replace(principal, company_id="company_other")):
        with pytest.raises(AuthorizationDenied):
            commercial.release_payment(
                claimed, tenant_id=context.tenant_id, company_id=context.company_id,
                order_id=order.order_id, eligibility=PaymentEligibility.PAY_NOW_ELIGIBLE,
                eligible_at=None, tax_disposition=TaxDisposition.PROVIDER_CALCULATED,
                tax_review_state=TaxReviewState.PROVIDER_CALCULATED,
                decision_ref="forged_claim", tax_review_ref="test_only",
                expires_at=NOW + timedelta(minutes=30),
            )
    identity_repo.save_session(replace(session, revoked_at=NOW))
    with pytest.raises(AuthorizationDenied):
        appointment.verify(principal, tenant_id=context.tenant_id,
                           company_id=context.company_id, action="payment.release")
    assert any(event.action == "commercial.operator.denied" for event in repo.audit)


def test_test_admission_helper_cannot_operate_in_production_configuration():
    context, _, _, _, _, commercial, _ = setup()
    order = fixed_order(context, commercial)
    with pytest.raises(CommercialConflict):
        commercial.record_payment_readiness(
            tenant_id=context.tenant_id, company_id=context.company_id,
            order_id=order.order_id, eligibility=PaymentEligibility.PAY_NOW_ELIGIBLE,
            eligible_at=None, tax_disposition=TaxDisposition.NON_TAXABLE,
            review_ref="fake_browser_or_test_input",
        )


def test_quote_is_founder_approved_and_release_binds_to_exact_version():
    context, _, repo, _, principal, commercial, _ = setup()
    order = commercial.create_existing_business_order(
        context, audit_ref="operator_reviewed_test_audit", recommendation_digest="a" * 64,
    )
    quote = commercial.create_existing_business_quote(
        tenant_id=context.tenant_id, company_id=context.company_id,
        order_id=order.order_id, audit_ref="operator_reviewed_test_audit",
        recommendation_digest="a" * 64, upfront_minor=169500,
        expires_at=NOW + timedelta(days=7), operator_principal=principal,
    )
    assert commercial.create_existing_business_quote(
        tenant_id=context.tenant_id, company_id=context.company_id,
        order_id=order.order_id, audit_ref="operator_reviewed_test_audit",
        recommendation_digest="a" * 64, upfront_minor=169500,
        expires_at=NOW + timedelta(days=7), operator_principal=principal,
    ).quote_id == quote.quote_id
    with pytest.raises(CommercialConflict):
        commercial.create_existing_business_quote(
            tenant_id=context.tenant_id, company_id=context.company_id,
            order_id=order.order_id, audit_ref="operator_reviewed_test_audit",
            recommendation_digest="a" * 64, upfront_minor=179500,
            expires_at=NOW + timedelta(days=7), operator_principal=principal,
        )
    commercial.approve_quote(context, quote.quote_id, "a" * 64)
    assert commercial.approve_quote(context, quote.quote_id, "a" * 64).quote_id == quote.quote_id
    admitted = release(context, principal, commercial, order.order_id)
    repo.append_quote(replace(repo.get_quote(context.tenant_id, context.company_id, quote.quote_id),
                              expires_at=NOW + timedelta(days=8), version=3))
    with pytest.raises(CommercialConflict):
        commercial.create_checkout(context, admitted.order_id, "quote_retry_key")
