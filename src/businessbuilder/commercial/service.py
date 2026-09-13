from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from functools import wraps
from typing import Callable

from businessbuilder.identity import AuthorizationContext, AuthorizationPolicy, Permission

from .models import (
    Amount,
    BillingMode,
    BillingPeriod,
    CancellationPolicy,
    CancellationRecord,
    CancellationTiming,
    CheckoutIntent,
    CheckoutStatus,
    CommercialEvent,
    EntitlementClass,
    EntitlementGrant,
    EntitlementStatus,
    GracePeriod,
    NormalizedBillingEvent,
    ENTITLEMENT_TRANSITIONS,
    ORDER_TRANSITIONS,
    Order,
    OrderAuditEvent,
    OrderItem,
    OrderStatus,
    PaymentIntentRef,
    ProductCode,
    RefundKind,
    RefundRecord,
    RenewalState,
    Subscription,
    SubscriptionAuditEvent,
    SubscriptionPlanRef,
    SubscriptionStatus,
    SUBSCRIPTION_TRANSITIONS,
)
from .ports import CommercialEventSink
from .repository import CommercialConflict, CommercialRepository


SUPPORTED_BILLING_EVENTS = frozenset(
    {
        "billing.checkout.completed",
        "billing.payment.succeeded",
        "billing.payment.failed",
        "billing.subscription.created",
        "billing.subscription.updated",
        "billing.subscription.canceled",
        "billing.refund.created",
    }
)


