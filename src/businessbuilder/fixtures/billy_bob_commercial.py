from __future__ import annotations

from datetime import datetime, timedelta, timezone

from businessbuilder.commercial import (
    BillingPeriod,
    CommercialService,
    EntitlementClass,
    InMemoryCommercialRepository,
    NormalizedBillingEvent,
    ProductCode,
    SubscriptionStatus,
    seed_default_catalog,
)
from businessbuilder.company_brain import CompanyBrainService, SQLiteCompanyBrainRepository, load_billy_bob
from businessbuilder.identity import AuthorizationContext, IdentityService, InMemoryIdentityRepository
from businessbuilder.integration.commercial import RuntimeCommercialEventSink, RuntimeEntitlementGuard
from businessbuilder.runtime.audit import AuditLog
from businessbuilder.runtime.events import LocalEventBus
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.runtime.storage import SQLiteRuntimeRepository


FIXED_NOW = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)
TENANT_ID = "tenant_billy"
COMPANY_ID = "co_billy_bob_lawn"


class FixtureClock:
    def __init__(self) -> None:
        self.now = FIXED_NOW

    def __call__(self) -> datetime:
        return self.now


def run_billy_bob_commercial() -> dict:
    """Canonical account-to-cancellation proof; entirely offline and provider neutral."""
    clock = FixtureClock()
    ids = DeterministicIds()

    brain_repository = SQLiteCompanyBrainRepository()
    brain_repository.migrate()
    brain = CompanyBrainService(brain_repository)
    scope = load_billy_bob(brain, TENANT_ID)
    readiness_before = brain.get_company(scope).readiness

    identity_repository = InMemoryIdentityRepository()
    identity = IdentityService(identity_repository, id_factory=ids, clock=clock)
    billy, founder_profile = identity.register_founder("billy.bob@example.test", "Billy Bob")
    tenant, organization, membership = identity.create_account(
        billy.user_id, "Billy Bob Ventures", tenant_id=TENANT_ID,
        organization_id="org_billy", membership_id="membership_billy_owner",
    )
    owner_context = AuthorizationContext(billy.user_id, TENANT_ID)
    identity.attach_company(owner_context, COMPANY_ID)
    company_context = AuthorizationContext(billy.user_id, TENANT_ID, COMPANY_ID)

    runtime_repository = SQLiteRuntimeRepository()
    runtime_events = LocalEventBus(runtime_repository, AuditLog(runtime_repository, ids, clock))
    fulfillment_events = []
    runtime_events.subscribe("commercial.fulfillment.eligible", fulfillment_events.append)

    commercial_repository = InMemoryCommercialRepository()
    seed_default_catalog(commercial_repository, effective_at=clock())
    commercial = CommercialService(
        commercial_repository, identity.authorization, RuntimeCommercialEventSink(runtime_events),
        id_factory=ids, clock=clock,
    )

    def purchase(code: ProductCode, ordinal: int):
        version_id = f"product_version_{code.value.lower()}_v1"
        order = commercial.create_order(company_context, version_id)
        checkout = commercial.create_checkout(company_context, order.order_id, f"billy-checkout-{ordinal}")
        commercial.handle_billing_event(
            NormalizedBillingEvent(
                f"billing_event_payment_{ordinal}", "billing.payment.succeeded", TENANT_ID,
                billy.user_id, COMPANY_ID, clock(), "offline_fixture",
                f"provider_payment_event_{ordinal}", f"correlation_purchase_{ordinal}",
                order_id=order.order_id, checkout_intent_id=checkout.checkout_intent_id,
                payment_provider_ref=f"opaque_payment_{ordinal}",
            )
        )
        return commercial_repository.get_order(TENANT_ID, COMPANY_ID, order.order_id)

    build_order = purchase(ProductCode.BUILD_BUSINESS, 1)
    readiness_after_purchase = brain.get_company(scope).readiness

    run_order = purchase(ProductCode.BUILD_AND_RUN, 2)
    period = BillingPeriod(clock(), clock() + timedelta(days=30))
    commercial.handle_billing_event(
        NormalizedBillingEvent(
            "billing_event_subscription_1", "billing.subscription.created", TENANT_ID,
            billy.user_id, COMPANY_ID, clock(), "offline_fixture", "provider_subscription_event_1",
            "correlation_subscription_1", order_id=run_order.order_id,
            subscription_provider_ref="opaque_subscription_billy",
            subscription_status=SubscriptionStatus.ACTIVE, current_period=period,
        )
    )
    subscription = commercial_repository.get_subscription_by_provider_ref(TENANT_ID, COMPANY_ID, "opaque_subscription_billy")
    guard = RuntimeEntitlementGuard(commercial)
    guard.require_capability(TENANT_ID, COMPANY_ID, "ai_workforce.execute")
    commercial.request_subscription_cancellation(company_context, subscription.subscription_id, "Billy chose to stop managed operations")
    clock.now = period.ends_at
    commercial.advance_time(TENANT_ID, COMPANY_ID)

    grants = commercial_repository.get_current_entitlement_grants(TENANT_ID, COMPANY_ID)
    customer_owned = tuple(item for item in grants if item.entitlement_class is EntitlementClass.CUSTOMER_OWNED)
    managed = tuple(item for item in grants if item.entitlement_class is EntitlementClass.STROMATION_MANAGED and item.source_subscription_id)
    return {
        "user": billy,
        "founder_profile": founder_profile,
        "tenant_id": TENANT_ID,
        "organization": identity_repository.get_organization_by_tenant(TENANT_ID),
        "membership": identity_repository.get_active_membership(TENANT_ID, billy.user_id),
        "scope": scope,
        "build_order": build_order,
        "run_order": run_order,
        "subscription": commercial_repository.get_subscription(TENANT_ID, COMPANY_ID, subscription.subscription_id),
        "customer_owned_grants": customer_owned,
        "managed_run_grants": managed,
        "readiness_before": readiness_before,
        "readiness_after_purchase": readiness_after_purchase,
        "fulfillment_events": tuple(fulfillment_events),
        "runtime_events": tuple(runtime_repository.list_events(TENANT_ID, COMPANY_ID)),
        "identity_audit": identity_repository.list_audit(TENANT_ID),
        "commercial_audit": commercial_repository.list_audit(TENANT_ID, COMPANY_ID),
        "brain": brain,
        "brain_repository": brain_repository,
        "runtime_repository": runtime_repository,
        "commercial": commercial,
        "commercial_repository": commercial_repository,
    }
