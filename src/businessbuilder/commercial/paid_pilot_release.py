"""Order-scoped, append-only first paid-pilot release ceremony.

This is a safety boundary, not a legal or tax decision engine. No external
approval is inferred from configuration, and there is no customer mutation API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import re
from typing import Callable, TYPE_CHECKING

from .models import Order, PaymentEligibility, TaxDisposition
if TYPE_CHECKING:
    from .repository import CommercialRepository


class GateKind(str, Enum):
    FTC_LEGAL = "ftc_business_opportunity_legal"
    TEXAS_TAX = "texas_tax_package_classification"
    PRODUCTION_STRIPE = "production_stripe"
    PRODUCTION_COGNITO = "production_cognito"
    OPERATOR_PROVISIONING = "operator_provisioning"
    EVIDENCE_PRIVACY_RETENTION = "evidence_privacy_retention"
    MONITORING_ALERTS = "monitoring_alert_ownership"
    ROLLBACK_MIGRATION = "rollback_migration"
    CUSTOMER_TERMS = "customer_terms_refund_cancellation"


class GateStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    HOLD = "HOLD"


class ReleaseStatus(str, Enum):
    NOT_READY = "NOT_READY"
    READY_FOR_SUPERVISED_PILOT = "READY_FOR_SUPERVISED_PILOT"
    HOLD = "HOLD"
    APPROVED_FOR_LIVE_CHARGE = "APPROVED_FOR_LIVE_CHARGE"


@dataclass(frozen=True, slots=True)
class GateRecord:
    tenant_id: str
    company_id: str
    order_id: str
    kind: GateKind
    status: GateStatus
    owner: str
    evidence_ref: str | None
    approved_by: str | None
    approved_at: datetime | None
    review_at: datetime | None
    blocking: bool
    recorded_at: datetime
    version: int
    reason_code: str

    def current(self, at: datetime) -> bool:
        return (self.status is GateStatus.APPROVED and self.approved_at is not None
                and self.approved_at <= at and self.review_at is not None and at < self.review_at
                and bool(self.evidence_ref) and bool(self.approved_by))


@dataclass(frozen=True, slots=True)
class FirstCustomerPacket:
    tenant_id: str
    company_id: str
    order_id: str
    order_digest: str
    offer_code: str
    price_minor_units: int
    currency: str
    tax_state: str
    legal_ref: str
    legal_state: str
    terms_version: str
    terms_acceptance_ref: str
    refund_terms_ref: str
    cancellation_terms_ref: str
    operator_id: str
    monitoring_owner: str
    rollback_owner: str
    support_contact_ref: str
    eligibility: str
    release_decision: ReleaseStatus
    decided_by: str
    decided_at: datetime
    expires_at: datetime
    version: int


class PaidPilotReleaseGate:
    """Server-side authority verifier is mandatory for every gate mutation.

    Verifier must authenticate the named external reviewer/operator, their
    jurisdiction and role, and the exact order scope. It defaults to deny.
    """

    def __init__(self, repository: CommercialRepository, *, clock: Callable[[], datetime],
                 authority_verifier: Callable[[str, GateKind | None, str, str, str], bool] | None = None,
                 packet_evidence_verifier: Callable[[FirstCustomerPacket], bool] | None = None) -> None:
        self.repository = repository
        self.clock = clock
        self.authority_verifier = authority_verifier
        self.packet_evidence_verifier = packet_evidence_verifier

    @staticmethod
    def digest(order: Order) -> str:
        data = (order.tenant_id, order.company_id, order.order_id, order.user_id,
                order.offer_code, order.quote_id, order.total.currency if order.total else None,
                order.total.minor_units if order.total else None, order.admission_id,
                order.eligibility.value, order.tax_disposition.value,
                order.existing_audit_ref, order.existing_recommendation_digest,
                tuple((item.order_item_id, item.product_code.value, item.product_version_id,
                       item.billing_mode.value, item.quantity,
                       item.unit_amount.currency if item.unit_amount else None,
                       item.unit_amount.minor_units if item.unit_amount else None)
                      for item in order.items))
        return sha256(repr(data).encode()).hexdigest()

    def _authorized(self, actor: str, kind: GateKind | None, tenant: str, company: str, order: str) -> None:
        if not actor or self.authority_verifier is None or not self.authority_verifier(actor, kind, tenant, company, order):
            raise PermissionError("paid-pilot release authority required")

    @staticmethod
    def _safe_ref(value: str | None) -> bool:
        return bool(value and len(value) <= 256 and
                    re.fullmatch(r"[A-Za-z0-9._:/-]+", value) and
                    not re.search(r"sk_(?:test|live)_|whsec_|AKIA[0-9A-Z]{16}", value))

    def record_gate(self, *, tenant_id: str, company_id: str, order_id: str,
                    kind: GateKind, status: GateStatus, owner: str, actor: str,
                    evidence_ref: str | None, review_at: datetime | None,
                    blocking: bool = True, reason_code: str = "review") -> GateRecord:
        self._authorized(actor, kind, tenant_id, company_id, order_id)
        now = self.clock()
        if not owner or len(owner) > 128 or not reason_code or len(reason_code) > 80:
            raise CommercialConflict("owner and reason required")
        if not blocking:
            raise CommercialConflict("required paid-pilot approvals are always blocking")
        if status is GateStatus.APPROVED and (not evidence_ref or len(evidence_ref) > 256
                                               or review_at is None or review_at <= now):
            raise CommercialConflict("current external approval evidence and review date required")
        if evidence_ref is not None and not self._safe_ref(evidence_ref):
            raise CommercialConflict("safe opaque evidence reference required")
        with self.repository.transaction():
            self.repository.get_order(tenant_id, company_id, order_id)
            history = self.repository.gate_history(tenant_id, company_id, order_id, kind)
            record = GateRecord(tenant_id, company_id, order_id, kind, status, owner, evidence_ref,
                                actor if status is GateStatus.APPROVED else None,
                                now if status is GateStatus.APPROVED else None, review_at,
                                blocking, now, len(history) + 1, reason_code)
            self.repository.append_release_gate(record)
            return record

    def status(self, tenant_id: str, company_id: str, order_id: str) -> tuple[ReleaseStatus, tuple[GateKind, ...]]:
        self.repository.get_order(tenant_id, company_id, order_id)
        now = self.clock()
        missing: list[GateKind] = []
        held = False
        for kind in GateKind:
            history = self.repository.gate_history(tenant_id, company_id, order_id, kind)
            current = history[-1] if history else None
            if current is None or (current.blocking and not current.current(now)):
                missing.append(kind)
            if current is not None and current.status is GateStatus.HOLD:
                held = True
        if held:
            return ReleaseStatus.HOLD, tuple(missing)
        if missing:
            return ReleaseStatus.NOT_READY, tuple(missing)
        packet = self.repository.get_release_packet(tenant_id, company_id, order_id)
        order = self.repository.get_order(tenant_id, company_id, order_id)
        if (packet and packet.expires_at > now and packet.order_digest == self.digest(order)
            and self.packet_evidence_verifier is not None
            and self.packet_evidence_verifier(packet)):
            return ReleaseStatus.APPROVED_FOR_LIVE_CHARGE, ()
        return ReleaseStatus.READY_FOR_SUPERVISED_PILOT, ()

    def approve(self, *, tenant_id: str, company_id: str, order_id: str, actor: str,
                terms_version: str, terms_acceptance_ref: str,
                refund_terms_ref: str, cancellation_terms_ref: str,
                operator_id: str, monitoring_owner: str, rollback_owner: str,
                support_contact_ref: str, expires_at: datetime) -> FirstCustomerPacket:
        self._authorized(actor, None, tenant_id, company_id, order_id)
        with self.repository.transaction():
            state, missing = self.status(tenant_id, company_id, order_id)
            if state is not ReleaseStatus.READY_FOR_SUPERVISED_PILOT or missing:
                raise CommercialConflict("blocking paid-pilot gates not green")
            order = self.repository.get_order(tenant_id, company_id, order_id)
            now = self.clock()
            if (expires_at <= now or not order.offer_code or order.total is None
                or order.eligibility is not PaymentEligibility.PAY_NOW_ELIGIBLE
                or order.tax_disposition in {TaxDisposition.MANUAL_REVIEW, TaxDisposition.TEST_MODE_UNDETERMINED}
                or not order.admission_id):
                raise CommercialConflict("live payment eligibility, tax and exact order required")
            admission = self.repository.get_admission(tenant_id, company_id, order.admission_id)
            if admission.test_only or admission.order_digest is None:
                raise CommercialConflict("test-mode commercial admission cannot authorize live charge")
            fields = (terms_version, terms_acceptance_ref, refund_terms_ref, cancellation_terms_ref, operator_id,
                      monitoring_owner, rollback_owner, support_contact_ref)
            if any(not self._safe_ref(value) for value in fields):
                raise CommercialConflict("complete first-customer packet required")
            legal = self.repository.gate_history(tenant_id, company_id, order_id, GateKind.FTC_LEGAL)[-1]
            packet = FirstCustomerPacket(
                tenant_id, company_id, order_id, self.digest(order), order.offer_code,
                order.total.minor_units, order.total.currency, order.tax_disposition.value,
                legal.evidence_ref or "", "approved_current", terms_version, terms_acceptance_ref,
                refund_terms_ref,
                cancellation_terms_ref, operator_id, monitoring_owner, rollback_owner,
                support_contact_ref, order.eligibility.value,
                ReleaseStatus.APPROVED_FOR_LIVE_CHARGE, actor, now, expires_at, 1,
            )
            if self.packet_evidence_verifier is None or not self.packet_evidence_verifier(packet):
                raise CommercialConflict("founder terms acceptance and packet evidence not verified")
            self.repository.append_release_packet(packet)
            return packet

    def require_live_charge(self, order: Order) -> FirstCustomerPacket:
        if not order.admission_id or self.repository.get_admission(
            order.tenant_id, order.company_id, order.admission_id).test_only:
            raise CommercialConflict("test or missing admission cannot authorize live charge")
        state, _ = self.status(order.tenant_id, order.company_id, order.order_id)
        if state is not ReleaseStatus.APPROVED_FOR_LIVE_CHARGE:
            raise CommercialConflict("live charging blocked by supervised paid-pilot release gate")
        packet = self.repository.get_release_packet(order.tenant_id, order.company_id, order.order_id)
        if packet is None or packet.order_digest != self.digest(order):
            raise CommercialConflict("first-customer release packet is stale")
        return packet


from .repository import CommercialConflict  # noqa: E402 - after model definitions for codec registration
