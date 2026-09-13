from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from .adapters import (
    adapt_approval, adapt_blocker, adapt_budget, adapt_event, adapt_evidence, adapt_founder_action,
    adapt_job, adapt_readiness, adapt_verification, scoped,
)
from .projection import BuildRoomProjection, project_build_room


TENANT_ID = "tenant_billy"
COMPANY_ID = "co_billy_bob_lawn"
FIXED_TIME = "2026-09-13T18:00:00Z"


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{sha256(encoded).hexdigest()}"


def _ref(kind: str, identifier: str) -> dict[str, Any]:
    return {"type": kind, "id": identifier, "version": 1}


def canonical_billy_bob_records() -> dict[str, Any]:
    """Contract-valid v2 records plus domain-shaped evidence/blockers for the offline trace."""
    job_specs = (
        ("job_bb_brain_001", "company_brain_lock_offer", "Approve the narrow launch offer and its explicit exclusions.", "succeeded", []),
        ("job_bb_website_001", "fake_website_build", "Create an accessible responsive package from approved Billy Bob facts.", "succeeded", [_ref("artifact", "artifact_brief")]),
        ("job_bb_handoff_001", "business_handoff", "Assemble the asset export ownership map evidence pack and runbook.", "waiting_founder", [_ref("artifact", "artifact_website_package")]),
    )
    jobs = tuple({
        "schema_version": "job.v2", "tenant_id": TENANT_ID, "job_id": job_id, "company_id": COMPANY_ID,
        "capability_id": capability, "objective": objective, "status": status,
        "idempotency_key": f"offline-billy-{job_id}-v1", "input_artifact_refs": inputs,
        "output_artifact_refs": [_ref("artifact", "artifact_website_package")] if job_id == "job_bb_website_001" else [],
        "required_approval_refs": [_ref("approval", "approval_bb_website_direction")] if job_id == "job_bb_website_001" else [],
        "budget_ref": _ref("budget", "budget_bb_build"), "provider_ref": "fake.website.build" if job_id == "job_bb_website_001" else None,
        "created_at": "2026-09-13T17:50:00Z", "updated_at": "2026-09-13T18:00:00Z", "version": 1,
    } for job_id, capability, objective, status, inputs in job_specs)

    verification_specs = (
        ("website_deployed", "executed", "A deploy attempt exists; production ownership is not yet verified."),
        ("website_https", "tested", "TLS passed offline; current live evidence is still required."),
        ("website_links", "verified", "Required routes and links passed deterministic checks."),
        ("website_mobile", "verified", "Responsive behavior passed declared mobile checks."),
        ("website_forms", "verified", "Valid, invalid and out-of-radius lead cases passed."),
        ("crm_lead_capture", "verified", "Test leads were stored and traceable."),
        ("workflow_quote", "verified", "Standard and exception quote fixtures passed."),
        ("scheduling_booking", "verified", "Capacity and conflict checks passed."),
        ("email_inbound", "verified", "Inbound support test reached the owned queue."),
        ("workflow_rollback", "verified", "Failed release can be safely withdrawn."),
        ("monitoring_active", "proposed", "Monitoring requires a named owner before Fully Set."),
        ("handoff_complete", "proposed", "Take the Keys has not yet been accepted."),
    )
    verifications = tuple({
        "schema_version": "verification.v2", "tenant_id": TENANT_ID,
        "verification_id": f"verify_{definition}", "company_id": COMPANY_ID,
        "target_ref": _ref("requirement", definition), "state": state,
        "method": "automated_plus_human" if definition.startswith("website") else "automated",
        "evidence_refs": [_ref("artifact", f"artifact_{definition}_result")] if state in {"tested", "verified"} else [],
        "verified_scope": detail, "failure_code": None, "expires_at": None,
        "retest_on": ["market_denton_12mi"] if definition.startswith("website") else [],
        "criticality": "critical" if definition in {"website_deployed", "website_https"} else "material",
        "updated_at": FIXED_TIME, "version": 1,
    } for definition, state, detail in verification_specs)

    approval_specs = (
        ("approval_bb_launch_scope", "scope", "granted", "2026-09-13T17:54:00Z"),
        ("approval_bb_website_direction", "direction", "granted", "2026-09-13T17:56:00Z"),
        ("approval_bb_publish", "publish", "requested", None),
    )
    approvals = tuple({
        "schema_version": "approval.v2", "tenant_id": TENANT_ID, "approval_id": approval_id,
        "company_id": COMPANY_ID, "approval_type": approval_type,
        "subject_ref": _ref("job", "job_bb_website_001"), "subject_digest": _digest({"approval_id": approval_id}),
        "requested_by": _ref("system", "runtime_approvals"), "required_approver_role": "founder",
        "state": state, "decision_by": _ref("party", "party_billy") if state == "granted" else None,
        "decision_reason": "Explicitly reviewed in the offline trace." if state == "granted" else None,
        "requested_at": "2026-09-13T17:52:00Z", "decided_at": decided_at, "expires_at": None, "version": 1,
    } for approval_id, approval_type, state, decided_at in approval_specs)

    founder_actions = (
        {
            "schema_version": "founder-action.v2", "tenant_id": TENANT_ID, "founder_action_id": "founder_buy_domain",
            "company_id": COMPANY_ID, "action_type": "purchase", "title": "Buy the domain in your account",
            "reason": "Customer ownership and registrar evidence are required before launch.",
            "instructions": ["Purchase the approved domain in an account you control.", "Provide the receipt and ownership confirmation."],
            "risk": "medium", "irreversible": True, "state": "required",
            "required_evidence_kinds": ["provider_receipt", "founder_attestation"], "evidence_refs": [],
            "blocks": [_ref("verification", "verify_website_deployed")], "due_at": None,
            "created_at": FIXED_TIME, "version": 1,
        },
        {
            "schema_version": "founder-action.v2", "tenant_id": TENANT_ID, "founder_action_id": "founder_accept_handoff",
            "company_id": COMPANY_ID, "action_type": "accept_terms", "title": "Accept the Take the Keys handoff",
            "reason": "Confirms that assets, access and the runbook were received.",
            "instructions": ["Review the export and account map after deployment checks pass."],
            "risk": "low", "irreversible": False, "state": "required",
            "required_evidence_kinds": ["founder_attestation"], "evidence_refs": [],
            "blocks": [_ref("verification", "verify_handoff_complete")], "due_at": None,
            "created_at": FIXED_TIME, "version": 1,
        },
    )

    verification_evidence = tuple({
        "evidence_id": f"evidence_{definition}_result", "tenant_id": TENANT_ID, "company_id": COMPANY_ID,
        "evidence_type": "test_result", "artifact_ref": f"artifact_{definition}_result", "captured_at": FIXED_TIME,
        "expires_at": None, "test_name": definition.replace("_", "-"), "test_passed": True,
        "issuer": "offline-independent-qa", "provenance": {"mode": "offline_fixture"},
    } for definition, state, _ in verification_specs if state in {"tested", "verified"})
    package_evidence = {
        "evidence_id": "evidence_website_package", "tenant_id": TENANT_ID, "company_id": COMPANY_ID,
        "evidence_type": "audit_record", "artifact_ref": "artifact_website_package", "captured_at": FIXED_TIME,
        "expires_at": None, "test_name": None, "test_passed": None, "issuer": "fake.website.build",
        "provenance": {"mode": "offline_fixture", "meaning": "package_ready_not_deployed"},
    }
    evidence = (*verification_evidence, package_evidence)

    raw_events = (
        (1, "company.created", "Company workspace created", "Billy Bob Lawn Care entered assembly."),
        (2, "decision.approved", "Launch scope approved", "The service area and exclusions were locked."),
        (3, "job.requested", "Website build queued", "The website job was created with a bounded ceiling."),
        (4, "founder.approved", "Build approved", "Billy approved the exact website job subject."),
        (5, "job.started", "Website build started", "The offline fake began one bounded attempt."),
        (6, "capability.progress", "Website package reviewed", "Research, build and review milestones completed."),
        (7, "budget.settled", "Build cost settled", "$2.71 settled and the unused reservation was released."),
        (8, "job.completed", "Website package ready", "Package artifact produced. Deployment is separate."),
        (9, "verification.requested", "Independent checks opened", "Customer-path verification entered its evidence flow."),
    )
    events = tuple({
        "schema_version": "event.v2", "tenant_id": TENANT_ID, "event_id": f"event_build_room_{sequence:02d}",
        "company_id": COMPANY_ID, "event_type": event_type, "occurred_at": f"2026-09-13T17:{50 + sequence:02d}:00Z",
        "recorded_at": f"2026-09-13T17:{50 + sequence:02d}:01Z", "producer": "offline.integration",
        "sequence": sequence, "correlation_id": "correlation_billy_build", "causation_id": None,
        "payload": {"customer_label": label, "customer_detail": detail},
        "payload_digest": _digest({"customer_label": label, "customer_detail": detail}), "visibility": "customer",
    } for sequence, event_type, label, detail in raw_events)

    budget = {
        "schema_version": "budget-spend.v2", "tenant_id": TENANT_ID, "budget_id": "budget_bb_build",
        "company_id": COMPANY_ID, "scope_ref": _ref("company", COMPANY_ID),
        "ceiling": {"currency": "USD", "minor_units": 1000}, "reserved": {"currency": "USD", "minor_units": 0},
        "settled": {"currency": "USD", "minor_units": 271}, "pass_through": {"currency": "USD", "minor_units": 0},
        "state": "active", "overage_policy": "fail_closed", "period_start": "2026-09-13T00:00:00Z",
        "period_end": "2026-10-13T00:00:00Z", "approval_ref": None, "version": 1,
    }
    return {"jobs": jobs, "verifications": verifications, "approvals": approvals, "founder_actions": founder_actions, "evidence": evidence, "events": events, "budget": budget}


