from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from businessbuilder.commercial import (
    CommercialService, InMemoryCommercialRepository, RecordingCommercialEventSink,
    seed_default_catalog,
)
from businessbuilder.commercial.models import AccessSource, EntitlementClass, OrderStatus, ProductCode
from businessbuilder.commercial.pilot_access import AwsPilotCodeReader, PilotSecretUnavailable
from businessbuilder.commercial.repository import CommercialConflict
from businessbuilder.identity import AuthorizationContext, IdentityService, InMemoryIdentityRepository
from businessbuilder.identity.exceptions import IdentityNotFound
from businessbuilder.runtime.ids import DeterministicIds


NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


class SyntheticSecret:
    def __init__(self):
        self.value = uuid4().hex
        self.reads = 0

    def read_code(self):
        self.reads += 1
        return self.value


def setup():
    ids = DeterministicIds()
    identities = InMemoryIdentityRepository()
    identity = IdentityService(identities, id_factory=ids, clock=lambda: NOW)
    user, _ = identity.register_founder("synthetic-pilot-founder@example.test", "Pilot Founder")
    tenant, _, _ = identity.create_account(user.user_id, "Pilot Organization")
    identity.attach_company(AuthorizationContext(user.user_id, tenant.tenant_id), "company_pilot")
    context = AuthorizationContext(user.user_id, tenant.tenant_id, "company_pilot")
    repository = InMemoryCommercialRepository()
    sink = RecordingCommercialEventSink()
    seed_default_catalog(repository, effective_at=NOW)
    service = CommercialService(repository, identity.authorization, sink, id_factory=ids, clock=lambda: NOW)
    order = service.create_order(context, "product_version_build_business_v1")
    return context, repository, service, order, SyntheticSecret(), sink


def test_valid_pilot_has_zero_payment_and_full_managed_entitlements():
    context, repo, service, order, secret, sink = setup()
    assert service.redeem_pilot_access(context, order.order_id, secret.value, secret)
    admitted = repo.get_order(context.tenant_id, context.company_id, order.order_id)
    assert admitted.status is OrderStatus.FULFILLMENT_PENDING
    assert admitted.access_source is AccessSource.PILOT_ACCESS
    assert admitted.total.minor_units == 0
    assert {item.product_code for item in admitted.items} == {ProductCode.BUILD_BUSINESS, ProductCode.BUILD_AND_RUN}
    assert all(item.unit_amount.minor_units == 0 for item in admitted.items)
    grants = repo.get_current_entitlement_grants(context.tenant_id, context.company_id)
    assert grants and all(item.provenance is AccessSource.PILOT_ACCESS for item in grants)
    assert any(item.entitlement_class is EntitlementClass.STROMATION_MANAGED for item in grants)
    assert repo.list_current_subscriptions(context.tenant_id, context.company_id) == ()
    assert repo.get_payment_for_order(context.tenant_id, context.company_id, order.order_id) is None
    assert repo.get_checkout_by_idempotency(context.tenant_id, context.company_id, "unused") is None
    assert repo.get_pilot_redemption(context.tenant_id, context.company_id).founder_user_id == context.actor_user_id
    with pytest.raises(CommercialConflict):
        service.create_checkout(context, order.order_id, "pilot-retry-key-0001")
    history = repo.order_history(context.tenant_id, context.company_id, order.order_id)
    assert all(item.status is not OrderStatus.PAID for item in history)
    assert "payment" not in repr(sink.events).lower()
    assert secret.value not in repr(repo.list_audit(context.tenant_id, context.company_id))
    assert secret.value not in repr(repo.list_outbox())


def test_invalid_missing_rate_limit_and_unavailable_fail_closed():
    context, repo, service, order, secret, _ = setup()
    for index in range(5):
        assert not service.redeem_pilot_access(context, order.order_id, "" if index == 0 else uuid4().hex, secret)
    reads = secret.reads
    assert not service.redeem_pilot_access(context, order.order_id, uuid4().hex, secret)
    assert secret.reads == reads
    assert repo.get_pilot_redemption(context.tenant_id, context.company_id) is None
    assert repo.get_order(context.tenant_id, context.company_id, order.order_id).status is OrderStatus.DRAFT
    context2, repo2, service2, order2, _, _ = setup()
    class Unavailable:
        def read_code(self):
            raise PilotSecretUnavailable("synthetic unavailable")
    assert not service2.redeem_pilot_access(context2, order2.order_id, uuid4().hex, Unavailable())
    assert repo2.get_pilot_redemption(context2.tenant_id, context2.company_id) is None


def test_duplicate_redemption_code_rotation_and_tenant_scope():
    context, repo, service, order, secret, _ = setup()
    assert service.redeem_pilot_access(context, order.order_id, secret.value, secret)
    before = repo.get_current_entitlement_grants(context.tenant_id, context.company_id)
    class Removed:
        def read_code(self):
            raise AssertionError("already authorized pilot must not reread AWS")
    assert service.redeem_pilot_access(context, order.order_id, "", Removed())
    assert repo.get_current_entitlement_grants(context.tenant_id, context.company_id) == before
    assert repo.get_pilot_redemption("tenant_wrong", context.company_id) is None
    assert repo.get_current_entitlement_grants("tenant_wrong", context.company_id) == ()
    with pytest.raises((PermissionError, CommercialConflict, IdentityNotFound)):
        service.redeem_pilot_access(AuthorizationContext(context.actor_user_id, "tenant_wrong", context.company_id),
                                    order.order_id, secret.value, secret)


def test_aws_adapter_reads_only_named_field_without_exposing_payload():
    synthetic = SyntheticSecret()
    class Client:
        def get_secret_value(self, *, SecretId):
            assert SecretId == "business-builder/pilot-access"
            return {"SecretString": '{"promo_code":"' + synthetic.value + '","unrelated":"unused"}'}
    assert AwsPilotCodeReader(Client()).read_code() == synthetic.value
    class Malformed:
        def get_secret_value(self, *, SecretId):
            return {"SecretString": '{"wrong":"' + synthetic.value + '"}'}
    with pytest.raises(PilotSecretUnavailable) as caught:
        AwsPilotCodeReader(Malformed()).read_code()
    assert synthetic.value not in str(caught.value)


def test_failed_entitlement_write_rolls_back_order_redemption_audit_and_events():
    context, repo, service, order, secret, _ = setup()
    before_audit = repo.list_audit(context.tenant_id, context.company_id)
    before_outbox = repo.list_outbox()
    original = repo.append_entitlement_grant
    def fail_once(grant):
        raise RuntimeError("synthetic entitlement write failure")
    repo.append_entitlement_grant = fail_once
    with pytest.raises(RuntimeError):
        service.redeem_pilot_access(context, order.order_id, secret.value, secret)
    repo.append_entitlement_grant = original
    assert repo.get_order(context.tenant_id, context.company_id, order.order_id).status is OrderStatus.DRAFT
    assert repo.get_pilot_redemption(context.tenant_id, context.company_id) is None
    assert repo.get_current_entitlement_grants(context.tenant_id, context.company_id) == ()
    assert repo.list_audit(context.tenant_id, context.company_id) == before_audit
    assert repo.list_outbox() == before_outbox
