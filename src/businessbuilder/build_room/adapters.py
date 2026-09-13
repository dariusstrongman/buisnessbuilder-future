from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from businessbuilder.integration.contracts import tenant_v2_projection

from .projection import ProjectionError, ScopedEnvelope


APPROVAL_STATES = frozenset({"requested", "granted", "denied", "revoked", "expired", "superseded"})
FOUNDER_ACTION_STATES = frozenset({"required", "in_progress", "submitted", "verified", "declined", "expired", "waived_noncritical"})
VERIFICATION_STATES = frozenset({"proposed", "executed", "tested", "verified", "failed", "expired"})
BLOCKER_SEVERITIES = frozenset({"informational", "noncritical", "critical"})

CONTRACT_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "job.v2": (
        frozenset({"schema_version", "tenant_id", "job_id", "company_id", "capability_id", "objective", "status", "idempotency_key", "input_artifact_refs", "required_approval_refs", "budget_ref", "created_at", "updated_at", "version"}),
        frozenset({"schema_version", "tenant_id", "job_id", "company_id", "capability_id", "objective", "status", "idempotency_key", "input_artifact_refs", "output_artifact_refs", "required_approval_refs", "budget_ref", "provider_ref", "failure", "created_at", "updated_at", "version"}),
    ),
    "verification.v2": (
        frozenset({"schema_version", "tenant_id", "verification_id", "company_id", "target_ref", "state", "method", "evidence_refs", "verified_scope", "retest_on", "criticality", "updated_at", "version"}),
        frozenset({"schema_version", "tenant_id", "verification_id", "company_id", "target_ref", "state", "method", "evidence_refs", "verified_scope", "failure_code", "expires_at", "retest_on", "criticality", "updated_at", "version"}),
    ),
    "approval.v2": (
        frozenset({"schema_version", "tenant_id", "approval_id", "company_id", "approval_type", "subject_ref", "subject_digest", "requested_by", "required_approver_role", "state", "requested_at", "version"}),
        frozenset({"schema_version", "tenant_id", "approval_id", "company_id", "approval_type", "subject_ref", "subject_digest", "requested_by", "required_approver_role", "state", "decision_by", "decision_reason", "requested_at", "decided_at", "expires_at", "version"}),
    ),
    "founder-action.v2": (
        frozenset({"schema_version", "tenant_id", "founder_action_id", "company_id", "action_type", "title", "reason", "instructions", "risk", "irreversible", "state", "required_evidence_kinds", "created_at", "version"}),
        frozenset({"schema_version", "tenant_id", "founder_action_id", "company_id", "action_type", "title", "reason", "instructions", "risk", "irreversible", "state", "required_evidence_kinds", "evidence_refs", "blocks", "due_at", "created_at", "version"}),
    ),
    "event.v2": (
        frozenset({"schema_version", "tenant_id", "event_id", "company_id", "event_type", "occurred_at", "recorded_at", "producer", "sequence", "correlation_id", "causation_id", "payload", "payload_digest"}),
        frozenset({"schema_version", "tenant_id", "event_id", "company_id", "event_type", "occurred_at", "recorded_at", "producer", "sequence", "correlation_id", "causation_id", "subject_ref", "payload", "payload_digest", "visibility"}),
    ),
    "budget-spend.v2": (
        frozenset({"schema_version", "tenant_id", "budget_id", "company_id", "scope_ref", "ceiling", "reserved", "settled", "pass_through", "state", "period_start", "period_end", "version"}),
        frozenset({"schema_version", "tenant_id", "budget_id", "company_id", "scope_ref", "ceiling", "reserved", "settled", "pass_through", "state", "overage_policy", "period_start", "period_end", "approval_ref", "version"}),
    ),
}


