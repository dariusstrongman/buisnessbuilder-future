from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import os
from uuid import uuid4

import pytest

from businessbuilder.commercial.paid_pilot_release import GateKind, GateStatus, PaidPilotReleaseGate, ReleaseStatus
from businessbuilder.postgres import PostgresCommercialRepository
from tests.commercial.test_paid_pilot_release_gate import authorized
from tests.commercial.test_supervised_checkout import NOW, ready_order, setup


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