def _transactional(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.repository.transaction():
            return method(self, *args, **kwargs)

    return wrapped


class CommercialService:
    """Commercial control layer. Provider events enter only after normalization."""

    def __init__(
        self,
        repository: CommercialRepository,
        authorization: AuthorizationPolicy,
        events: CommercialEventSink,
        *,
        id_factory: Callable[[str], str],
        clock: Callable[[], datetime],
        grace_duration: timedelta = timedelta(days=7),
        automation_restriction_delay: timedelta = timedelta(days=2),
    ) -> None:
        self.repository = repository
        self.authorization = authorization
        self.events = events
        self.id_factory = id_factory
        self.clock = clock
        self.grace_duration = grace_duration
        self.automation_restriction_delay = automation_restriction_delay

    @_transactional
    def create_order(
        self,
        context: AuthorizationContext,
        product_version_id: str,
        *,
        amount: Amount | None = None,
        order_id: str | None = None,
    ) -> Order:
        self.authorization.require(context, Permission.AUTHORIZE_SPEND, at=self.clock())
        if context.company_id is None:
            raise ValueError("company_id required for a commercial order")
        version = self.repository.get_product_version(product_version_id)
        now = self.clock()
        item = OrderItem(
            self.id_factory("order_item"), version.product_code, product_version_id,
            version.package.display_name, version.package.billing_mode, 1, amount,
        )
        order = Order(
            order_id or self.id_factory("order"), context.tenant_id, context.actor_user_id,
            context.company_id, OrderStatus.DRAFT, (item,), now, now, total=amount,
        )
        self.repository.append_order(order)
        self._audit_order(order, context.actor_user_id, "order.created", "Customer created order")
        return order

    @_transactional
    def create_checkout(
        self, context: AuthorizationContext, order_id: str, idempotency_key: str,
        *, provider_ref: str | None = None,
    ) -> CheckoutIntent:
        self.authorization.require(context, Permission.AUTHORIZE_SPEND, at=self.clock())
        if context.company_id is None:
            raise ValueError("company_id required")
        existing = self.repository.get_checkout_by_idempotency(context.tenant_id, context.company_id, idempotency_key)
        if existing:
            return existing
        order = self.repository.get_order(context.tenant_id, context.company_id, order_id)
        if order.user_id != context.actor_user_id:
            raise PermissionError("order owner mismatch")
        order = self._transition_order(order, OrderStatus.PENDING_PAYMENT, context.actor_user_id, "Checkout opened")
        checkout = CheckoutIntent(
            self.id_factory("checkout"), order.tenant_id, order.user_id, order.company_id,
            order.order_id, provider_ref, CheckoutStatus.OPEN, idempotency_key, self.clock(),
        )
        self.repository.save_checkout(checkout)
        linked = replace(order, checkout_intent_id=checkout.checkout_intent_id, version=order.version + 1, updated_at=self.clock())
        self.repository.append_order(linked)
        return checkout

    def handle_billing_event(self, event: NormalizedBillingEvent) -> bool:
        """Apply one normalized event exactly once; raw provider payloads are prohibited."""
        if event.event_type not in SUPPORTED_BILLING_EVENTS:
            raise ValueError("unsupported normalized billing event")
        if event.raw_payload is not None:
            raise ValueError("raw provider payload must not cross the billing boundary")
        with self.repository.billing_event_transaction(
            event.provider, event.provider_event_ref
        ) as should_process:
            if not should_process:
                return False

            if event.event_type == "billing.checkout.completed":
                self._checkout_completed(event)
            elif event.event_type == "billing.payment.succeeded":
                self._payment_succeeded(event)
            elif event.event_type == "billing.payment.failed":
                self._payment_failed(event)
            elif event.event_type == "billing.subscription.created":
                self._subscription_created(event)
            elif event.event_type == "billing.subscription.updated":
                self._subscription_updated(event)
            elif event.event_type == "billing.subscription.canceled":
                self._subscription_canceled(event)
            elif event.event_type == "billing.refund.created":
                self._refund_created(event)

            return True

    @_transactional
    def request_subscription_cancellation(
        self, context: AuthorizationContext, subscription_id: str, reason: str
    ) -> Subscription:
        self.authorization.require(context, Permission.CHANGE_SUBSCRIPTION, at=self.clock())
        if context.company_id is None:
            raise ValueError("company_id required")
        subscription = self.repository.get_subscription(context.tenant_id, context.company_id, subscription_id)
        if subscription.status in {SubscriptionStatus.CANCELED, SubscriptionStatus.CANCEL_AT_PERIOD_END}:
            return subscription
        changed = self._append_subscription(
            subscription, SubscriptionStatus.CANCEL_AT_PERIOD_END, RenewalState.WILL_CANCEL,
            "Customer requested cancellation at period end", grace_period=subscription.grace_period,
            actor_id=context.actor_user_id,
        )
        self.repository.append_cancellation(
            CancellationRecord(
                self.id_factory("cancellation"), context.tenant_id, context.company_id,
                None, subscription_id, CancellationTiming.PERIOD_END, reason,
                context.actor_user_id, self.clock(), subscription.billing_period.ends_at,
            )
        )
        self._transition_managed_entitlements(
            subscription, EntitlementStatus.EXPIRING,
            "Recurring service cancels at period end", effective_until=subscription.billing_period.ends_at,
        )
        self._emit("subscription.cancellation_scheduled", subscription, context.actor_user_id, {"subscription_id": subscription_id, "effective_at": subscription.billing_period.ends_at.isoformat()})
        return changed

    @_transactional
    def cancel_order(
        self, context: AuthorizationContext, order_id: str,
        timing: CancellationTiming, reason: str,
    ) -> Order:
        self.authorization.require(context, Permission.AUTHORIZE_SPEND, at=self.clock())
        if context.company_id is None or not reason.strip():
            raise ValueError("company_id and reason required")
        if timing not in {CancellationTiming.BEFORE_FULFILLMENT, CancellationTiming.AFTER_FULFILLMENT_STARTED}:
            raise ValueError("order cancellation timing must describe fulfillment state")
        order = self.repository.get_order(context.tenant_id, context.company_id, order_id)
        if order.user_id != context.actor_user_id:
            raise PermissionError("order owner mismatch")
        if timing is CancellationTiming.BEFORE_FULFILLMENT and order.status not in {
            OrderStatus.DRAFT, OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED,
            OrderStatus.PAID, OrderStatus.FULFILLMENT_PENDING,
        }:
            raise CommercialConflict("order has already entered active fulfillment")
        if timing is CancellationTiming.AFTER_FULFILLMENT_STARTED and order.status is not OrderStatus.ACTIVE:
            raise CommercialConflict("after-fulfillment cancellation requires active order")
        changed = self._transition_order(order, OrderStatus.CANCELED, context.actor_user_id, reason)
        self.repository.append_cancellation(
            CancellationRecord(
                self.id_factory("cancellation"), order.tenant_id, order.company_id,
                order.order_id, None, timing, reason, context.actor_user_id,
                self.clock(), self.clock(),
            )
        )
        self._emit("order.canceled", changed, context.actor_user_id, {"order_id": order.order_id, "timing": timing.value})
        return changed

    @_transactional
    def record_admin_override(
        self, context: AuthorizationContext, target_type: str, target_id: str,
        reason: str, metadata: dict | None = None,
    ) -> None:
        """Audit an owner-authorized override; this method grants no additional power."""
        self.authorization.require(context, Permission.APPROVE_FOUNDER_DECISIONS, at=self.clock())
        if context.company_id is None or not reason.strip():
            raise ValueError("company_id and reason required")
        self.repository.append_order_audit(
            OrderAuditEvent(
                self.id_factory("commercial_audit"), context.tenant_id, context.company_id,
                context.actor_user_id, "admin.override_recorded", target_type, target_id,
                self.clock(), reason, "businessbuilder.commercial", metadata or {},
            )
        )

    @_transactional
    def suspend_for_security(
        self, context: AuthorizationContext, subscription_id: str, reason: str
    ) -> Subscription:
        self.authorization.require(context, Permission.CHANGE_SUBSCRIPTION, at=self.clock())
        if context.company_id is None or not reason.strip():
            raise ValueError("company_id and reason required")
        subscription = self.repository.get_subscription(context.tenant_id, context.company_id, subscription_id)
        if not subscription.cancellation_policy.immediate_security_suspension_allowed:
            raise CommercialConflict("policy does not allow immediate security suspension")
        changed = self._append_subscription(
            subscription, SubscriptionStatus.SUSPENDED, RenewalState.ENDED,
            reason, grace_period=None, actor_id=context.actor_user_id,
        )
        self.repository.append_cancellation(
            CancellationRecord(
                self.id_factory("cancellation"), context.tenant_id, context.company_id,
                None, subscription_id, CancellationTiming.IMMEDIATE_SECURITY, reason,
                context.actor_user_id, self.clock(), self.clock(),
            )
        )
        self._transition_managed_entitlements(subscription, EntitlementStatus.SUSPENDED, reason)
        self._emit("entitlement.operations_suspended", subscription, context.actor_user_id, {"subscription_id": subscription_id, "reason": reason})
        return changed

    @_transactional
    def advance_time(self, tenant_id: str, company_id: str, *, at: datetime | None = None) -> None:
        """Offline scheduler hook for grace and period-end transitions."""
        effective_at = at or self.clock()
        for subscription in self.repository.list_current_subscriptions(tenant_id, company_id):
            if subscription.status is SubscriptionStatus.CANCEL_AT_PERIOD_END and effective_at >= subscription.billing_period.ends_at:
                changed = self._append_subscription(
                    subscription, SubscriptionStatus.CANCELED, RenewalState.ENDED,
                    "Cancellation reached period end", grace_period=None, actor_id="commercial_scheduler",
                )
                self._transition_managed_entitlements(changed, EntitlementStatus.EXPIRED, "Recurring service period ended")
                self._emit("entitlement.operations_expired", changed, "commercial_scheduler", {"subscription_id": changed.subscription_id})
            elif subscription.status is SubscriptionStatus.PAST_DUE and subscription.grace_period:
                if effective_at >= subscription.grace_period.ends_at:
                    changed = self._append_subscription(
                        subscription, SubscriptionStatus.SUSPENDED, RenewalState.WILL_RENEW,
                        "Payment grace period expired", grace_period=subscription.grace_period,
                        actor_id="commercial_scheduler",
                    )
                    self._transition_managed_entitlements(changed, EntitlementStatus.SUSPENDED, "Payment grace period expired")
                    self._emit("entitlement.operations_suspended", changed, "commercial_scheduler", {"subscription_id": changed.subscription_id, "reason": "payment_grace_expired"})
                elif effective_at >= subscription.grace_period.restrict_automation_at:
                    self._transition_managed_entitlements(subscription, EntitlementStatus.SUSPENDED, "Payment grace restrictions active")

    def capability_allowed(self, tenant_id: str, company_id: str, capability: str) -> bool:
        return any(
            grant.entitlement_code == capability
            and (
                grant.status is EntitlementStatus.ACTIVE
                or (
                    grant.status is EntitlementStatus.EXPIRING
                    and grant.effective_until is not None
                    and grant.effective_until > self.clock()
                )
            )
            for grant in self.repository.get_current_entitlement_grants(tenant_id, company_id)
        )

    def _checkout_completed(self, event: NormalizedBillingEvent) -> None:
        if not event.checkout_intent_id:
            raise ValueError("checkout completion requires checkout_intent_id")
        checkout = self.repository.get_checkout(event.tenant_id, event.company_id, event.checkout_intent_id)
        self._assert_event_owner(event, checkout.user_id)
        if checkout.status is not CheckoutStatus.COMPLETED:
            self.repository.save_checkout(replace(checkout, status=CheckoutStatus.COMPLETED, provider_ref=checkout.provider_ref or event.provider_event_ref))
        self._emit_from_billing("checkout.completed", event, {"order_id": checkout.order_id, "checkout_intent_id": checkout.checkout_intent_id})

    def _payment_succeeded(self, event: NormalizedBillingEvent) -> None:
        if not event.order_id or not event.payment_provider_ref:
            raise ValueError("payment success requires order and payment references")
        order = self.repository.get_order(event.tenant_id, event.company_id, event.order_id)
        self._assert_event_owner(event, order.user_id)
        existing_payment = self.repository.get_payment_for_order(event.tenant_id, event.company_id, event.order_id)
        if existing_payment and existing_payment.provider_ref == event.payment_provider_ref and order.status in {
            OrderStatus.PAID, OrderStatus.FULFILLMENT_PENDING, OrderStatus.ACTIVE,
            OrderStatus.COMPLETED, OrderStatus.PARTIALLY_REFUNDED, OrderStatus.REFUNDED,
        }:
            return
        if order.status not in {OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED}:
            raise CommercialConflict(f"payment cannot succeed from {order.status.value}")
        payment = PaymentIntentRef(
            self.id_factory("payment"), event.tenant_id, event.company_id, order.order_id,
            event.payment_provider_ref, "succeeded", event.amount or order.total, event.occurred_at,
        )
        self.repository.save_payment(payment)
        paid = replace(order, payment_intent_ref=payment.payment_ref_id)
        paid = self._transition_order(paid, OrderStatus.PAID, event.provider, "Provider reported payment success")
        pending = self._transition_order(paid, OrderStatus.FULFILLMENT_PENDING, "commercial_service", "Paid order is eligible for fulfillment")
        for item in pending.items:
            version = self.repository.get_product_version(item.product_version_id)
            if version.package.billing_mode is BillingMode.ONE_TIME:
                self._grant_entitlements(pending, version, source_subscription_id=None)
        self._emit_from_billing("order.paid", event, {"order_id": order.order_id, "payment_ref_id": payment.payment_ref_id})
        self._emit_from_billing("commercial.fulfillment.eligible", event, {"order_id": order.order_id, "product_codes": [item.product_code.value for item in order.items]})

    def _payment_failed(self, event: NormalizedBillingEvent) -> None:
        if event.subscription_provider_ref:
            subscription = self.repository.get_subscription_by_provider_ref(event.tenant_id, event.company_id, event.subscription_provider_ref)
            if subscription is None:
                raise CommercialConflict("subscription payment failure has unknown provider reference")
            self._assert_event_owner(event, subscription.user_id)
            if subscription.status is SubscriptionStatus.PAST_DUE and subscription.grace_period is not None:
                return
            now = event.occurred_at
            grace = GracePeriod(now, now + self.grace_duration, now + self.automation_restriction_delay)
            changed = self._append_subscription(
                subscription, SubscriptionStatus.PAST_DUE, RenewalState.WILL_RENEW,
                event.reason or "Recurring payment failed", grace_period=grace, actor_id=event.provider,
            )
            self._transition_managed_entitlements(changed, EntitlementStatus.EXPIRING, "Payment failed; grace period active", effective_until=grace.ends_at)
            self._emit_from_billing("subscription.payment_failed", event, {"subscription_id": changed.subscription_id, "grace_ends_at": grace.ends_at.isoformat(), "automation_restricts_at": grace.restrict_automation_at.isoformat()})
            return
        if not event.order_id:
            raise ValueError("payment failure requires order_id or subscription reference")
        order = self.repository.get_order(event.tenant_id, event.company_id, event.order_id)
        self._assert_event_owner(event, order.user_id)
        if order.status is OrderStatus.PENDING_PAYMENT:
            self._transition_order(order, OrderStatus.PAYMENT_FAILED, event.provider, event.reason or "Payment failed")
            self._emit_from_billing("order.payment_failed", event, {"order_id": order.order_id})

    def _subscription_created(self, event: NormalizedBillingEvent) -> None:
        if not event.order_id or not event.subscription_provider_ref or not event.current_period:
            raise ValueError("subscription creation requires order, provider ref, and period")
        order = self.repository.get_order(event.tenant_id, event.company_id, event.order_id)
        self._assert_event_owner(event, order.user_id)
        recurring_items = [item for item in order.items if item.billing_mode is BillingMode.RECURRING]
        if len(recurring_items) != 1:
            raise CommercialConflict("subscription order must have exactly one recurring item")
        item = recurring_items[0]
        if self.repository.get_subscription_by_provider_ref(event.tenant_id, event.company_id, event.subscription_provider_ref):
            return
        status = event.subscription_status or SubscriptionStatus.ACTIVE
        renewal = RenewalState.WILL_CANCEL if status is SubscriptionStatus.CANCEL_AT_PERIOD_END else RenewalState.WILL_RENEW
        subscription = Subscription(
            self.id_factory("subscription"), event.tenant_id, event.user_id, event.company_id,
            order.order_id, event.subscription_provider_ref,
            SubscriptionPlanRef(item.product_code, item.product_version_id), status,
            event.current_period, renewal, CancellationPolicy("default_run_v1"),
            event.occurred_at, event.occurred_at,
        )
        self.repository.append_subscription(subscription)
        self._audit_subscription(subscription, None, status, event.provider, "Provider created subscription")
        if status in {SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING}:
            self._grant_entitlements(order, self.repository.get_product_version(item.product_version_id), subscription.subscription_id)
        self._emit_from_billing("subscription.activated", event, {"subscription_id": subscription.subscription_id, "status": status.value})

    def _subscription_updated(self, event: NormalizedBillingEvent) -> None:
        if not event.subscription_provider_ref or not event.subscription_status:
            raise ValueError("subscription update requires reference and status")
        subscription = self.repository.get_subscription_by_provider_ref(event.tenant_id, event.company_id, event.subscription_provider_ref)
        if subscription is None:
            raise CommercialConflict("unknown subscription")
        self._assert_event_owner(event, subscription.user_id)
        period = event.current_period or subscription.billing_period
        if event.subscription_status is subscription.status and period == subscription.billing_period:
            return
        changed = replace(subscription, billing_period=period)
        if event.subscription_status in {SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING}:
            changed = self._append_subscription(changed, event.subscription_status, RenewalState.WILL_RENEW, event.reason or "Subscription active", grace_period=None, actor_id=event.provider)
            self._transition_managed_entitlements(changed, EntitlementStatus.ACTIVE, "Payment current", effective_until=None)
        elif event.subscription_status is SubscriptionStatus.CANCEL_AT_PERIOD_END:
            changed = self._append_subscription(changed, event.subscription_status, RenewalState.WILL_CANCEL, event.reason or "Cancellation scheduled", grace_period=None, actor_id=event.provider)
            self._transition_managed_entitlements(changed, EntitlementStatus.EXPIRING, "Cancellation scheduled", effective_until=period.ends_at)
        elif event.subscription_status is SubscriptionStatus.SUSPENDED:
            changed = self._append_subscription(changed, event.subscription_status, subscription.renewal_state, event.reason or "Subscription suspended", grace_period=subscription.grace_period, actor_id=event.provider)
            self._transition_managed_entitlements(changed, EntitlementStatus.SUSPENDED, "Subscription suspended")
        else:
            changed = self._append_subscription(changed, event.subscription_status, subscription.renewal_state, event.reason or "Subscription updated", grace_period=subscription.grace_period, actor_id=event.provider)
        self._emit_from_billing("subscription.updated", event, {"subscription_id": changed.subscription_id, "status": changed.status.value})

    def _subscription_canceled(self, event: NormalizedBillingEvent) -> None:
        if not event.subscription_provider_ref:
            raise ValueError("subscription cancellation requires reference")
        subscription = self.repository.get_subscription_by_provider_ref(event.tenant_id, event.company_id, event.subscription_provider_ref)
        if subscription is None:
            raise CommercialConflict("unknown subscription")
        self._assert_event_owner(event, subscription.user_id)
        if subscription.status is SubscriptionStatus.CANCELED:
            return
        changed = self._append_subscription(subscription, SubscriptionStatus.CANCELED, RenewalState.ENDED, event.reason or "Provider canceled subscription", grace_period=None, actor_id=event.provider)
        self._transition_managed_entitlements(changed, EntitlementStatus.EXPIRED, "Recurring service ended")
        self._emit_from_billing("entitlement.operations_expired", event, {"subscription_id": changed.subscription_id})

    def _refund_created(self, event: NormalizedBillingEvent) -> None:
        if not event.order_id or not event.payment_provider_ref or not event.amount:
            raise ValueError("refund requires order, payment reference, and amount")
        order = self.repository.get_order(event.tenant_id, event.company_id, event.order_id)
        self._assert_event_owner(event, order.user_id)
        if any(item.provider_ref == event.provider_event_ref for item in self.repository.list_refunds(event.tenant_id, event.company_id, order.order_id)):
            return
        payment = self.repository.get_payment_for_order(event.tenant_id, event.company_id, order.order_id)
        if payment is None or payment.provider_ref != event.payment_provider_ref:
            raise CommercialConflict("refund payment does not match order")
        existing_total = sum(item.amount.minor_units for item in self.repository.list_refunds(event.tenant_id, event.company_id, order.order_id))
        order_total = order.total.minor_units if order.total else event.amount.minor_units
        if existing_total + event.amount.minor_units > order_total:
            raise CommercialConflict("refund exceeds recorded order total")
        kind = RefundKind.FULL if existing_total + event.amount.minor_units == order_total else RefundKind.PARTIAL
        refund = RefundRecord(
            self.id_factory("refund"), event.tenant_id, event.company_id, order.order_id,
            payment.payment_ref_id, event.provider_event_ref, kind, event.amount,
            event.reason or "Provider reported refund", event.occurred_at,
        )
        self.repository.append_refund(refund)
        target = OrderStatus.REFUNDED if kind is RefundKind.FULL else OrderStatus.PARTIALLY_REFUNDED
        if order.status is not target:
            self._transition_order(order, target, event.provider, refund.reason)
        self._emit_from_billing("order.refund_recorded", event, {"order_id": order.order_id, "refund_id": refund.refund_id, "kind": kind.value})

    def _grant_entitlements(self, order: Order, version, source_subscription_id: str | None) -> None:
        for definition in version.entitlements:
            grant = EntitlementGrant(
                self.id_factory("entitlement_grant"), order.tenant_id, order.user_id, order.company_id,
                definition.entitlement_code, definition.entitlement_class, EntitlementStatus.ACTIVE,
                order.order_id, source_subscription_id, self.clock(), self.clock(),
            )
            self.repository.append_entitlement_grant(grant)
            self._audit_order(order, "commercial_service", "entitlement.granted", "Entitlement activated", target_type="entitlement_grant", target_id=grant.grant_id, metadata={"entitlement_code": grant.entitlement_code, "class": grant.entitlement_class.value})
            self._emit("entitlement.activated", order, "commercial_service", {"grant_id": grant.grant_id, "entitlement_code": grant.entitlement_code, "entitlement_class": grant.entitlement_class.value})

    def _transition_managed_entitlements(
        self, subscription: Subscription, status: EntitlementStatus, reason: str,
        effective_until: datetime | None = None,
    ) -> None:
        for grant in self.repository.get_current_entitlement_grants(subscription.tenant_id, subscription.company_id):
            if grant.source_subscription_id != subscription.subscription_id or grant.entitlement_class is not EntitlementClass.STROMATION_MANAGED:
                continue
            if grant.status is status and grant.effective_until == effective_until:
                continue
            if status is not grant.status and status not in ENTITLEMENT_TRANSITIONS[grant.status]:
                raise CommercialConflict(f"illegal entitlement transition {grant.status.value} -> {status.value}")
            changed = replace(
                grant, status=status, effective_until=effective_until, status_reason=reason,
                updated_at=self.clock(), version=grant.version + 1,
            )
            self.repository.append_entitlement_grant(changed)
            order = self.repository.get_order(grant.tenant_id, grant.company_id, grant.source_order_id)
            self._audit_order(order, "commercial_service", f"entitlement.{status.value}", reason, target_type="entitlement_grant", target_id=grant.grant_id, metadata={"entitlement_code": grant.entitlement_code})

    def _transition_order(self, order: Order, target: OrderStatus, actor: str, reason: str) -> Order:
        if target is order.status:
            return order
        if target not in ORDER_TRANSITIONS[order.status]:
            raise CommercialConflict(f"illegal order transition {order.status.value} -> {target.value}")
        changed = replace(order, status=target, updated_at=self.clock(), version=order.version + 1)
        self.repository.append_order(changed)
        self._audit_order(changed, actor, f"order.{target.value}", reason, metadata={"prior_status": order.status.value})
        return changed

    def _append_subscription(
        self, subscription: Subscription, status: SubscriptionStatus, renewal: RenewalState,
        reason: str, *, grace_period: GracePeriod | None, actor_id: str,
    ) -> Subscription:
        if status is not subscription.status and status not in SUBSCRIPTION_TRANSITIONS[subscription.status]:
            raise CommercialConflict(f"illegal subscription transition {subscription.status.value} -> {status.value}")
        changed = replace(
            subscription, status=status, renewal_state=renewal, grace_period=grace_period,
            updated_at=self.clock(), version=subscription.version + 1,
        )
        self.repository.append_subscription(changed)
        self._audit_subscription(changed, subscription.status, status, actor_id, reason)
        return changed

    def _assert_event_owner(self, event: NormalizedBillingEvent, expected_user_id: str) -> None:
        if event.user_id != expected_user_id:
            raise CommercialConflict("billing event user does not match commercial record")

    def _audit_order(
        self, order: Order, actor_id: str, action: str, reason: str,
        *, target_type: str = "order", target_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.repository.append_order_audit(
            OrderAuditEvent(
                self.id_factory("commercial_audit"), order.tenant_id, order.company_id,
                actor_id, action, target_type, target_id or order.order_id, self.clock(), reason,
                "businessbuilder.commercial", metadata or {},
            )
        )

    def _audit_subscription(
        self, subscription: Subscription, prior: SubscriptionStatus | None,
        new: SubscriptionStatus, actor_id: str, reason: str,
    ) -> None:
        self.repository.append_subscription_audit(
            SubscriptionAuditEvent(
                self.id_factory("subscription_audit"), subscription.tenant_id,
                subscription.company_id, actor_id, subscription.subscription_id,
                prior, new, self.clock(), reason, "businessbuilder.commercial",
            )
        )

    def _emit(self, event_type: str, subject, actor: str, payload: dict) -> None:
        self.events.publish(
            CommercialEvent(
                self.id_factory("commercial_event"), event_type, subject.tenant_id,
                subject.user_id, subject.company_id, self.clock(), "businessbuilder.commercial",
                f"commercial:{getattr(subject, 'order_id', getattr(subject, 'subscription_id', 'event'))}",
                None, payload,
            )
        )

    def _emit_from_billing(self, event_type: str, event: NormalizedBillingEvent, payload: dict) -> None:
        self.events.publish(
            CommercialEvent(
                self.id_factory("commercial_event"), event_type, event.tenant_id,
                event.user_id, event.company_id, self.clock(), "businessbuilder.commercial",
                event.correlation_id, event.event_id, payload,
            )
        )
