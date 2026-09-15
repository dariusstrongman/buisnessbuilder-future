from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from uuid import uuid4

import pytest

from businessbuilder.commercial.models import AccessSource, EntitlementClass
from businessbuilder.customer_api.bootstrap import create_postgres_customer_api
from businessbuilder.identity import FakeDevAuthenticationProvider, IdentityService, SessionService
from businessbuilder.postgres import PostgresIdentityRepository
from businessbuilder.runtime.ids import DeterministicIds
from tests.commercial.test_pilot_access import SyntheticSecret


NOW = datetime(2026, 9, 15, 13, tzinfo=timezone.utc)
SIGNING_KEY = b"isolated-private-pilot-principal-signing-key-v1"


def call(api, method, path, token, *, company_id=None, body=None):
    return api.handle(
        method=method, path=path,
        headers={"Authorization": f"Bearer {token}"} if token else {},
        query={"company_id": [company_id]} if company_id else {},
        body=body if body is not None else {},
        request_id=f"pilot_request_{uuid4().hex[:8]}",
        correlation_id="pilot_correlation_test",
    )


@pytest.mark.skipif(not os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"), reason="isolated PostgreSQL required")
def test_founder_approved_pilot_redemption_concurrency_restart_and_tenant_isolation():
    dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
    prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
    schema = f"{prefix}_private_pilot_{uuid4().hex[:10]}"
    ids = DeterministicIds()
    identity_repo = PostgresIdentityRepository(dsn, schema=schema)
    identity = IdentityService(identity_repo, id_factory=ids, clock=lambda: NOW)
    founder = identity.register_user("private-pilot-founder@example.test")
    provider = FakeDevAuthenticationProvider()
    provider.register(founder.user_id, founder.email, "synthetic-session-proof")
    sessions = SessionService(identity_repo, provider, id_factory=ids, clock=lambda: NOW)
    _, token = sessions.sign_in(founder.email, "synthetic-session-proof")
    identity_repo.close()
    secret = SyntheticSecret()
    api1 = create_postgres_customer_api(signing_key=SIGNING_KEY, dsn=dsn, schema=schema,
                                        clock=lambda: NOW, pilot_code_reader=secret)
    start = call(api1, "POST", "/api/v1/pilots/residential-cleaning/intakes", token, body={
        "idempotency_key": "private-pilot-cleaning-0001",
        "intake": {
            "starting_point": "idea",
            "idea": "Build a reliable residential cleaning service for busy Denton households.",
            "founder_display_name": "Private Founder",
            "organization_name": "Private Cleaning Organization",
            "company_name": "Private Cleaning",
            "country": "US", "region": "TX", "locality": "Denton",
            "service_radius_miles": 12, "weekly_hours": 35,
            "startup_budget_minor": 300000,
            "working_preferences": {"owner_operated_at_launch": True},
        },
    })
    assert start.status == 201
    journey = start.body["journey"]
    company_id = journey["company"]["company_id"]
    tenant_id = api1.identity_repository.list_user_memberships(founder.user_id)[0].tenant_id
    approval = call(api1, "POST", f"/api/v1/companies/{company_id}/residential-cleaning-pilot/approve",
                    token, body={"approval_id": journey["scope_commit"]["approval_id"]})
    assert approval.status == 200
    order_id = approval.body["journey"]["order"]["order_id"]
    path = f"/api/v1/orders/{order_id}/pilot-access"
    assert call(api1, "POST", path, None, company_id=company_id,
                body={"code": secret.value}).status == 401
    assert call(api1, "POST", path, token, company_id="company_other",
                body={"code": secret.value}).status in {403, 404}
    assert call(api1, "POST", path, token, company_id=company_id,
                body={"code": uuid4().hex}).body == {"result": "INVALID"}
    assert call(api1, "POST", path, token, company_id=company_id,
                body={}).body == {"result": "INVALID"}
    api2 = create_postgres_customer_api(signing_key=SIGNING_KEY, dsn=dsn, schema=schema,
                                        clock=lambda: NOW, pilot_code_reader=secret)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda api: call(api, "POST", path, token, company_id=company_id,
                             body={"code": secret.value}), (api1, api2)))
    assert [response.body for response in results] == [{"result": "VALID"}, {"result": "VALID"}]
    assert secret.reads == 3  # two invalid requests plus one successful redemption
    order = api1.commercial_repository.get_order(tenant_id, company_id, order_id)
    assert order.access_source is AccessSource.PILOT_ACCESS and order.total.minor_units == 0
    assert api1.commercial_repository.get_pilot_redemption(tenant_id, company_id)
    grants = api1.commercial_repository.get_current_entitlement_grants(tenant_id, company_id)
    assert grants and any(item.entitlement_class is EntitlementClass.STROMATION_MANAGED for item in grants)
    assert all(item.provenance is AccessSource.PILOT_ACCESS for item in grants)
    assert api1.commercial_repository.get_current_entitlement_grants("tenant_wrong", company_id) == ()
    assert api1.commercial_repository.get_payment_for_order(tenant_id, company_id, order_id) is None
    assert api1.commercial_repository.list_current_subscriptions(tenant_id, company_id) == ()
    assert secret.value not in repr(api1.commercial_repository.list_audit(tenant_id, company_id))
    assert secret.value not in repr(api1.commercial_repository.list_outbox())
    projected = call(api1, "GET", f"/api/v1/companies/{company_id}/build-room", token)
    assert projected.status == 200
    assert projected.body["build_room"]["commercial"]["orders"][0]["access_source"] == "PILOT_ACCESS"
    assert not projected.body["build_room"]["readiness"]["ready"]
    assert not projected.body["build_room"]["readiness"]["fully_set"]
    api1.close()
    api2.close()
    restarted = create_postgres_customer_api(signing_key=SIGNING_KEY, dsn=dsn, schema=schema,
                                             clock=lambda: NOW, pilot_code_reader=secret)
    again = call(restarted, "POST", path, token, company_id=company_id, body={"code": ""})
    assert again.body == {"result": "VALID"}
    assert restarted.commercial_repository.get_current_entitlement_grants(tenant_id, company_id) == grants
    assert secret.value not in repr(again.body)
    restarted.close()
