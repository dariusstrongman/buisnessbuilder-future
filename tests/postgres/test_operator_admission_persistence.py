"""Disposable PostgreSQL restart proof for immutable commercial admission."""

from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import os
from uuid import uuid4

import pytest

from businessbuilder.commercial import CommercialService, RecordingCommercialEventSink
from businessbuilder.commercial.models import PaymentEligibility, TaxDisposition, TaxReviewState
from businessbuilder.commercial.operator_authority import CommercialOperatorAuthority
from businessbuilder.commercial.repository import CommercialConflict
from businessbuilder.postgres import PostgresCommercialRepository
from businessbuilder.identity import FakeDevAuthenticationProvider, SessionService
from tests.commercial.test_operator_admission import (
    NOW, fixed_order, release, setup,
)


@pytest.mark.skipif(not os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"), reason="isolated PostgreSQL required")
def test_long_lived_api_rechecks_external_operator_appointment_and_revocation():
    dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
    prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
    schema = f"{prefix}_live_grant_{uuid4().hex[:10]}"
    api_repo = PostgresCommercialRepository(dsn, schema=schema)
    writer_repo = PostgresCommercialRepository(dsn, schema=schema)
    context, _, _, authority, principal, _, _ = setup(writer_repo)
    assert api_repo.get_operator_grant(context.tenant_id, context.company_id, principal.grant_id).active_at(NOW)
    authority.revoke(
        "privileged_test_control", tenant_id=context.tenant_id,
        company_id=context.company_id, grant_id=principal.grant_id,
        reason_code="isolated_external_revocation",
    )
    assert not api_repo.get_operator_grant(context.tenant_id, context.company_id, principal.grant_id).active_at(NOW)
    writer_repo.close()
    api_repo.close()


@pytest.mark.skipif(not os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"), reason="isolated PostgreSQL required")
def test_operator_grant_admission_restart_and_scope():
    dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
    prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
    schema = f"{prefix}_admission_{uuid4().hex[:10]}"
    repo = PostgresCommercialRepository(dsn, schema=schema)
    context, identity_repo, _, authority, principal, service, _ = setup(repo)
    order = fixed_order(context, service)
    admitted = release(context, principal, service, order.order_id)
    grant_id = principal.grant_id
    repo.close()

    restarted = PostgresCommercialRepository(dsn, schema=schema)
    assert restarted.get_operator_grant(context.tenant_id, context.company_id, grant_id).active_at(NOW)
    assert restarted.get_admission(context.tenant_id, context.company_id, admitted.admission_id).order_digest
    assert restarted.get_order(context.tenant_id, context.company_id, order.order_id).admission_id == admitted.admission_id
    assert not restarted.list_current_orders("tenant_wrong", context.company_id)
    new_authority = type(authority)(
        identity_repo, restarted, signing_key=b"test_only_operator_signing_material_32bytes",
        clock=lambda: NOW,
    )
    new_service = CommercialService(
        restarted, service.authorization, RecordingCommercialEventSink(),
        id_factory=service.id_factory, clock=lambda: NOW,
        operator_authority=new_authority,
    )
    assert new_service.create_checkout(context, order.order_id, "postgres_retry_key").order_id == order.order_id
    with pytest.raises(CommercialConflict):
        new_service.record_payment_readiness(
            tenant_id=context.tenant_id, company_id=context.company_id,
            order_id=order.order_id, eligibility=admitted.eligibility,
            eligible_at=None, tax_disposition=admitted.tax_disposition,
            review_ref="forbidden_test_path",
        )
    restarted.close()


@pytest.mark.skipif(not os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"), reason="isolated PostgreSQL required")
def test_concurrent_operator_releases_cannot_create_two_current_admissions():
    dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
    prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
    schema = f"{prefix}_race_{uuid4().hex[:10]}"
    first_repo = PostgresCommercialRepository(dsn, schema=schema)
    context, identity_repo, _, _, principal, first_service, _ = setup(first_repo)
    order = fixed_order(context, first_service)
    second_repo = PostgresCommercialRepository(dsn, schema=schema)
    second_authority = CommercialOperatorAuthority(
        identity_repo, second_repo,
        signing_key=b"test_only_operator_signing_material_32bytes",
        clock=lambda: NOW,
    )
    second_service = CommercialService(
        second_repo, first_service.authorization, RecordingCommercialEventSink(),
        id_factory=first_service.id_factory, clock=lambda: NOW,
        operator_authority=second_authority,
        tax_authority_verifier=lambda state, disposition, reference: reference == "test_tax_boundary_only",
    )
    barrier = Barrier(2)

    def attempt(service):
        barrier.wait()
        try:
            release(context, principal, service, order.order_id)
            return "released"
        except CommercialConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (first_service, second_service)))
    assert sorted(outcomes) == ["conflict", "released"]
    first_repo.close()
    second_repo.close()
    persisted = PostgresCommercialRepository(dsn, schema=schema)
    current = persisted.get_order(context.tenant_id, context.company_id, order.order_id)
    assert current.admission_id is not None
    assert len([item for item in persisted.admissions.values() if item.order_id == order.order_id]) == 1
    persisted.close()


@pytest.mark.skipif(not os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"), reason="isolated PostgreSQL required")
def test_delayed_operator_release_survives_restart_without_erasing_prior_decision():
    dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
    prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
    schema = f"{prefix}_delayed_{uuid4().hex[:10]}"
    clock = [NOW]
    repo = PostgresCommercialRepository(dsn, schema=schema)
    context, identity_repo, _, authority, principal, service, _ = setup(repo, clock=clock)
    order = fixed_order(context, service)
    delayed = release(context, principal, service, order.order_id,
                      eligibility=PaymentEligibility.PAYMENT_DELAY_REQUIRED)
    repo.close()

    clock[0] = NOW + timedelta(minutes=31)
    reopened = PostgresCommercialRepository(dsn, schema=schema)
    new_authority = CommercialOperatorAuthority(
        identity_repo, reopened,
        signing_key=b"test_only_operator_signing_material_32bytes",
        clock=lambda: clock[0],
    )
    sessions = SessionService(
        identity_repo, provider=FakeDevAuthenticationProvider(),
        id_factory=lambda kind: kind + "_restarted_operator_session",
        clock=lambda: clock[0],
    )
    _, raw = sessions.issue_for_user(
        principal.operator_user_id, provider_name="cognito",
        provider_session_id="restart_test_provider_session",
    )
    renewed_principal = new_authority.issue(
        raw, tenant_id=context.tenant_id, company_id=context.company_id,
        grant_id=principal.grant_id,
    )
    new_service = CommercialService(
        reopened, service.authorization, RecordingCommercialEventSink(),
        id_factory=service.id_factory, clock=lambda: clock[0],
        operator_authority=new_authority,
        tax_authority_verifier=lambda state, disposition, reference: reference == "test_tax_boundary_only",
    )
    with pytest.raises(CommercialConflict):
        new_service.create_checkout(context, order.order_id, "delay_restart_checkout")
    release_until = NOW + timedelta(minutes=45)

    def upgrade():
        return new_service.release_payment(
            renewed_principal, tenant_id=context.tenant_id,
            company_id=context.company_id, order_id=order.order_id,
            eligibility=PaymentEligibility.PAY_NOW_ELIGIBLE, eligible_at=None,
            tax_disposition=TaxDisposition.PROVIDER_CALCULATED,
            tax_review_state=TaxReviewState.PROVIDER_CALCULATED,
            decision_ref="post_wait_operator_release", tax_review_ref="test_tax_boundary_only",
            expires_at=release_until,
        )

    released = upgrade()
    assert upgrade().admission_id == released.admission_id
    assert released.admission_id != delayed.admission_id
    reopened.close()

    persisted = PostgresCommercialRepository(dsn, schema=schema)
    prior = persisted.get_admission(context.tenant_id, context.company_id, delayed.admission_id)
    current = persisted.get_admission(context.tenant_id, context.company_id, released.admission_id)
    assert prior.eligibility is PaymentEligibility.PAYMENT_DELAY_REQUIRED
    assert current.supersedes_admission_id == prior.admission_id
    assert persisted.get_order(context.tenant_id, context.company_id, order.order_id).admission_id == current.admission_id
    assert not persisted.list_current_orders("tenant_other", context.company_id)
    persisted.close()


@pytest.mark.skipif(not os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"), reason="isolated PostgreSQL required")
def test_concurrent_delayed_release_upgrade_has_one_current_decision():
    dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
    prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
    schema = f"{prefix}_upgrade_race_{uuid4().hex[:10]}"
    clock = [NOW]
    first_repo = PostgresCommercialRepository(dsn, schema=schema)
    context, identity_repo, _, first_authority, principal, first_service, _ = setup(first_repo, clock=clock)
    order = fixed_order(context, first_service)
    delayed = release(context, principal, first_service, order.order_id,
                      eligibility=PaymentEligibility.PAYMENT_DELAY_REQUIRED)
    clock[0] = NOW + timedelta(minutes=31)
    sessions = SessionService(
        identity_repo, provider=FakeDevAuthenticationProvider(),
        id_factory=lambda kind: kind + "_concurrent_upgrade_session",
        clock=lambda: clock[0],
    )
    _, raw = sessions.issue_for_user(
        principal.operator_user_id, provider_name="cognito",
        provider_session_id="concurrent_upgrade_provider_session",
    )
    renewed = first_authority.issue(
        raw, tenant_id=context.tenant_id, company_id=context.company_id,
        grant_id=principal.grant_id,
    )
    second_repo = PostgresCommercialRepository(dsn, schema=schema)
    second_authority = CommercialOperatorAuthority(
        identity_repo, second_repo,
        signing_key=b"test_only_operator_signing_material_32bytes",
        clock=lambda: clock[0],
    )
    second_service = CommercialService(
        second_repo, first_service.authorization, RecordingCommercialEventSink(),
        id_factory=first_service.id_factory, clock=lambda: clock[0],
        operator_authority=second_authority,
        tax_authority_verifier=lambda state, disposition, reference: reference == "test_tax_boundary_only",
    )
    barrier = Barrier(2)

    def attempt(service, decision_ref):
        barrier.wait()
        try:
            service.release_payment(
                renewed, tenant_id=context.tenant_id, company_id=context.company_id,
                order_id=order.order_id, eligibility=PaymentEligibility.PAY_NOW_ELIGIBLE,
                eligible_at=None, tax_disposition=TaxDisposition.PROVIDER_CALCULATED,
                tax_review_state=TaxReviewState.PROVIDER_CALCULATED,
                decision_ref=decision_ref, tax_review_ref="test_tax_boundary_only",
                expires_at=NOW + timedelta(minutes=45),
            )
            return "released"
        except CommercialConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda pair: attempt(*pair),
                                 ((first_service, "upgrade_release_one"),
                                  (second_service, "upgrade_release_two"))))
    assert sorted(outcomes) == ["conflict", "released"]
    first_repo.close()
    second_repo.close()
    persisted = PostgresCommercialRepository(dsn, schema=schema)
    current = persisted.get_order(context.tenant_id, context.company_id, order.order_id)
    admission = persisted.get_admission(context.tenant_id, context.company_id, current.admission_id)
    assert admission.supersedes_admission_id == delayed.admission_id
    assert len([item for item in persisted.admissions.values() if item.order_id == order.order_id]) == 2
    persisted.close()
