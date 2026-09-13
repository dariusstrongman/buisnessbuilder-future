from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Iterable, Mapping


class ProjectionError(ValueError):
    """Canonical input could not be projected safely."""


TERMINAL_STATUSES = frozenset({"succeeded", "verified", "cancelled", "failed", "skipped"})
ACTIVE_STATUSES = frozenset({"accepted", "running", "in_progress", "tested", "executed", "submitted"})
BLOCKED_STATUSES = frozenset({"blocked", "waiting_approval", "waiting_founder", "required", "expired"})
BLOCKER_SEVERITIES = frozenset({"informational", "noncritical", "critical"})


@dataclass(frozen=True)
class ScopedEnvelope:
    """A computed projection aggregate bound to exactly one tenant and company."""

    tenant_id: str
    company_id: str
    value: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.company_id:
            raise ProjectionError("scoped envelope requires tenant_id and company_id")


@dataclass(frozen=True)
class BuildRoomProjection:
    schema_version: str
    generated_at: str
    source: Mapping[str, Any]
    company: Mapping[str, Any]
    summary: Mapping[str, Any]
    readiness: Mapping[str, Any]
    budget: Mapping[str, Any]
    work_items: tuple[Mapping[str, Any], ...]
    approvals: tuple[Mapping[str, Any], ...]
    evidence: tuple[Mapping[str, Any], ...]
    founder_actions: tuple[Mapping[str, Any], ...]
    blockers: tuple[Mapping[str, Any], ...]
    timeline: tuple[Mapping[str, Any], ...]
    handoff: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return deepcopy({
            "schema_version": self.schema_version, "generated_at": self.generated_at,
            "source": dict(self.source), "company": dict(self.company), "summary": dict(self.summary),
            "readiness": dict(self.readiness), "budget": dict(self.budget),
            "work_items": [dict(item) for item in self.work_items],
            "approvals": [dict(item) for item in self.approvals],
            "evidence": [dict(item) for item in self.evidence],
            "founder_actions": [dict(item) for item in self.founder_actions],
            "blockers": [dict(item) for item in self.blockers],
            "timeline": [dict(item) for item in self.timeline], "handoff": dict(self.handoff),
        })


