from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from functools import wraps
from hashlib import sha256
import json
from typing import Callable

from businessbuilder.identity import AuthorizationContext, AuthorizationDenied, AuthorizationPolicy, Permission

from .models import (
    Amount,
    AccessSource,
    BillingMode,
    BillingPeriod,
    CancellationPolicy,
    CancellationRecord,
    CancellationTiming,
    CheckoutIntent,
    CheckoutStatus,
    CommercialQuote,
    CommercialAdmissionRecord,
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
    PaymentEligibility,
    QuoteStatus,
    TaxDisposition,
    TaxReviewState,
    PaymentIntentRef,
    ProductCode,
    PilotAttempt,
    PilotRedemption,
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
from .outbox import CommercialOutboxDispatcher
from .repository import CommercialConflict, CommercialRepository
from .pricing import FOUNDING_PRICES, OfferCode, PriceKind
from .operator_authority import CommercialOperatorAuthority, CommercialOperatorPrincipal
from .pilot_access import (PilotCodeReader, PilotSecretUnavailable, code_matches,
                           INVALID_ATTEMPT_LIMIT, INVALID_ATTEMPT_WINDOW)


SUPPORTED_BILLING_EVENTS = frozenset(
    {
        "billing.checkout.completed",
        "billing.checkout.expired",
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
        try:
            with self.repository.transaction():
                result = method(self, *args, **kwargs)
        except AuthorizationDenied:
            if method.__name__ in {"release_payment", "create_existing_business_quote"} and self.operator_authority:
                self.operator_authority.record_durable_denial(
                    operation=method.__name__, order_id=kwargs.get("order_id"),
                )
            raise
        if self.auto_dispatch_outbox:
            self.dispatch_pending_events()
        return result

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
        auto_dispatch_outbox: bool = True,
        outbox_dispatcher_id: str = "commercial-inline",
        operator_authority: CommercialOperatorAuthority | None = None,
        allow_test_admission: bool = False,
        tax_authority_verifier: Callable[[TaxReviewState, TaxDisposition, str | None], bool] | None = None,
        paid_pilot_release_gate=None,
    ) -> None:
        self.repository = repository
        self.authorization = authorization
        self.events = events
        self.id_factory = id_factory
        self.clock = clock
        self.grace_duration = grace_duration
        self.automation_restriction_delay = automation_restriction_delay
        self.auto_dispatch_outbox = auto_dispatch_outbox
        self.operator_authority = operator_authority
        self.allow_test_admission = allow_test_admission
        self.tax_authority_verifier = tax_authority_verifier
        self.paid_pilot_release_gate = paid_pilot_release_gate
        self.outbox = CommercialOutboxDispatcher(
            repository,
            events,
            dispatcher_id=outbox_dispatcher_id,
            clock=clock,
        )

    def dispatch_pending_events(self, *, limit: int = 100) -> int:
        return self.outbox.dispatch_pending(limit=limit)

    def require_live_checkout(self, order: Order):
        if self.paid_pilot_release_gate is None:
            raise CommercialConflict("live paid-pilot release gate not configured")
        self._require_current_admission(order)
        return self.paid_pilot_release_gate.require_live_charge(order)

    def _admission_digest(self, order: Order) -> str:
        quote = self.repository.get_quote(order.tenant_id, order.company_id, order.quote_id) if order.quote_id else None
        payload = {
            "order_id": order.order_id, "user_id": order.user_id,
            "tenant_id": order.tenant_id, "company_id": order.company_id,
            "offer_code": order.offer_code,
            "existing_audit_ref": order.existing_audit_ref,
            "existing_recommendation_digest": order.existing_recommendation_digest,
            "items": [(item.product_version_id, item.quantity,
                       item.unit_amount.currency if item.unit_amount else None,
                       item.unit_amount.minor_units if item.unit_amount else None) for item in order.items],
            "total": (order.total.currency, order.total.minor_units) if order.total else None,
            "quote": (quote.quote_id, quote.version, quote.recommendation_digest, quote.audit_ref,
                      quote.upfront.minor_units, quote.monthly.minor_units,
                      quote.approved_by, quote.approved_at.isoformat() if quote.approved_at else None,
                      quote.expires_at.isoformat()) if quote else None,
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _require_current_admission(self, order: Order) -> CommercialAdmissionRecord:
        if not order.admission_id:
            raise CommercialConflict("commercial operator admission required")
        admission = self.repository.get_admission(order.tenant_id, order.company_id, order.admission_id)
        now = self.clock()
        if (admission.order_id != order.order_id or admission.quote_id != order.quote_id
            or admission.order_digest != self._admission_digest(order)
            or admission.eligibility is not PaymentEligibility.PAY_NOW_ELIGIBLE
            or admission.expires_at <= now or (admission.eligible_at and admission.eligible_at > now)):
            raise CommercialConflict("commercial admission is stale or delayed")
        if admission.test_only:
            if not self.allow_test_admission:
                raise CommercialConflict("test admission disabled")
        else:
            if self.operator_authority is None:
                raise CommercialConflict("commercial operator authority unavailable")
            grant = self.repository.get_operator_grant(order.tenant_id, order.company_id, admission.operator_grant_id)
            if not grant.active_at(now) or admission.operator_user_id != grant.operator_user_id:
                raise CommercialConflict("commercial operator appointment expired or revoked")
        if admission.tax_review_state is TaxReviewState.TAX_REVIEW_REQUIRED:
            raise CommercialConflict("tax review required")
        return admission

    @_transactional
    def create_order(
        self,
        context: AuthorizationContext,
        product_version_id: str,
        *,
        amount: Amount | None = None,
        order_id: str | None = None,
        offer_code: OfferCode | None = None,
    ) -> Order:
        self.authorization.require(context, Permission.AUTHORIZE_SPEND, at=self.clock())
        if context.company_id is None:
            raise ValueError("company_id required for a commercial order")
        version = self.repository.get_product_version(product_version_id)
        if offer_code is not None:
            price = FOUNDING_PRICES[offer_code]
            if price.kind is PriceKind.QUOTE_REQUIRED:
                raise CommercialConflict("existing-business order requires an approved quote")
            expected = {
                OfferCode.WEBSITE: ProductCode.BUILD_WEBSITE,
                OfferCode.BUSINESS: ProductCode.BUILD_BUSINESS,
            }
            if offer_code not in expected or version.product_code is not expected[offer_code]:
                raise CommercialConflict("canonical offer does not match its product version")
            amount = Amount(price.currency, price.upfront_minor or 0)
        now = self.clock()
        item = OrderItem(
            self.id_factory("order_item"), version.product_code, product_version_id,
            version.package.display_name, version.package.billing_mode, 1, amount,
        )
        order = Order(
            order_id or self.id_factory("order"), context.tenant_id, context.actor_user_id,
            context.company_id, OrderStatus.DRAFT, (item,), now, now, total=amount,
            offer_code=offer_code.value if offer_code else None,
        )
        self.repository.append_order(order)
        self._audit_order(order, context.actor_user_id, "order.created", "Customer created order")
        return order

    @_transactional
    def select_fixed_offer(self, context: AuthorizationContext, order_id: str, offer_code: OfferCode) -> Order:
        """Reprice only an uncharged, approved new-business order."""
        self.authorization.require(context, Permission.AUTHORIZE_SPEND, at=self.clock())
        if context.company_id is None:
            raise PermissionError("company scope required")
        order = self.repository.get_order(context.tenant_id, context.company_id, order_id)
        if order.user_id != context.actor_user_id or order.status is not OrderStatus.DRAFT:
            raise CommercialConflict("order is not an owner-controlled draft")
        if order.items[0].product_code is not ProductCode.BUILD_BUSINESS or offer_code not in {OfferCode.BUSINESS, OfferCode.BUSINESS_RUN}:
            raise CommercialConflict("offer does not match approved new-business scope")
        if order.offer_code == offer_code.value:
            return order
        price = FOUNDING_PRICES[offer_code]
        upfront = Amount("USD", price.upfront_minor or 0)
        build_item = replace(order.items[0], unit_amount=upfront)
        items = (build_item,)
        if offer_code is OfferCode.BUSINESS_RUN:
            version_id = "product_version_build_and_run_v1"
            version = self.repository.get_product_version(version_id)
            items += (OrderItem(
                self.id_factory("order_item"), version.product_code, version_id,
                version.package.display_name, BillingMode.RECURRING, 1,
                Amount("USD", price.monthly_minor or 0),
            ),)
        first_due = Amount("USD", upfront.minor_units + (price.monthly_minor or 0))
        changed = replace(
            order, items=items, total=first_due, offer_code=offer_code.value,
            version=order.version + 1, updated_at=self.clock(),
        )
        self.repository.append_order(changed)
        self._audit_order(changed, context.actor_user_id, "order.offer_selected", "Founder selected a canonical fixed offer")
        return changed

    @_transactional
    def redeem_pilot_access(
        self, context: AuthorizationContext, order_id: str, submitted_code: str,
        secret_reader: PilotCodeReader,
    ) -> bool:
        """Atomically admit one founder-approved cleaning company without Stripe."""
        self.authorization.require(context, Permission.AUTHORIZE_SPEND, at=self.clock())
        if context.company_id is None:
            raise PermissionError("company scope required")
        tenant_id, company_id = context.tenant_id, context.company_id
        self.repository.lock_pilot_scope(tenant_id, company_id)
        order = self.repository.get_order(tenant_id, company_id, order_id)
        if order.user_id != context.actor_user_id:
            raise PermissionError("order owner mismatch")
        redeemed = self.repository.get_pilot_redemption(tenant_id, company_id)
        if redeemed is not None:
            if redeemed.order_id != order_id:
                raise CommercialConflict("company pilot access already redeemed")
            return True
        if order.status is not OrderStatus.DRAFT or order.checkout_intent_id is not None:
            raise CommercialConflict("approved uncharged draft order required")
        if not order.items or order.items[0].product_code is not ProductCode.BUILD_BUSINESS:
            raise CommercialConflict("pilot access requires Build My Business scope")
        recent = self.repository.recent_pilot_attempts(
            tenant_id, company_id, self.clock() - INVALID_ATTEMPT_WINDOW)
        if sum(item.outcome in {"INVALID", "UNAVAILABLE"} for item in recent) >= INVALID_ATTEMPT_LIMIT:
            self._audit_order(order, context.actor_user_id, "pilot_access.rate_limited",
                              "Pilot redemption rate limited")
            return False
        try:
            configured_code = secret_reader.read_code()
            valid = code_matches(submitted_code, configured_code)
        except PilotSecretUnavailable:
            # Fail closed; no submitted/configured value or AWS error is persisted.
            self.repository.append_pilot_attempt(PilotAttempt(
                self.id_factory("pilot_attempt"), tenant_id, company_id,
                context.actor_user_id, self.clock(), "UNAVAILABLE"))
            self._audit_order(order, context.actor_user_id, "pilot_access.unavailable",
                              "Pilot access validation unavailable")
            return False
        if not valid:
            self.repository.append_pilot_attempt(PilotAttempt(
                self.id_factory("pilot_attempt"), tenant_id, company_id,
                context.actor_user_id, self.clock(), "INVALID"))
            self._audit_order(order, context.actor_user_id, "pilot_access.invalid",
                              "Invalid pilot access attempt")
            return False
        build_version = self.repository.get_product_version(order.items[0].product_version_id)
        run_version_id = "product_version_build_and_run_v1"
        run_version = self.repository.get_product_version(run_version_id)
        build_item = replace(order.items[0], unit_amount=Amount("USD", 0))
        run_item = OrderItem(
            self.id_factory("order_item"), ProductCode.BUILD_AND_RUN,
            run_version_id, run_version.package.display_name, BillingMode.RECURRING,
            1, Amount("USD", 0),
        )
        zero_order = replace(
            order, items=(build_item, run_item), total=Amount("USD", 0),
            offer_code=OfferCode.BUSINESS_RUN.value,
            access_source=AccessSource.PILOT_ACCESS,
        )
        admitted = self._transition_order(
            zero_order, OrderStatus.FULFILLMENT_PENDING,
            context.actor_user_id, "Pilot access admitted; no payment occurred")
        redemption = PilotRedemption(
            self.id_factory("pilot_redemption"), tenant_id, company_id,
            admitted.order_id, context.actor_user_id, self.clock())
        self.repository.append_pilot_redemption(redemption)
        self._grant_entitlements(admitted, build_version, source_subscription_id=None)
        self._grant_entitlements(admitted, run_version, source_subscription_id=None)
        self._audit_order(admitted, context.actor_user_id, "pilot_access.redeemed",
                          "Company pilot access granted", target_type="pilot_redemption",
                          target_id=redemption.redemption_id,
                          metadata={"source": AccessSource.PILOT_ACCESS.value})
        self._emit("commercial.fulfillment.eligible", admitted, context.actor_user_id,
                   {"order_id": admitted.order_id,
                    "product_codes": [item.product_code.value for item in admitted.items],
                    "source": AccessSource.PILOT_ACCESS.value})
        return True

    @_transactional
    def create_existing_business_order(
        self, context: AuthorizationContext, *, audit_ref: str,
        recommendation_digest: str, order_id: str | None = None,
    ) -> Order:
        """Only a recorded Existing Business Audit may lead to a scoped draft."""
        self.authorization.require(context, Permission.AUTHORIZE_SPEND, at=self.clock())
        if context.company_id is None or not audit_ref or len(recommendation_digest) != 64:
            raise CommercialConflict("an audit-backed scope is required")
        now = self.clock()
        setup_version_id = "product_version_existing_business_onboarding_v1"
        run_version_id = "product_version_existing_business_run_v1"
        setup_version = self.repository.get_product_version(setup_version_id)
        run_version = self.repository.get_product_version(run_version_id)
        order = Order(
            order_id or self.id_factory("order"), context.tenant_id, context.actor_user_id,
            context.company_id, OrderStatus.DRAFT,
            (
                OrderItem(self.id_factory("order_item"), setup_version.product_code,
                          setup_version_id, setup_version.package.display_name,
                          BillingMode.ONE_TIME, 1, None),
                OrderItem(self.id_factory("order_item"), run_version.product_code,
                          run_version_id, run_version.package.display_name,
                          BillingMode.RECURRING, 1, Amount("USD", 29900)),
            ), now, now, offer_code=OfferCode.EXISTING_RUN.value,
            existing_audit_ref=audit_ref,
            existing_recommendation_digest=recommendation_digest,
        )
        self.repository.append_order(order)
        self._audit_order(order, context.actor_user_id, "order.audit_scope_recorded", "Existing Business Audit scope recorded")
        return order

    @_transactional
    def create_existing_business_quote(
        self, *, tenant_id: str, company_id: str, order_id: str,
        audit_ref: str, recommendation_digest: str, upfront_minor: int,
        expires_at: datetime,
        operator_principal: CommercialOperatorPrincipal | None = None,
    ) -> CommercialQuote:
        """Scoped quote requires current appointed operator authority outside test mode."""
        if self.operator_authority is not None:
            operator = self.operator_authority.verify(
                operator_principal, tenant_id=tenant_id, company_id=company_id,
                action="quote.publish", order_id=order_id,
            )
            actor = operator.operator_user_id
        elif self.allow_test_admission:
            actor = "deterministic_test_operator"
        else:
            raise CommercialConflict("commercial operator authority required")
        order = self.repository.get_order(tenant_id, company_id, order_id)
        if order.quote_id:
            existing = self.repository.get_quote(tenant_id, company_id, order.quote_id)
            if (existing.status is QuoteStatus.PROPOSED
                and existing.expires_at > self.clock()
                and existing.audit_ref == audit_ref
                and existing.recommendation_digest == recommendation_digest
                and existing.upfront == Amount("USD", upfront_minor)
                and existing.expires_at == expires_at):
                return existing
            raise CommercialConflict("quote retry changed the current scoped terms")
        if (order.status is not OrderStatus.DRAFT or not audit_ref or len(recommendation_digest) != 64
            or order.existing_audit_ref != audit_ref
            or order.existing_recommendation_digest != recommendation_digest
        ):
            raise CommercialConflict("a draft and digest-bound audit are required")
        if order.offer_code != OfferCode.EXISTING_RUN.value:
            raise CommercialConflict("quote requires the distinct existing-business package")
        floor = FOUNDING_PRICES[OfferCode.EXISTING_RUN].floor_minor or 0
        if upfront_minor < floor or expires_at <= self.clock():
            raise CommercialConflict("quote amount or expiration is invalid")
        quote = CommercialQuote(
            self.id_factory("quote"), tenant_id, company_id, order_id,
            recommendation_digest, audit_ref, Amount("USD", upfront_minor),
            Amount("USD", 29900), QuoteStatus.PROPOSED,
            self.clock(), expires_at,
        )
        self.repository.append_quote(quote)
        linked = replace(order, quote_id=quote.quote_id,
                         version=order.version + 1, updated_at=self.clock())
        self.repository.append_order(linked)
        self._audit_order(linked, actor, "quote.proposed", "Audit-backed onboarding quote proposed")
        return quote

    @_transactional
    def approve_quote(self, context: AuthorizationContext, quote_id: str, recommendation_digest: str) -> CommercialQuote:
        self.authorization.require(context, Permission.AUTHORIZE_SPEND, at=self.clock())
        if context.company_id is None:
            raise PermissionError("company scope required")
        quote = self.repository.get_quote(context.tenant_id, context.company_id, quote_id)
        order = self.repository.get_order(context.tenant_id, context.company_id, quote.order_id)
        if (quote.status is QuoteStatus.APPROVED and quote.approved_by == context.actor_user_id
            and quote.recommendation_digest == recommendation_digest
            and order.quote_id == quote_id):
            return quote
        if (order.user_id != context.actor_user_id or order.quote_id != quote_id
            or quote.status is not QuoteStatus.PROPOSED):
            raise CommercialConflict("quote is not approvable")
        if quote.expires_at <= self.clock() or quote.recommendation_digest != recommendation_digest:
            raise CommercialConflict("stale or mismatched quote")
        approved = replace(quote, status=QuoteStatus.APPROVED, approved_by=context.actor_user_id,
                           approved_at=self.clock(), version=quote.version + 1)
        self.repository.append_quote(approved)
        setup_item = replace(order.items[0], unit_amount=quote.upfront)
        changed = replace(order, quote_id=quote_id,
                          items=(setup_item, *order.items[1:]),
                          total=Amount("USD", quote.upfront.minor_units + quote.monthly.minor_units),
                          version=order.version + 1, updated_at=self.clock())
        self.repository.append_order(changed)
        self._audit_order(changed, context.actor_user_id, "quote.approved", "Founder approved exact quote digest")
        return approved

    @_transactional
    def create_checkout(
        self, context: AuthorizationContext, order_id: str, idempotency_key: str,
        *, provider_ref: str | None = None,
    ) -> CheckoutIntent:
        self.authorization.require(context, Permission.AUTHORIZE_SPEND, at=self.clock())
        if context.company_id is None:
            raise ValueError("company_id required")
        order = self.repository.get_order(context.tenant_id, context.company_id, order_id)
        if order.user_id != context.actor_user_id:
            raise PermissionError("order owner mismatch")
        if order.access_source is AccessSource.PILOT_ACCESS:
            raise CommercialConflict("pilot access is not a payable checkout")
        existing = self.repository.get_checkout_by_idempotency(context.tenant_id, context.company_id, idempotency_key)
        if existing:
            if existing.order_id != order_id:
                raise CommercialConflict("checkout idempotency key belongs to another order")
            return existing
        if order.offer_code is not None and order.total is None:
            raise CommercialConflict("canonical priced order required")
        if order.offer_code == OfferCode.EXISTING_RUN.value:
            if not order.quote_id:
                raise CommercialConflict("existing-business quote required")
            quote = self.repository.get_quote(order.tenant_id, order.company_id, order.quote_id)
            quoted_first_charge = Amount("USD", quote.upfront.minor_units + quote.monthly.minor_units)
            if quote.status is not QuoteStatus.APPROVED or quote.expires_at <= self.clock() or quoted_first_charge != order.total:
                raise CommercialConflict("approved, current quote required")
        if order.offer_code is not None and (order.eligibility is not PaymentEligibility.PAY_NOW_ELIGIBLE or (
            order.eligible_at is not None and order.eligible_at > self.clock()
        )):
            raise CommercialConflict("payment eligibility delay is active")
        if order.offer_code is not None:
            self._require_current_admission(order)
        if order.offer_code is not None and order.tax_disposition is TaxDisposition.MANUAL_REVIEW:
            raise CommercialConflict("tax treatment awaits authorized review")
        order = self._transition_order(order, OrderStatus.PENDING_PAYMENT, context.actor_user_id, "Checkout opened")
        checkout = CheckoutIntent(
            self.id_factory("checkout"), order.tenant_id, order.user_id, order.company_id,
            order.order_id, provider_ref, CheckoutStatus.OPEN, idempotency_key, self.clock(),
            expected_customer_email_digest=sha256(
                self.authorization.repository.get_user(order.user_id).email.strip().lower().encode()
            ).hexdigest(),
        )
        self.repository.save_checkout(checkout)
        linked = replace(order, checkout_intent_id=checkout.checkout_intent_id, version=order.version + 1, updated_at=self.clock())
        self.repository.append_order(linked)
        return checkout

    @_transactional
    def record_payment_readiness(
        self, *, tenant_id: str, company_id: str, order_id: str,
        eligibility: PaymentEligibility, eligible_at: datetime | None,
        tax_disposition: TaxDisposition, review_ref: str,
    ) -> Order:
        """Deterministic offline admission only; production rejects this path."""
        if not self.allow_test_admission:
            raise CommercialConflict("test payment admission disabled")
        if not review_ref or len(review_ref) > 160:
            raise CommercialConflict("supervised decision reference required")
        order = self.repository.get_order(tenant_id, company_id, order_id)
        if order.status is not OrderStatus.DRAFT or not order.offer_code or order.total is None:
            raise CommercialConflict("priced draft required")
        if order.offer_code == OfferCode.EXISTING_RUN.value:
            if not order.quote_id or self.repository.get_quote(tenant_id, company_id, order.quote_id).status is not QuoteStatus.APPROVED:
                raise CommercialConflict("founder-approved quote required")
        if eligibility is PaymentEligibility.PAY_NOW_ELIGIBLE and eligible_at is not None and eligible_at > self.clock():
            raise CommercialConflict("future eligibility cannot be pay-now")
        if tax_disposition is TaxDisposition.MANUAL_REVIEW and eligibility is PaymentEligibility.PAY_NOW_ELIGIBLE:
            raise CommercialConflict("manual tax review cannot be pay-now")
        admission = CommercialAdmissionRecord(
            self.id_factory("admission"), tenant_id, company_id, order_id, order.quote_id,
            "deterministic_test_operator", "deterministic_test_grant",
            self._admission_digest(order), eligibility, eligible_at, tax_disposition,
            TaxReviewState.TEST_MODE_UNDETERMINED if tax_disposition is TaxDisposition.TEST_MODE_UNDETERMINED
            else TaxReviewState.TAX_APPROVED,
            review_ref, review_ref, self.clock(), self.clock() + timedelta(hours=1), True,
        )
        self.repository.append_admission(admission)
        changed = replace(order, eligibility=eligibility, eligible_at=eligible_at,
                          tax_disposition=tax_disposition, admission_id=admission.admission_id,
                          version=order.version + 1, updated_at=self.clock())
        self.repository.append_order(changed)
        self._audit_order(changed, "deterministic_test_operator", "order.eligibility_reviewed", review_ref)
        return changed

    @_transactional
    def release_payment(
        self, operator_principal: CommercialOperatorPrincipal, *, tenant_id: str,
        company_id: str, order_id: str, eligibility: PaymentEligibility,
        eligible_at: datetime | None, tax_disposition: TaxDisposition,
        tax_review_state: TaxReviewState, decision_ref: str,
        tax_review_ref: str | None, expires_at: datetime,
    ) -> Order:
        """Immutable, digest-bound release by an appointed non-founder operator."""
        if self.operator_authority is None:
            raise CommercialConflict("commercial operator authority required")
        operator = self.operator_authority.verify(
            operator_principal, tenant_id=tenant_id, company_id=company_id,
            action="payment.release", order_id=order_id,
        )
        grant = self.repository.get_operator_grant(tenant_id, company_id, operator.grant_id)
        now = self.clock()
        if not decision_ref or len(decision_ref) > 160 or expires_at <= now or expires_at > grant.ends_at:
            raise CommercialConflict("scoped release reference or expiry invalid")
        if eligibility is PaymentEligibility.PAY_NOW_ELIGIBLE and eligible_at and eligible_at > now:
            raise CommercialConflict("future eligibility requires payment delay")
        if eligibility is PaymentEligibility.PAY_NOW_ELIGIBLE:
            test_boundary = (self.allow_test_admission
                             and tax_review_state is TaxReviewState.TEST_MODE_UNDETERMINED
                             and tax_disposition is TaxDisposition.TEST_MODE_UNDETERMINED)
            reviewed_boundary = (
                tax_review_state in {TaxReviewState.TAX_APPROVED, TaxReviewState.PROVIDER_CALCULATED}
                and tax_disposition not in {TaxDisposition.MANUAL_REVIEW, TaxDisposition.TEST_MODE_UNDETERMINED}
                and bool(tax_review_ref)
                and self.tax_authority_verifier is not None
                and self.tax_authority_verifier(tax_review_state, tax_disposition, tax_review_ref)
            )
            if not (test_boundary or reviewed_boundary):
                raise CommercialConflict("tax authority review or explicit test boundary required")
        order = self.repository.get_order(tenant_id, company_id, order_id)
        if order.status is not OrderStatus.DRAFT or not order.offer_code or order.total is None:
            raise CommercialConflict("priced draft required")
        supersedes_admission_id = None
        if order.admission_id:
            existing = self.repository.get_admission(tenant_id, company_id, order.admission_id)
            if (existing.operator_user_id == operator.operator_user_id
                and existing.operator_grant_id == operator.grant_id
                and existing.decision_ref == decision_ref
                and existing.eligibility is eligibility
                and existing.eligible_at == eligible_at
                and existing.tax_disposition is tax_disposition
                and existing.tax_review_state is tax_review_state
                and existing.tax_review_ref == tax_review_ref
                and existing.expires_at == expires_at
                and existing.order_digest == self._admission_digest(order)):
                return order
            if (existing.eligibility is not PaymentEligibility.PAYMENT_DELAY_REQUIRED
                or eligibility is not PaymentEligibility.PAY_NOW_ELIGIBLE
                or existing.eligible_at is None or existing.eligible_at > now
                or existing.order_digest != self._admission_digest(order)
                or existing.quote_id != order.quote_id
                or existing.decision_ref == decision_ref
                or order.checkout_intent_id is not None):
                raise CommercialConflict("commercial admission retry changed its scope or terms")
            supersedes_admission_id = existing.admission_id
        if order.offer_code == OfferCode.EXISTING_RUN.value:
            if not order.quote_id:
                raise CommercialConflict("founder-approved quote required")
            quote = self.repository.get_quote(tenant_id, company_id, order.quote_id)
            if quote.status is not QuoteStatus.APPROVED or quote.expires_at <= now:
                raise CommercialConflict("founder-approved current quote required")
        admission = CommercialAdmissionRecord(
            self.id_factory("admission"), tenant_id, company_id, order_id,
            order.quote_id, operator.operator_user_id, operator.grant_id,
            self._admission_digest(order), eligibility, eligible_at,
            tax_disposition, tax_review_state, decision_ref, tax_review_ref,
            now, expires_at, supersedes_admission_id=supersedes_admission_id,
        )
        self.repository.append_admission(admission)
        changed = replace(order, eligibility=eligibility, eligible_at=eligible_at,
                          tax_disposition=tax_disposition, admission_id=admission.admission_id,
                          version=order.version + 1, updated_at=now)
        self.repository.append_order(changed)
        self._audit_order(changed, operator.operator_user_id, "order.payment_released", decision_ref)
        return changed

    def handle_billing_event(self, event: NormalizedBillingEvent) -> bool:
        """Apply one normalized event exactly once; raw provider payloads are prohibited."""
        if event.event_type not in SUPPORTED_BILLING_EVENTS:
            raise ValueError("unsupported normalized billing event")
        if event.raw_payload is not None:
            raise ValueError("raw provider payload must not cross the billing boundary")
        applied = False
        with self.repository.billing_event_transaction(
            event.provider, event.provider_event_ref
        ) as should_process:
            if should_process:
                if event.event_type == "billing.checkout.completed":
                    self._checkout_completed(event)
                elif event.event_type == "billing.checkout.expired":
                    self._checkout_expired(event)
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
                applied = True
        if self.auto_dispatch_outbox:
            self.dispatch_pending_events()
        return applied

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

    def _checkout_expired(self, event: NormalizedBillingEvent) -> None:
        if not event.checkout_intent_id:
            raise ValueError("checkout expiry requires checkout_intent_id")
        checkout = self.repository.get_checkout(event.tenant_id, event.company_id, event.checkout_intent_id)
        self._assert_event_owner(event, checkout.user_id)
        if checkout.status is CheckoutStatus.OPEN:
            self.repository.save_checkout(replace(checkout, status=CheckoutStatus.EXPIRED))
        order = self.repository.get_order(event.tenant_id, event.company_id, checkout.order_id)
        if order.status is OrderStatus.PENDING_PAYMENT:
            self._transition_order(order, OrderStatus.CANCELED, event.provider, "Provider checkout expired")

    def _payment_succeeded(self, event: NormalizedBillingEvent) -> None:
        if not event.order_id or not event.payment_provider_ref:
            raise ValueError("payment success requires order and payment references")
        order = self.repository.get_order(event.tenant_id, event.company_id, event.order_id)
        self._assert_event_owner(event, order.user_id)
        if event.amount is not None and order.total is not None and (
            event.amount.currency != order.total.currency
            or (
                event.amount.minor_units < order.total.minor_units
                if order.tax_disposition is TaxDisposition.PROVIDER_CALCULATED
                else event.amount != order.total
            )
        ):
            raise CommercialConflict("provider amount does not match priced order")
        if event.provider == "stripe":
            if not event.checkout_intent_id or event.checkout_intent_id != order.checkout_intent_id:
                raise CommercialConflict("Stripe payment is not bound to this checkout")
            checkout = self.repository.get_checkout(event.tenant_id, event.company_id, event.checkout_intent_id)
            if checkout.order_id != order.order_id or not checkout.provider_ref or checkout.status is not CheckoutStatus.COMPLETED:
                raise CommercialConflict("Stripe checkout mapping is absent")
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
        paid = replace(order, payment_intent_ref=payment.payment_ref_id,
                       access_source=(AccessSource.STRIPE_PAYMENT if event.provider == "stripe"
                                      else AccessSource.PROVIDER_PAYMENT))
        paid = self._transition_order(paid, OrderStatus.PAID, event.provider, "Provider reported payment success")
        pending = self._transition_order(paid, OrderStatus.FULFILLMENT_PENDING, "commercial_service", "Paid order is eligible for fulfillment")
        for item in pending.items:
            version = self.repository.get_product_version(item.product_version_id)
            if version.package.billing_mode is BillingMode.ONE_TIME:
                self._grant_entitlements(pending, version, source_subscription_id=None)
        for subscription in self.repository.list_current_subscriptions(order.tenant_id, order.company_id):
            if subscription.order_id == order.order_id and subscription.status in {SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING}:
                self._grant_entitlements(pending, self.repository.get_product_version(subscription.plan.product_version_id), subscription.subscription_id)
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
        if status in {SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING} and order.status in {
            OrderStatus.PAID, OrderStatus.FULFILLMENT_PENDING, OrderStatus.ACTIVE,
        }:
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
            order = self.repository.get_order(event.tenant_id, event.company_id, changed.order_id)
            if order.status in {OrderStatus.PAID, OrderStatus.FULFILLMENT_PENDING, OrderStatus.ACTIVE}:
                self._grant_entitlements(order, self.repository.get_product_version(changed.plan.product_version_id), changed.subscription_id)
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
        if kind is RefundKind.FULL:
            for grant in self.repository.get_current_entitlement_grants(order.tenant_id, order.company_id):
                if grant.source_order_id != order.order_id or grant.entitlement_class is not EntitlementClass.STROMATION_MANAGED:
                    continue
                if grant.status in {EntitlementStatus.ACTIVE, EntitlementStatus.EXPIRING}:
                    changed = replace(
                        grant, status=EntitlementStatus.SUSPENDED,
                        status_reason="Initial charge fully refunded; supervised fulfillment review required",
                        updated_at=self.clock(), version=grant.version + 1,
                    )
                    self.repository.append_entitlement_grant(changed)
                    self._audit_order(order, "commercial_service", "entitlement.suspended_after_refund",
                                      "Full refund paused managed fulfillment", target_type="entitlement_grant",
                                      target_id=grant.grant_id)
        self._emit_from_billing("order.refund_recorded", event, {"order_id": order.order_id, "refund_id": refund.refund_id, "kind": kind.value})

    def _grant_entitlements(self, order: Order, version, source_subscription_id: str | None) -> None:
        for definition in version.entitlements:
            if any(
                grant.source_order_id == order.order_id
                and grant.source_subscription_id == source_subscription_id
                and grant.entitlement_code == definition.entitlement_code
                for grant in self.repository.get_current_entitlement_grants(order.tenant_id, order.company_id)
            ):
                continue
            grant = EntitlementGrant(
                self.id_factory("entitlement_grant"), order.tenant_id, order.user_id, order.company_id,
                definition.entitlement_code, definition.entitlement_class, EntitlementStatus.ACTIVE,
                order.order_id, source_subscription_id, self.clock(), self.clock(),
                provenance=order.access_source,
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
        event = CommercialEvent(
            self.id_factory("commercial_event"), event_type, subject.tenant_id,
            subject.user_id, subject.company_id, self.clock(), "businessbuilder.commercial",
            f"commercial:{getattr(subject, 'order_id', getattr(subject, 'subscription_id', 'event'))}",
            None, payload,
        )
        self.repository.enqueue_outbox(event, event.event_id)

    def _emit_from_billing(self, event_type: str, event: NormalizedBillingEvent, payload: dict) -> None:
        outgoing = CommercialEvent(
            self.id_factory("commercial_event"), event_type, event.tenant_id,
            event.user_id, event.company_id, self.clock(), "businessbuilder.commercial",
            event.correlation_id, event.event_id, payload,
        )
        self.repository.enqueue_outbox(outgoing, outgoing.event_id)
