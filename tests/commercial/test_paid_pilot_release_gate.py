from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from businessbuilder.commercial.paid_pilot_release import (
    GateKind, GateStatus, PaidPilotReleaseGate, ReleaseStatus,
)
from businessbuilder.commercial.models import PaymentEligibility, TaxDisposition
from businessbuilder.commercial.repository import CommercialConflict, SQLiteCommercialRepository
from tests.commercial.test_supervised_checkout import NOW, ready_order, setup


def gate(repo, now=lambda: NOW, verifier=None, packet_verifier=None):
    return PaidPilotReleaseGate(repo, clock=now, authority_verifier=verifier,
        packet_evidence_verifier=packet_verifier)


def authorized(actor, kind, tenant, company, order):
    return actor == "trusted-reviewer" and tenant.startswith("tenant_") and company == "company_checkout"


def approve_all(release, order):
    for kind in GateKind:
        release.record_gate(tenant_id=order.tenant_id, company_id=order.company_id,
            order_id=order.order_id, kind=kind, status=GateStatus.APPROVED,
            owner=f"owner:{kind.value}", actor="trusted-reviewer",
            evidence_ref=f"external-review:{kind.value}", review_at=NOW + timedelta(days=30))


def test_missing_expired_and_held_approvals_fail_closed():
    context, repo, service = setup()
    order = ready_order(context, repo, service)
    clock = [NOW]
    release = gate(repo, lambda: clock[0], authorized)
    assert release.status(order.tenant_id, order.company_id, order.order_id) == (
        ReleaseStatus.NOT_READY, tuple(GateKind))
    with pytest.raises(CommercialConflict):
        release.require_live_charge(order)
    approve_all(release, order)
    assert release.status(order.tenant_id, order.company_id, order.order_id)[0] is ReleaseStatus.READY_FOR_SUPERVISED_PILOT
    with pytest.raises(CommercialConflict, match="test-mode"):
        release.approve(tenant_id=order.tenant_id, company_id=order.company_id,
            order_id=order.order_id, actor="trusted-reviewer", terms_version="terms-v1",
            terms_acceptance_ref="accepted:terms-v1",
            refund_terms_ref="refund-v1", cancellation_terms_ref="cancel-v1",
            operator_id="operator-1", monitoring_owner="monitor-1", rollback_owner="rollback-1",
            support_contact_ref="support-1", expires_at=NOW + timedelta(days=1))
    release.record_gate(tenant_id=order.tenant_id, company_id=order.company_id,
        order_id=order.order_id, kind=GateKind.PRODUCTION_STRIPE, status=GateStatus.HOLD,
        owner="stripe-owner", actor="trusted-reviewer", evidence_ref="hold:stripe",
        review_at=NOW + timedelta(days=1), reason_code="webhook_unready")
    assert release.status(order.tenant_id, order.company_id, order.order_id)[0] is ReleaseStatus.HOLD
    clock[0] = NOW + timedelta(days=31)
    assert release.status(order.tenant_id, order.company_id, order.order_id)[0] is ReleaseStatus.HOLD
    assert GateKind.FTC_LEGAL in release.status(order.tenant_id, order.company_id, order.order_id)[1]


def test_forged_review_nonblocking_bypass_missing_evidence_and_cross_tenant_denied():
    context, repo, service = setup()
    order = ready_order(context, repo, service)
    release = gate(repo, verifier=authorized)
    arguments = dict(tenant_id=order.tenant_id, company_id=order.company_id,
                     order_id=order.order_id, kind=GateKind.FTC_LEGAL,
                     status=GateStatus.APPROVED, owner="counsel", actor="browser-founder",
                     evidence_ref="external:legal", review_at=NOW + timedelta(days=1))
    with pytest.raises(PermissionError):
        release.record_gate(**arguments)
    arguments["actor"] = "trusted-reviewer"
    with pytest.raises(CommercialConflict, match="always blocking"):
        release.record_gate(**arguments, blocking=False)
    with pytest.raises(CommercialConflict):
        release.record_gate(**{**arguments, "evidence_ref": None})
    with pytest.raises(CommercialConflict, match="opaque evidence reference"):
        release.record_gate(**{**arguments, "evidence_ref": "sk_live_" + "x" * 20})
    with pytest.raises(LookupError):
        release.record_gate(**{**arguments, "tenant_id": "tenant_other"})
    assert release.status(order.tenant_id, order.company_id, order.order_id)[0] is ReleaseStatus.NOT_READY


def test_append_only_retry_history_and_restart_with_existing_ledger(tmp_path):
    context, _, service = setup()
    # The test order is copied into the disposable SQLite commercial ledger.
    repo = SQLiteCommercialRepository(str(tmp_path / "commercial.sqlite"))
    for version in service.repository.product_versions.values():
        product = service.repository.products[version.product_code.value]
        repo.save_product(product, version)
    order = ready_order(context, service.repository, service)
    for item in service.repository.order_history(order.tenant_id, order.company_id, order.order_id):
        repo.append_order(item)
    release = gate(repo, verifier=authorized)
    release.record_gate(tenant_id=order.tenant_id, company_id=order.company_id,
        order_id=order.order_id, kind=GateKind.CUSTOMER_TERMS, status=GateStatus.PENDING,
        owner="terms-owner", actor="trusted-reviewer", evidence_ref="draft:terms", review_at=None)
    release.record_gate(tenant_id=order.tenant_id, company_id=order.company_id,
        order_id=order.order_id, kind=GateKind.CUSTOMER_TERMS, status=GateStatus.APPROVED,
        owner="terms-owner", actor="trusted-reviewer", evidence_ref="reviewed:terms",
        review_at=NOW + timedelta(days=1))
    assert len(repo.gate_history(order.tenant_id, order.company_id, order.order_id, GateKind.CUSTOMER_TERMS)) == 2
    repo.connection.close()
    restarted = SQLiteCommercialRepository(str(tmp_path / "commercial.sqlite"))
    assert len(restarted.gate_history(order.tenant_id, order.company_id, order.order_id, GateKind.CUSTOMER_TERMS)) == 2
    assert not restarted.gate_history("tenant_other", order.company_id, order.order_id, GateKind.CUSTOMER_TERMS)
    assert gate(restarted).status(order.tenant_id, order.company_id, order.order_id)[0] is ReleaseStatus.NOT_READY
    restarted.connection.close()


