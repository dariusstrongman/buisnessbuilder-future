from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
import os
from uuid import uuid4

import pytest

from businessbuilder.commercial.paid_pilot_release import GateKind, GateStatus, PaidPilotReleaseGate, ReleaseStatus
from businessbuilder.postgres import PostgresCommercialRepository
from tests.commercial.test_paid_pilot_release_gate import authorized
from tests.commercial.test_supervised_checkout import NOW, ready_order, setup
from scripts.paid_pilot_readiness_rehearsal import MATRIX


@pytest.mark.skipif(not os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"), reason="isolated PostgreSQL required")
def test_gate_history_rollback_restart_and_tenant_isolation():
    dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
    schema = f"bb_release_{uuid4().hex[:12]}"
    context, memory, service = setup()
    order = ready_order(context, memory, service)
    repo = PostgresCommercialRepository(dsn, schema=schema)
    for version in memory.product_versions.values():
        repo.save_product(memory.products[version.product_code.value], version)
    for version in memory.order_history(order.tenant_id, order.company_id, order.order_id):
        repo.append_order(version)
    release = PaidPilotReleaseGate(repo, clock=lambda: NOW, authority_verifier=authorized)
    with pytest.raises(RuntimeError, match="simulated rollback"):
        with repo.transaction():
            release.record_gate(tenant_id=order.tenant_id, company_id=order.company_id,
                order_id=order.order_id, kind=GateKind.FTC_LEGAL, status=GateStatus.APPROVED,
                owner="counsel", actor="trusted-reviewer", evidence_ref="review:legal",
                review_at=NOW + timedelta(days=1))
            raise RuntimeError("simulated rollback")
    assert not repo.gate_history(order.tenant_id, order.company_id, order.order_id, GateKind.FTC_LEGAL)
    record = release.record_gate(tenant_id=order.tenant_id, company_id=order.company_id,
        order_id=order.order_id, kind=GateKind.FTC_LEGAL, status=GateStatus.APPROVED,
        owner="counsel", actor="trusted-reviewer", evidence_ref="review:legal",
        review_at=NOW + timedelta(days=1))
    assert record.version == 1
    repo.close()
    restarted = PostgresCommercialRepository(dsn, schema=schema)
    assert restarted.gate_history(order.tenant_id, order.company_id, order.order_id, GateKind.FTC_LEGAL) == (record,)
    assert not restarted.gate_history("tenant_other", order.company_id, order.order_id, GateKind.FTC_LEGAL)
    assert PaidPilotReleaseGate(restarted, clock=lambda: NOW).status(
        order.tenant_id, order.company_id, order.order_id)[0] is ReleaseStatus.NOT_READY
    competing = PostgresCommercialRepository(dsn, schema=schema)
    changed = replace(order, version=order.version + 1, offer_code="revised_scope")
    competing.append_order(changed)
    assert restarted.get_order(order.tenant_id, order.company_id, order.order_id) == changed
    competing.close()
    restarted.close()


@pytest.mark.skipif(not os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"), reason="isolated PostgreSQL required")
def test_all_nine_holds_are_durable_and_cannot_issue_release_packet_after_restart():
    dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
    schema = f"bb_rehearsal_{uuid4().hex[:12]}"
    context, memory, service = setup()
    order = ready_order(context, memory, service)
    repo = PostgresCommercialRepository(dsn, schema=schema)
    for version in memory.product_versions.values():
        repo.save_product(memory.products[version.product_code.value], version)
    for version in memory.order_history(order.tenant_id, order.company_id, order.order_id):
        repo.append_order(version)
    gate = PaidPilotReleaseGate(repo, clock=lambda: NOW,
        authority_verifier=lambda actor, kind, tenant, company, scoped_order:
            actor == "rehearsal-hold-recorder" and tenant == order.tenant_id
            and company == order.company_id and scoped_order == order.order_id)
    rows = json.loads(MATRIX.read_text(encoding="utf-8"))["gates"]
    for row in rows:
        gate.record_gate(tenant_id=order.tenant_id, company_id=order.company_id,
            order_id=order.order_id, kind=GateKind(row["kind"]), status=GateStatus.HOLD,
            owner=row["owner_role"], actor="rehearsal-hold-recorder",
            evidence_ref=row["evidence_refs"][0] if row["evidence_refs"] else None,
            review_at=None, reason_code=row["reason"])
    assert gate.status(order.tenant_id, order.company_id, order.order_id)[0] is ReleaseStatus.HOLD
    repo.close()
    restarted = PostgresCommercialRepository(dsn, schema=schema)
    resumed = PaidPilotReleaseGate(restarted, clock=lambda: NOW)
    assert resumed.status(order.tenant_id, order.company_id, order.order_id)[0] is ReleaseStatus.HOLD
    for kind in GateKind:
        assert len(restarted.gate_history(order.tenant_id, order.company_id, order.order_id, kind)) == 1
        assert not restarted.gate_history("tenant_other", order.company_id, order.order_id, kind)
    with pytest.raises(PermissionError):
        resumed.approve(tenant_id=order.tenant_id, company_id=order.company_id,
            order_id=order.order_id, actor="browser-founder", terms_version="terms-v1",
            terms_acceptance_ref="accepted:terms-v1", refund_terms_ref="refund-v1",
            cancellation_terms_ref="cancel-v1", operator_id="operator-1",
            monitoring_owner="monitor-1", rollback_owner="rollback-1",
            support_contact_ref="support-1", expires_at=NOW + timedelta(hours=1))
    assert restarted.get_release_packet(order.tenant_id, order.company_id, order.order_id) is None
    restarted.close()
