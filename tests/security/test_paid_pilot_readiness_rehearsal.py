from __future__ import annotations

from copy import deepcopy
import json

import pytest

from businessbuilder._serialization import decode_record, encode_record
from businessbuilder.commercial.paid_pilot_release import GateKind, GateRecord, GateStatus
from businessbuilder.commercial.repository import _COMMERCIAL_TYPES
from tests.commercial.test_supervised_checkout import NOW
from scripts.paid_pilot_readiness_rehearsal import (
    COGNITO_NAMES, MATRIX, STRIPE_NAMES, rehearse, source_rollback_rehearsal,
)


def matrix():
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def test_exact_nine_gate_hold_matrix_and_no_live_charge():
    report = rehearse(matrix(), environment={})
    assert report["decision"] == "HOLD"
    assert report["live_charge_allowed"] is False
    assert {row["kind"] for row in report["gates"]} == {kind.value for kind in GateKind}
    assert all(row["status"] == "HOLD" for row in report["gates"])
    assert report["stripe_missing_environment_names"] == list(STRIPE_NAMES)
    assert report["cognito_missing_environment_names"] == list(COGNITO_NAMES)


def test_environment_names_alone_never_become_approval():
    present = {name: "uninspected-value" for name in STRIPE_NAMES + COGNITO_NAMES}
    report = rehearse(matrix(), environment=present)
    assert report["stripe_missing_environment_names"] == []
    assert report["cognito_missing_environment_names"] == []
    assert report["live_charge_allowed"] is False
    assert report["decision"] == "HOLD"


@pytest.mark.parametrize("tampering", [
    lambda data: data["gates"].pop(),
    lambda data: data["gates"][0].update(status="APPROVED"),
    lambda data: data["gates"][0].update(kind="invented_tenth_gate"),
    lambda data: data["gates"][0].update(evidence_refs=["missing-evidence.txt"]),
    lambda data: data["gates"][0].update(evidence_refs=["sk_live_" + "x" * 20]),
    lambda data: data.update(source_checkpoint="different_checkpoint"),
])
def test_missing_or_forged_rehearsal_evidence_fails_closed(tampering):
    forged = deepcopy(matrix())
    tampering(forged)
    with pytest.raises(ValueError):
        rehearse(forged, environment={})


def test_incompatible_rollback_revisions_are_explicitly_excluded():
    rollback = source_rollback_rehearsal()
    assert rollback["old_revision_7_excluded"]
    assert rollback["pilot_revision_12_excluded_after_gate_rows"]
    assert rollback["compatible_rollback_image_drill_completed"] is False
    record = GateRecord("tenant_rehearsal", "company_rehearsal", "order_rehearsal",
        GateKind.FTC_LEGAL, GateStatus.HOLD, "qualified_counsel", None,
        None, None, None, True, NOW, 1, "external_legal_decision_absent")
    earlier_codec = {name: value for name, value in _COMMERCIAL_TYPES.items()
                     if name not in {"GateRecord", "GateKind", "GateStatus", "ReleaseStatus", "FirstCustomerPacket"}}
    with pytest.raises(KeyError, match="GateRecord"):
        decode_record(encode_record(record), earlier_codec)