def _mapping(record: Any, schema: str) -> dict[str, Any]:
    if isinstance(record, Mapping):
        value = dict(record)
    elif hasattr(record, "to_contract") and hasattr(record, "tenant_id"):
        value = tenant_v2_projection(record.to_contract(), record.tenant_id)
    else:
        raise ProjectionError(f"{schema} adapter requires a v2 mapping or supported domain record")
    if value.get("schema_version") != schema:
        raise ProjectionError(f"{schema} record is required")
    required, allowed = CONTRACT_FIELDS[schema]
    missing, extra = required - set(value), set(value) - allowed
    if missing:
        raise ProjectionError(f"{schema} missing required fields: {', '.join(sorted(missing))}")
    if extra:
        raise ProjectionError(f"{schema} has unexpected fields: {', '.join(sorted(extra))}")
    return value


def _required(value: Mapping[str, Any], fields: tuple[str, ...], kind: str) -> None:
    missing = [field for field in fields if field not in value]
    if missing:
        raise ProjectionError(f"{kind} missing required fields: {', '.join(missing)}")


def _scope(value: Mapping[str, Any], kind: str) -> tuple[str, str]:
    tenant_id, company_id = value.get("tenant_id"), value.get("company_id")
    if not isinstance(tenant_id, str) or not tenant_id or not isinstance(company_id, str) or not company_id:
        raise ProjectionError(f"{kind} requires tenant_id and company_id")
    return tenant_id, company_id