def _frozen(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(deepcopy(dict(value)))


def _records(values: Iterable[Mapping[str, Any]], tenant_id: str, company_id: str, kind: str) -> tuple[dict[str, Any], ...]:
    records = tuple(deepcopy(dict(raw)) for raw in values)
    if any(item.get("tenant_id") != tenant_id or item.get("company_id") != company_id for item in records):
        raise ProjectionError(f"{kind} escaped tenant/company scope")
    return records


def _envelope(envelope: ScopedEnvelope, tenant_id: str, company_id: str, kind: str) -> dict[str, Any]:
    if not isinstance(envelope, ScopedEnvelope):
        raise ProjectionError(f"{kind} must be a ScopedEnvelope")
    if envelope.tenant_id != tenant_id or envelope.company_id != company_id:
        raise ProjectionError(f"{kind} escaped tenant/company scope")
    return deepcopy(dict(envelope.value))


def _minor(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProjectionError(f"{field} must be an exact integer minor-unit value")
    if value < 0:
        raise ProjectionError(f"{field} cannot be negative")
    return value


def _money(minor: int, currency: str) -> str:
    return f"{currency} {Decimal(minor) / Decimal(100):,.2f}"


def _status_group(status: str) -> str:
    if status in TERMINAL_STATUSES:
        return "done" if status in {"succeeded", "verified", "skipped"} else status
    if status in ACTIVE_STATUSES:
        return "active"
    if status in BLOCKED_STATUSES:
        return "blocked"
    return "queued"


def project_build_room(
    *, generated_at: str, source: ScopedEnvelope, company: Mapping[str, Any], readiness: ScopedEnvelope,
    budget: ScopedEnvelope, jobs: Iterable[Mapping[str, Any]], verifications: Iterable[Mapping[str, Any]],
    approvals: Iterable[Mapping[str, Any]], evidence: Iterable[Mapping[str, Any]],
    founder_actions: Iterable[Mapping[str, Any]], blockers: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]], handoff: ScopedEnvelope,
) -> BuildRoomProjection:
    """Build a deterministic view from validated Build Room adapter output; performs no I/O."""
    tenant_id, company_id = company.get("tenant_id"), company.get("company_id")
    if not isinstance(tenant_id, str) or not tenant_id or not isinstance(company_id, str) or not company_id:
        raise ProjectionError("company tenant_id and company_id are required")

    source_in = _envelope(source, tenant_id, company_id, "source")
    readiness_in = _envelope(readiness, tenant_id, company_id, "readiness")
    budget_in = _envelope(budget, tenant_id, company_id, "budget")
    handoff_in = _envelope(handoff, tenant_id, company_id, "handoff")
    jobs_in = _records(jobs, tenant_id, company_id, "job")
    checks_in = _records(verifications, tenant_id, company_id, "verification")
    approvals_in = _records(approvals, tenant_id, company_id, "approval")
    evidence_in = _records(evidence, tenant_id, company_id, "evidence")
    actions_in = _records(founder_actions, tenant_id, company_id, "founder action")
    blockers_in = _records(blockers, tenant_id, company_id, "blocker")
    events_in = _records(events, tenant_id, company_id, "event")

    evidence_artifacts = {item.get("artifact_ref") for item in evidence_in}
    for item in (*jobs_in, *checks_in):
        for reference in item.get("evidence_refs", ()):
            if not isinstance(reference, str):
                raise ProjectionError("work item evidence reference must be a typed string")
            _, _, artifact_id = reference.partition(":")
            if not artifact_id or artifact_id not in evidence_artifacts:
                raise ProjectionError("work item references missing scoped evidence")

    for blocker in blockers_in:
        if blocker.get("severity") not in BLOCKER_SEVERITIES or not isinstance(blocker.get("open"), bool):
            raise ProjectionError("blocker severity/open state is invalid")
    open_critical = tuple(item for item in blockers_in if item["open"] and item["severity"] == "critical")

    raw_ready, raw_fully_set = readiness_in.get("ready"), readiness_in.get("fully_set")
    if not isinstance(raw_ready, bool) or not isinstance(raw_fully_set, bool):
        raise ProjectionError("readiness flags must be booleans")
    if raw_fully_set and not raw_ready:
        raise ProjectionError("Fully Set cannot be true while Ready is false")
    ready = raw_ready and not open_critical
    fully_set = raw_fully_set and ready

    ceiling = _minor(budget_in.get("ceiling_minor"), "ceiling_minor")
    settled = _minor(budget_in.get("settled_minor"), "settled_minor")
    reserved = _minor(budget_in.get("reserved_minor"), "reserved_minor")
    if settled + reserved > ceiling:
        raise ProjectionError("budget projection exceeds its ceiling")
    currency = budget_in.get("currency")
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isupper():
        raise ProjectionError("budget currency must be a three-letter uppercase code")

    work: list[dict[str, Any]] = []
    for item in (*jobs_in, *checks_in):
        status = item.get("status")
        if not isinstance(status, str):
            raise ProjectionError("work status is required")
        work.append({
            "id": item["id"], "kind": item["kind"], "title": item["title"],
            "description": item.get("description", ""), "owner": item["owner"], "status": status,
            "status_group": _status_group(status), "dependency_ids": list(item.get("dependency_ids", ())),
            "cost": _money(_minor(item.get("cost_minor", 0), "work cost_minor"), currency),
            "evidence_refs": list(item.get("evidence_refs", ())), "detail": item.get("detail", ""),
            "order": _minor(item.get("order", 999), "work order"),
        })
    work.sort(key=lambda item: (item["order"], item["id"]))
    for item in work:
        del item["order"]

    complete = sum(item["status_group"] == "done" for item in work)
    active = sum(item["status_group"] == "active" for item in work)
    blocked = sum(item["status_group"] == "blocked" for item in work)
    progress = round((complete / len(work)) * 100) if work else 0

    seen_sequences: set[int] = set()
    timeline: list[dict[str, Any]] = []
    for item in events_in:
        if item.get("visibility") != "customer":
            continue
        sequence = _minor(item.get("sequence"), "event sequence")
        if sequence < 1 or sequence in seen_sequences:
            raise ProjectionError("customer event sequence must be unique and positive")
        seen_sequences.add(sequence)
        timeline.append({
            "event_id": item["event_id"], "event_type": item["event_type"], "label": item["label"],
            "occurred_at": item["occurred_at"], "sequence": sequence, "detail": item.get("detail", ""),
        })
    timeline.sort(key=lambda item: (item["sequence"], item["occurred_at"], item["event_id"]))

    return BuildRoomProjection(
        schema_version="build-room.projection.v1", generated_at=generated_at,
        source=_frozen(source_in), company=_frozen(company),
        summary=_frozen({"total": len(work), "complete": complete, "active": active, "blocked": blocked, "progress_percent": progress}),
        readiness=_frozen({
            "ready": ready, "fully_set": fully_set,
            "unmet_ready": list(readiness_in.get("unmet_ready", ())),
            "unmet_fully_set": list(readiness_in.get("unmet_fully_set", ())),
            "explanation": readiness_in.get("explanation", "Readiness is calculated by Verification, not by this interface."),
        }),
        budget=_frozen({
            "currency": currency, "ceiling_minor": ceiling, "reserved_minor": reserved, "settled_minor": settled,
            "available_minor": ceiling - settled - reserved, "ceiling": _money(ceiling, currency),
            "reserved": _money(reserved, currency), "settled": _money(settled, currency),
            "available": _money(ceiling - settled - reserved, currency),
        }),
        work_items=tuple(_frozen(item) for item in work),
        approvals=tuple(_frozen(item) for item in sorted(approvals_in, key=lambda x: (x["requested_at"], x["approval_id"]))),
        evidence=tuple(_frozen(item) for item in sorted(evidence_in, key=lambda x: (x["captured_at"], x["evidence_id"]))),
        founder_actions=tuple(_frozen(item) for item in sorted(actions_in, key=lambda x: (x["order"], x["founder_action_id"]))),
        blockers=tuple(_frozen(item) for item in sorted(blockers_in, key=lambda x: (not x["open"], x["severity"], x["blocker_id"]))),
        timeline=tuple(_frozen(item) for item in timeline), handoff=_frozen(handoff_in),
    )
