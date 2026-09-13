from __future__ import annotations

from datetime import datetime, timezone

from .models import BudgetCeiling, CapabilityGrant, EscalationRule, ManagementAuthorityProof, RoleDefinition
from .policy import FOUNDER_BOUND_ACTIONS, WorkforcePolicyService


BILLY_TENANT_ID = "tenant_billy"
BILLY_COMPANY_ID = "co_billy_bob_lawn"
FIXED_NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
BILLY_FOUNDER_AUTHORITY = ManagementAuthorityProof(
    proof_ref="approval_billy_workforce_admin",
    tenant_id=BILLY_TENANT_ID,
    company_id=BILLY_COMPANY_ID,
    actor_id="founder_billy",
    actor_role="founder",
)


def verify_billy_management_authority(proof: ManagementAuthorityProof, command_digest: str) -> bool:
    """Offline fixture verifier standing in for a trusted approval/identity adapter."""
    return proof == BILLY_FOUNDER_AUTHORITY and command_digest.startswith("sha256:")

COMMON_DENIALS = FOUNDER_BOUND_ACTIONS | frozenset({
    "delete_customer_record",
    "export_customer_data",
    "change_account_access",
    "disable_audit",
    "change_role_policy",
})


def billy_bob_roles() -> tuple[RoleDefinition, ...]:
    return (
        RoleDefinition(
            role_id="role_intake_assistant",
            tenant_id=BILLY_TENANT_ID,
            company_id=BILLY_COMPANY_ID,
            name="Intake Assistant",
            objective="Capture complete lead facts and route only supported lawn-care requests without inventing customer or service details.",
            grants=(
                CapabilityGrant("customer.intake", frozenset({"read_submission", "classify_lead", "validate_required_fields", "request_missing_information"})),
                CapabilityGrant("communications.email", frozenset({"draft_acknowledgement"})),
            ),
            denied_actions=COMMON_DENIALS | frozenset({"accept_job", "promise_availability", "change_service_area", "send_nonstandard_reply"}),
            approval_actions=frozenset(),
            escalation_rules=(
                EscalationRule("out_of_radius", "Founder decides whether to consider an out-of-radius lead"),
                EscalationRule("unsupported_service", "Founder reviews requests outside approved mow, edge, and blow scope"),
                EscalationRule("safety_concern", "Safety concerns require human review"),
            ),
            budget=BudgetCeiling("USD", per_action_minor=0, period_minor=0),
            created_at=FIXED_NOW,
        ),
        RoleDefinition(
            role_id="role_quote_drafting_assistant",
            tenant_id=BILLY_TENANT_ID,
            company_id=BILLY_COMPANY_ID,
            name="Quote Drafting Assistant",
            objective="Draft quotes from Billy's approved services, radius, capacity, and pricing policy while routing every exception to Billy.",
            grants=(
                CapabilityGrant("company.policy", frozenset({"read_quote_policy", "read_service_scope", "read_capacity"})),
                CapabilityGrant("customer.quote", frozenset({"draft_standard_quote", "draft_exception_quote"})),
            ),
            denied_actions=COMMON_DENIALS | frozenset({"send_quote", "accept_job", "approve_discount", "issue_refund", "change_price_policy"}),
            approval_actions=frozenset({"draft_exception_quote"}),
            escalation_rules=(
                EscalationRule("missing_quote_fields", "A quote cannot be drafted from incomplete required facts"),
                EscalationRule("capacity_exception", "Billy decides work outside approved capacity"),
                EscalationRule("regulated_service", "Potential regulated work is outside the approved offer"),
            ),
            budget=BudgetCeiling("USD", per_action_minor=0, period_minor=0),
            created_at=FIXED_NOW,
        ),
        RoleDefinition(
            role_id="role_inbox_assistant",
            tenant_id=BILLY_TENANT_ID,
            company_id=BILLY_COMPANY_ID,
            name="Inbox Assistant",
            objective="Classify company email, draft responses, and handle only preapproved routine messages while escalating true owner-only exceptions.",
            grants=(CapabilityGrant("communications.email", frozenset({
                "read_message", "classify_message", "draft_reply", "send_preapproved_reply", "archive_message", "mark_spam",
            })),),
            denied_actions=COMMON_DENIALS | frozenset({"delete_mailbox", "change_forwarding", "send_bulk_email", "accept_contract", "issue_refund"}),
            approval_actions=frozenset(),
            escalation_rules=(
                EscalationRule("legal_threat", "Legal threats require founder handling"),
                EscalationRule("payment_dispute", "Payment disputes require founder handling"),
                EscalationRule("refund_request", "Refund decisions are founder-bound"),
                EscalationRule("safety_incident", "Safety incidents require immediate human review"),
                EscalationRule("nonstandard_commitment", "The assistant cannot make a new business commitment"),
            ),
            budget=BudgetCeiling("USD", per_action_minor=5, period_minor=100),
            created_at=FIXED_NOW,
        ),
        RoleDefinition(
            role_id="role_review_followup_assistant",
            tenant_id=BILLY_TENANT_ID,
            company_id=BILLY_COMPANY_ID,
            name="Review Follow-up Assistant",
            objective="Request honest feedback after confirmed completed service using Billy's approved template and contact rules.",
            grants=(
                CapabilityGrant("customer.service_history", frozenset({"read_completion_status", "check_contact_permission"})),
                CapabilityGrant("communications.email", frozenset({"draft_review_request", "send_preapproved_review_request"})),
            ),
            denied_actions=COMMON_DENIALS | frozenset({"fabricate_review", "offer_review_incentive", "suppress_negative_feedback", "contact_without_permission", "contact_incomplete_job"}),
            approval_actions=frozenset(),
            escalation_rules=(
                EscalationRule("negative_feedback", "Negative feedback routes to Billy instead of being suppressed"),
                EscalationRule("customer_opted_out", "Opted-out customers must not be contacted"),
                EscalationRule("completion_uncertain", "No review request is sent without confirmed completion"),
            ),
            budget=BudgetCeiling("USD", per_action_minor=5, period_minor=100),
            created_at=FIXED_NOW,
        ),
    )


def load_billy_bob_workforce(service: WorkforcePolicyService) -> tuple[RoleDefinition, ...]:
    roles = billy_bob_roles()
    for role in roles:
        service.create_role(
            role,
            authority=BILLY_FOUNDER_AUTHORITY,
            idempotency_key=f"fixture-create-{role.role_id}",
        )
    return roles
