"""Disposable PostgreSQL restart proof for immutable commercial admission."""

from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import os
from uuid import uuid4

import pytest

from businessbuilder.commercial import CommercialService, RecordingCommercialEventSink
from businessbuilder.commercial.operator_authority import CommercialOperatorAuthority
from businessbuilder.commercial.repository import CommercialConflict
from businessbuilder.postgres import PostgresCommercialRepository
from tests.commercial.test_operator_admission import (
    NOW, fixed_order, release, setup,
)


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