def _minor(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProjectionError(f"{field} must be a nonnegative exact integer")
    return value


def _ref_text(value: Mapping[str, Any]) -> str:
    _required(value, ("type", "id"), "reference")
    return f"{value['type']}:{value['id']}"


def _iso(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectionError(f"{field} must be an ISO timestamp or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectionError(f"{field} must be an ISO timestamp or null") from exc
    if parsed.tzinfo is None:
        raise ProjectionError(f"{field} must include a timezone")
    return value


def adapt_job(
    record: Any, *, title: str, owner: str, dependency_ids: tuple[str, ...] = (),
    cost_minor: int = 0, evidence_refs: tuple[str, ...] = (), detail: str = "", order: int = 999,
) -> dict[str, Any]:
    value = _mapping(record, "job.v2")
    _required(value, ("job_id", "company_id", "tenant_id", "objective", "status", "budget_ref", "version"), "job")
    tenant_id, company_id = _scope(value, "job")
    return {
        "tenant_id": tenant_id, "company_id": company_id, "id": value["job_id"], "kind": "job",
        "title": title, "description": value["objective"], "owner": owner, "status": value["status"],
        "dependency_ids": list(dependency_ids), "cost_minor": _minor(cost_minor, "job cost_minor"),
        "evidence_refs": list(evidence_refs), "detail": detail, "order": _minor(order, "job order"),
    }


def adapt_verification(record: Any, *, title: str, owner: str, order: int = 999, cost_minor: int = 0) -> dict[str, Any]:
    value = _mapping(record, "verification.v2")
    _required(value, ("verification_id", "company_id", "tenant_id", "state", "method", "evidence_refs", "verified_scope", "retest_on"), "verification")
    tenant_id, company_id = _scope(value, "verification")
    if value["state"] not in VERIFICATION_STATES:
        raise ProjectionError("verification state is invalid")
    evidence_refs = tuple(_ref_text(item) for item in value["evidence_refs"])
    if value["state"] in {"tested", "verified"} and not evidence_refs:
        raise ProjectionError("tested or verified work requires evidence")
    detail = value["verified_scope"]
    if value.get("expires_at"):
        detail = f"{detail} Evidence expires {value['expires_at']}."
    return {
        "tenant_id": tenant_id, "company_id": company_id, "id": value["verification_id"], "kind": "verification",
        "title": title, "description": value["verified_scope"], "owner": owner, "status": value["state"],
        "dependency_ids": list(value["retest_on"]), "cost_minor": _minor(cost_minor, "verification cost_minor"),
        "evidence_refs": list(evidence_refs), "detail": detail, "order": _minor(order, "verification order"),
    }


def adapt_approval(record: Mapping[str, Any], *, title: str, summary: str) -> dict[str, Any]:
    value = _mapping(record, "approval.v2")
    _required(value, ("approval_id", "tenant_id", "company_id", "state", "subject_digest", "subject_ref", "requested_by", "required_approver_role", "requested_at", "version"), "approval")
    tenant_id, company_id = _scope(value, "approval")
    if value["state"] not in APPROVAL_STATES:
        raise ProjectionError("approval state is invalid")
    decided_at = _iso(value.get("decided_at"), "approval decided_at")
    if value["state"] == "requested" and decided_at is not None:
        raise ProjectionError("requested approval cannot have a decision timestamp")
    if value["state"] in {"granted", "denied", "revoked"} and decided_at is None:
        raise ProjectionError("approval decisions require a decision timestamp")
    return {
        "tenant_id": tenant_id, "company_id": company_id, "approval_id": value["approval_id"],
        "title": title, "summary": summary, "state": value["state"], "subject_digest": value["subject_digest"],
        "subject_ref": _ref_text(value["subject_ref"]), "required_approver_role": value["required_approver_role"],
        "requested_at": _iso(value["requested_at"], "approval requested_at"), "decided_at": decided_at,
        "expires_at": _iso(value.get("expires_at"), "approval expires_at"),
    }


def adapt_founder_action(record: Mapping[str, Any], *, order: int = 999) -> dict[str, Any]:
    value = _mapping(record, "founder-action.v2")
    _required(value, ("founder_action_id", "tenant_id", "company_id", "title", "reason", "instructions", "risk", "irreversible", "state", "required_evidence_kinds", "created_at", "version"), "founder action")
    tenant_id, company_id = _scope(value, "founder action")
    if value["state"] not in FOUNDER_ACTION_STATES or not isinstance(value["instructions"], list) or not value["instructions"]:
        raise ProjectionError("founder action state/instructions are invalid")
    return {
        "tenant_id": tenant_id, "company_id": company_id, "founder_action_id": value["founder_action_id"],
        "title": value["title"], "reason": value["reason"], "instructions": list(value["instructions"]),
        "risk": value["risk"], "irreversible": value["irreversible"], "state": value["state"],
        "required_evidence_kinds": list(value["required_evidence_kinds"]), "order": _minor(order, "founder action order"),
    }


def adapt_event(record: Any) -> dict[str, Any]:
    value = _mapping(record, "event.v2")
    _required(value, ("event_id", "tenant_id", "company_id", "event_type", "occurred_at", "recorded_at", "producer", "sequence", "correlation_id", "causation_id", "payload", "payload_digest"), "event")
    tenant_id, company_id = _scope(value, "event")
    payload = value["payload"]
    if not isinstance(payload, Mapping):
        raise ProjectionError("event payload must be an object")
    return {
        "tenant_id": tenant_id, "company_id": company_id, "event_id": value["event_id"],
        "event_type": value["event_type"], "occurred_at": _iso(value["occurred_at"], "event occurred_at"),
        "sequence": _minor(value["sequence"], "event sequence"), "visibility": value.get("visibility", "internal"),
        "label": str(payload.get("customer_label", value["event_type"].replace(".", " ").title())),
        "detail": str(payload.get("customer_detail", "")),
    }


def adapt_evidence(record: Any, *, title: str) -> dict[str, Any]:
    if not isinstance(record, Mapping) and hasattr(record, "to_dict"):
        record = record.to_dict()
    if not isinstance(record, Mapping):
        raise ProjectionError("evidence adapter requires an EvidenceRef domain record or mapping")
    value = dict(record)
    _required(value, ("evidence_id", "tenant_id", "company_id", "evidence_type", "artifact_ref", "captured_at"), "evidence")
    tenant_id, company_id = _scope(value, "evidence")
    evidence_type = value["evidence_type"]
    if isinstance(evidence_type, Enum):
        evidence_type = evidence_type.value
    return {
        "tenant_id": tenant_id, "company_id": company_id, "evidence_id": value["evidence_id"], "title": title,
        "evidence_type": evidence_type, "artifact_ref": value["artifact_ref"],
        "captured_at": _iso(value["captured_at"], "evidence captured_at"),
        "expires_at": _iso(value.get("expires_at"), "evidence expires_at"), "issuer": value.get("issuer"),
        "test_name": value.get("test_name"), "test_passed": value.get("test_passed"),
    }


def adapt_blocker(record: Any) -> dict[str, Any]:
    fields = ("blocker_id", "tenant_id", "company_id", "severity", "affected_target", "reason", "remediation", "owner", "open")
    if isinstance(record, Mapping):
        value = dict(record)
    else:
        if not all(hasattr(record, field) for field in fields):
            raise ProjectionError("blocker adapter requires a Blocker domain record or mapping")
        value = {field: getattr(record, field) for field in fields}
    _required(value, fields, "blocker")
    tenant_id, company_id = _scope(value, "blocker")
    severity = value.get("severity")
    if isinstance(severity, Enum):
        severity = severity.value
    if severity not in BLOCKER_SEVERITIES or not isinstance(value.get("open"), bool):
        raise ProjectionError("blocker severity/open state is invalid")
    text_fields = ("blocker_id", "affected_target", "reason", "remediation", "owner")
    if any(not isinstance(value[field], str) or not value[field].strip() for field in text_fields):
        raise ProjectionError("blocker displayed fields must be nonempty strings")
    return {
        "tenant_id": tenant_id, "company_id": company_id, "blocker_id": value["blocker_id"],
        "severity": severity, "affected_target": value["affected_target"], "reason": value["reason"],
        "remediation": value["remediation"], "owner": value["owner"], "open": value["open"],
    }


def adapt_readiness(record: Any, *, tenant_id: str, company_id: str, explanation: str) -> ScopedEnvelope:
    fields = ("ready", "fully_set", "unmet_ready", "unmet_fully_set")
    if isinstance(record, Mapping):
        value = dict(record)
        _required(value, fields, "readiness")
    elif all(hasattr(record, field) for field in fields):
        value = {field: getattr(record, field) for field in fields}
    else:
        raise ProjectionError("readiness adapter requires an EvaluationResult or mapping")
    if not isinstance(value["ready"], bool) or not isinstance(value["fully_set"], bool):
        raise ProjectionError("readiness flags must be booleans")
    return ScopedEnvelope(tenant_id, company_id, {
        "ready": value["ready"], "fully_set": value["fully_set"],
        "unmet_ready": list(value["unmet_ready"]), "unmet_fully_set": list(value["unmet_fully_set"]),
        "explanation": explanation,
    })


def adapt_budget(record: Mapping[str, Any]) -> ScopedEnvelope:
    value = _mapping(record, "budget-spend.v2")
    _required(value, ("tenant_id", "company_id", "ceiling", "reserved", "settled", "state", "version"), "budget")
    tenant_id, company_id = _scope(value, "budget")
    monies = [value[name] for name in ("ceiling", "reserved", "settled")]
    if any(not isinstance(item, Mapping) for item in monies):
        raise ProjectionError("budget money values are invalid")
    currencies = {item.get("currency") for item in monies}
    if len(currencies) != 1:
        raise ProjectionError("budget currencies must match")
    amounts = [_minor(item.get("minor_units"), f"budget {name}") for name, item in zip(("ceiling", "reserved", "settled"), monies)]
    return ScopedEnvelope(tenant_id, company_id, {
        "currency": currencies.pop(), "ceiling_minor": amounts[0], "reserved_minor": amounts[1], "settled_minor": amounts[2],
    })


def scoped(tenant_id: str, company_id: str, **value: Any) -> ScopedEnvelope:
    return ScopedEnvelope(tenant_id, company_id, value)