def billy_bob_build_room() -> BuildRoomProjection:
    records = canonical_billy_bob_records()
    job_titles = ("Lock the launch offer", "Build the customer website package", "Prepare Take the Keys handoff")
    job_details = (
        "Chemicals, irrigation, tree work and hardscape remain outside launch scope.",
        "Package ready is delivery evidence only. It does not mean deployed or live.",
        "Handoff stays locked until deployment evidence is current and founder-owned accounts are confirmed.",
    )
    jobs = tuple(adapt_job(
        item, title=job_titles[index], owner="Website capability (offline fake)" if index == 1 else "Business Builder",
        dependency_ids=("job_bb_brain_001",) if index == 1 else (("verify_handoff_complete", "founder_buy_domain") if index == 2 else ()),
        cost_minor=271 if index == 1 else 0,
        evidence_refs=("artifact:artifact_website_package",) if index == 1 else (), detail=job_details[index], order=(10, 20, 90)[index],
    ) for index, item in enumerate(records["jobs"]))
    verification_titles = (
        "Production deployment", "Secure HTTPS", "Links and navigation", "Mobile experience", "Customer contact form",
        "Lead capture", "Quote path", "Scheduling path", "Support inbox", "Rollback path", "Launch monitoring", "Customer handoff",
    )
    verifications = tuple(adapt_verification(item, title=verification_titles[index], owner="Independent QA", order=30 + index) for index, item in enumerate(records["verifications"]))
    approval_titles = ("Launch scope and exclusions", "Website direction", "Publish website")
    approval_summaries = ("12-mile mow, edge and blow offer; no regulated work.", "Founder approved the customer-facing direction.", "Publishing remains blocked until deployment ownership is proven.")
    approvals = tuple(adapt_approval(item, title=approval_titles[index], summary=approval_summaries[index]) for index, item in enumerate(records["approvals"]))
    evidence = tuple(adapt_evidence(
        item,
        title=item["test_name"].replace("-", " ").title() if item["test_name"] else "Website package artifact",
    ) for item in records["evidence"])
    actions = tuple(adapt_founder_action(item, order=index + 1) for index, item in enumerate(records["founder_actions"]))
    events = tuple(adapt_event(item) for item in records["events"])
    raw_blockers = (
        {"tenant_id": TENANT_ID, "company_id": COMPANY_ID, "blocker_id": "blocker_domain_ownership", "severity": "critical", "affected_target": "website.deployed", "reason": "The domain is not yet confirmed in Billy's registrar account.", "remediation": "Billy purchases the domain and supplies customer-owned account evidence.", "owner": "Billy Bob", "open": True},
        {"tenant_id": TENANT_ID, "company_id": COMPANY_ID, "blocker_id": "blocker_monitoring_owner", "severity": "noncritical", "affected_target": "monitoring.active", "reason": "A named monitoring owner has not been selected.", "remediation": "Assign an owner during handoff.", "owner": "Billy Bob", "open": True},
    )
    blockers = tuple(adapt_blocker(item) for item in raw_blockers)
    return project_build_room(
        generated_at=FIXED_TIME,
        source=scoped(TENANT_ID, COMPANY_ID, mode="offline_fixture", fixture="Billy Bob v1", canonical=True, mutable=False),
        company={"tenant_id": TENANT_ID, "company_id": COMPANY_ID, "display_name": "Billy Bob Lawn Care", "archetype": "Mobile service", "jurisdiction": "Denton County, Texas", "offer": "Recurring mow, edge & blow", "service_area": "12-mile radius", "lifecycle": "assembly"},
        readiness=adapt_readiness({"ready": False, "fully_set": False, "unmet_ready": ["website_deployed", "website_https", "ownership"], "unmet_fully_set": ["monitoring", "handoff", "founder_buy_domain"]}, tenant_id=TENANT_ID, company_id=COMPANY_ID, explanation="The package is built, but Ready requires owned deployment and current customer-path evidence. Fully Set additionally requires monitoring and handoff."),
        budget=adapt_budget(records["budget"]), jobs=jobs, verifications=verifications, approvals=approvals, evidence=evidence,
        founder_actions=actions, blockers=blockers, events=events,
        handoff=scoped(TENANT_ID, COMPANY_ID, state="locked", title="Take the Keys", description="Your export, account map, evidence pack and operating runbook stay customer-owned.", includes=["Website package", "Brand and content assets", "Account ownership map", "Verification evidence", "Launch and rollback runbook"], blocked_by=["founder_buy_domain", "website_deployed", "monitoring_active"]),
    )