def test_exact_packet_requires_separate_ceremony_and_is_revoked_by_hold_expiry_or_order_change():
    context, repo, service = setup()
    order = ready_order(context, repo, service)
    old = repo.get_admission(order.tenant_id, order.company_id, order.admission_id)
    production_rehearsal = replace(old, admission_id="admission_production_rehearsal",
                                   test_only=False)
    repo.append_admission(production_rehearsal)
    order = replace(order, admission_id=production_rehearsal.admission_id,
                    version=order.version + 1)
    repo.append_order(order)
    clock = [NOW]
    release = gate(repo, lambda: clock[0], authorized,
        lambda packet: packet.terms_acceptance_ref == "accepted:terms-v1")
    approve_all(release, order)
    assert release.status(order.tenant_id, order.company_id, order.order_id)[0] is ReleaseStatus.READY_FOR_SUPERVISED_PILOT
    with pytest.raises(PermissionError):
        release.approve(tenant_id=order.tenant_id, company_id=order.company_id,
            order_id=order.order_id, actor="founder", terms_version="terms-v1",
            terms_acceptance_ref="accepted:terms-v1",
            refund_terms_ref="refund-v1", cancellation_terms_ref="cancel-v1",
            operator_id="operator-1", monitoring_owner="monitor-1", rollback_owner="rollback-1",
            support_contact_ref="support-1", expires_at=NOW + timedelta(hours=1))
    packet = release.approve(tenant_id=order.tenant_id, company_id=order.company_id,
        order_id=order.order_id, actor="trusted-reviewer", terms_version="terms-v1",
        terms_acceptance_ref="accepted:terms-v1",
        refund_terms_ref="refund-v1", cancellation_terms_ref="cancel-v1",
        operator_id="operator-1", monitoring_owner="monitor-1", rollback_owner="rollback-1",
        support_contact_ref="support-1", expires_at=NOW + timedelta(hours=1))
    assert packet.price_minor_units == order.total.minor_units
    assert release.require_live_charge(order) == packet
    with pytest.raises(CommercialConflict):
        release.approve(tenant_id=order.tenant_id, company_id=order.company_id,
            order_id=order.order_id, actor="trusted-reviewer", terms_version="terms-v1",
            terms_acceptance_ref="accepted:terms-v1",
            refund_terms_ref="refund-v1", cancellation_terms_ref="cancel-v1",
            operator_id="operator-1", monitoring_owner="monitor-1", rollback_owner="rollback-1",
            support_contact_ref="support-1", expires_at=NOW + timedelta(hours=1))
    clock[0] = NOW + timedelta(hours=2)
    assert release.status(order.tenant_id, order.company_id, order.order_id)[0] is ReleaseStatus.READY_FOR_SUPERVISED_PILOT
    with pytest.raises(CommercialConflict):
        release.require_live_charge(order)
    clock[0] = NOW
    repo.append_order(replace(order, version=order.version + 1, offer_code="changed_offer"))
    assert release.status(order.tenant_id, order.company_id, order.order_id)[0] is ReleaseStatus.READY_FOR_SUPERVISED_PILOT
    with pytest.raises(CommercialConflict):
        release.require_live_charge(repo.get_order(order.tenant_id, order.company_id, order.order_id))


@pytest.mark.parametrize("change", [
    {"eligibility": PaymentEligibility.PAYMENT_DELAY_REQUIRED},
    {"tax_disposition": TaxDisposition.MANUAL_REVIEW},
])
def test_payment_delay_and_tax_review_cannot_be_overridden_by_green_gate_records(change):
    context, repo, service = setup()
    order = ready_order(context, repo, service)
    old = repo.get_admission(order.tenant_id, order.company_id, order.admission_id)
    repo.append_admission(replace(old, admission_id="admission_rehearsal", test_only=False))
    order = replace(order, admission_id="admission_rehearsal", version=order.version + 1, **change)
    repo.append_order(order)
    release = gate(repo, verifier=authorized)
    approve_all(release, order)
    with pytest.raises(CommercialConflict):
        release.approve(tenant_id=order.tenant_id, company_id=order.company_id,
            order_id=order.order_id, actor="trusted-reviewer", terms_version="terms-v1",
            terms_acceptance_ref="accepted:terms-v1",
            refund_terms_ref="refund-v1", cancellation_terms_ref="cancel-v1",
            operator_id="operator-1", monitoring_owner="monitor-1", rollback_owner="rollback-1",
            support_contact_ref="support-1", expires_at=NOW + timedelta(hours=1))
