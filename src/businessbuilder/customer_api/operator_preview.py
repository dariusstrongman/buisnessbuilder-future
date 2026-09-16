"""Read-only operator (ADMIN PREVIEW) projections over existing authorities.

Nothing here is a new authority. Every value is read back from Company Brain,
Runtime, Verification, Commercial or Identity through the same scoped principal
the founder surfaces use, and every route is GET-only. There is no impersonation:
the caller stays themselves, the preview is labelled, and the support grant that
authorises it is returned alongside the data so the interface can say so.

Where a support grant genuinely cannot see something -- Commercial billing is
permanently outside `SUPPORT_NEVER_ALLOWED` -- the section is reported as
unavailable with a reason rather than fabricated or silently omitted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from businessbuilder.company_brain import RecordKind, Scope
from businessbuilder.identity import (
    MembershipStatus,
    Permission,
    Role,
    SupportAccessGrant,
    SupportImpersonationSession,
)


PREVIEW_MODE = "operator_preview"
PREVIEW_LABEL = "ADMIN PREVIEW"
PREVIEW_NOTICE = (
    "Operator preview. You are signed in as a Business Builder operator, not as the "
    "founder. This view is read-only, company-scoped, expiring and audited."
)
# Sections an operator preview can never present as founder state, with the reason.
PERMANENTLY_UNAVAILABLE = (
    {
        "section": "commercial_billing",
        "reason": (
            "Billing visibility is never delegated through a support grant. Order, "
            "subscription and entitlement detail stays with the founder."
        ),
    },
    {
        "section": "support_escalations",
        "reason": (
            "No escalation or ticket record exists in the backend yet. Operator access "
            "itself is the only authoritative support record, and it is shown above."
        ),
    },
)
# Company Brain kinds that are safe to count for an operator. Customer, lead and
# party records are deliberately excluded: an operator preview summarises the
# shape of a company, it does not read its customer list.
SUMMARY_KINDS = (
    RecordKind.GOAL,
    RecordKind.STRATEGY,
    RecordKind.DECISION,
    RecordKind.OFFER,
    RecordKind.SERVICE,
    RecordKind.MARKET,
    RecordKind.POLICY,
    RecordKind.CAPACITY,
    RecordKind.ASSET,
    RecordKind.WORKFLOW,
    RecordKind.RISK,
    RecordKind.OBLIGATION,
    RecordKind.FOUNDER_ACTION,
)
MAX_RUNTIME_ACTIVITY = 20


def active_support_memberships(repository, user_id: str) -> tuple[str, ...]:
    """Tenants where this user currently holds an active SUPPORT membership."""
    return tuple(
        sorted(
            {
                membership.tenant_id
                for membership in repository.list_user_memberships(user_id)
                if membership.status is MembershipStatus.ACTIVE
                and membership.role is Role.SUPPORT
            }
        )
    )


def grant_view(grant: SupportAccessGrant, at: datetime) -> dict[str, Any]:
    """Operator-facing grant description. Tenant identifiers stay internal."""
    return {
        "grant_id": grant.grant_id,
        "company_id": grant.company_id,
        "company_scoped": grant.company_id is not None,
        "permissions": sorted(item.value for item in grant.permissions),
        "reason": grant.reason,
        "starts_at": grant.starts_at.isoformat(),
        "expires_at": grant.ends_at.isoformat(),
        "active": grant.active_at(at),
        "read_only": not bool(grant.permissions - {Permission.VIEW_COMPANY_STATE, Permission.ACCESS_ARTIFACTS}),
    }


def session_view(session: SupportImpersonationSession, at: datetime) -> dict[str, Any]:
    return {
        "support_session_id": session.impersonation_session_id,
        "grant_id": session.grant_id,
        "company_id": session.company_id,
        "reason": session.reason,
        "started_at": session.started_at.isoformat(),
        "expires_at": session.ends_at.isoformat(),
        "active": session.active_at(at),
    }


def company_brain_summary(company_brain, scope: Scope, company: Mapping[str, Any]) -> dict[str, Any]:
    """A structural roll-up, never a dump of record contents."""
    records = company_brain.query_current_state(scope, kinds=SUMMARY_KINDS)
    counts: dict[str, int] = {}
    latest: str | None = None
    for record in records:
        if record.invalidated_at:
            continue
        counts[record.kind.value] = counts.get(record.kind.value, 0) + 1
        if latest is None or record.updated_at > latest:
            latest = record.updated_at
    return {
        "authority": "company_brain",
        "display_name": company.get("display_name"),
        "archetype": company.get("archetype"),
        "jurisdiction": dict(company.get("jurisdiction") or {}),
        "lifecycle": company.get("lifecycle"),
        "company_version": company.get("version"),
        "record_counts": dict(sorted(counts.items())),
        "recorded_kinds": len(counts),
        "last_recorded_at": latest,
        "detail_withheld": (
            "Record contents are not shown in an operator preview. Counts come from "
            "current, non-invalidated Company Brain records only."
        ),
    }


def runtime_activity(runtime_repository, tenant_id: str, company_id: str) -> dict[str, Any]:
    """Most recent authoritative Runtime job records, newest first."""
    jobs = list(runtime_repository.list_jobs(tenant_id, company_id))
    jobs.sort(key=lambda item: (item.updated_at, item.job_id), reverse=True)
    return {
        "authority": "runtime",
        "total": len(jobs),
        "recent": [
            {
                "job_id": item.job_id,
                "capability": item.capability,
                "status": item.contract_status(),
                "attempts": item.attempts,
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
            }
            for item in jobs[:MAX_RUNTIME_ACTIVITY]
        ],
    }


def connection_health(connections: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts by reported health. No health value is inferred when none is reported."""
    counts: dict[str, int] = {}
    action_required = 0
    for item in connections:
        health = item.get("health")
        if isinstance(health, str) and health:
            counts[health] = counts.get(health, 0) + 1
        if item.get("action_required"):
            action_required += 1
    return {
        "authority": "provider_connection",
        "total": len(connections),
        "by_health": dict(sorted(counts.items())),
        "action_required": action_required,
    }
