from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import re
from typing import Callable

from .models import (
    ActionRequest,
    AuditRecord,
    ManagementAuthorityProof,
    PolicyDecision,
    PolicyEvaluation,
    RoleDefinition,
    RoleState,
    digest,
    utc_now,
)
from .repository import IdempotencyConflict, InMemoryWorkforceRepository


FOUNDER_BOUND_ACTIONS = frozenset({
    "identity",
    "sign",
    "file",
    "purchase",
    "accept_terms",
    "connect_account",
    "approve_claim",
    "select_insurance",
    "increase_budget",
    "sign_document",
    "sign_contract",
    "submit_filing",
    "file_entity",
    "purchase_domain",
    "buy_domain",
    "register_domain",
    "pay",
    "make_payment",
    "open_account",
    "accept_provider_terms",
    "accept_contract",
    "agree_to_terms",
    "verify_identity",
    "attest_license",
    "choose_insurance",
    "hire_worker",
    "setup_bank_account",
    "setup_payment_account",
    "submit_formation",
    "approve_exceptional_quote",
    "approve_exception_quote",
    "issue_refund",
    "approve_refund",
    "execute_refund",
    "publish_website",
    "public_launch",
    "launch_publicly",
    "classify_employee",
    "employee_classification",
    "delete_account",
    "destroy_account",
    "close_account",
    "launch_paid_media",
    "run_paid_media",
    "buy_ads",
    "purchase_ads",
    "raise_budget",
    "bypass_approval",
    "approve_public_claim",
    "authorize_spend",
})


def normalize_action(action: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", action.strip().lower())).strip("_")


def is_founder_bound_action(action: str) -> bool:
    normalized = normalize_action(action)
    if normalized in FOUNDER_BOUND_ACTIONS:
        return True
    tokens = frozenset(normalized.split("_"))
    destructive_account_action = (
        normalized.startswith(("delete_", "destroy_", "close_", "remove_", "terminate_"))
        and "account" in tokens
    )
    bank_or_payment_account_setup = (
        normalized.startswith(("setup_", "open_", "connect_"))
        and "account" in tokens
        and bool(tokens & {"bank", "banking", "payment", "payments"})
    )
    exceptional_quote_action = (
        ("exceptional_quote" in normalized or "exception_quote" in normalized)
        and normalized.startswith(("approve_", "accept_", "issue_", "send_", "execute_"))
    )
    refund_action = (
        "refund" in tokens
        and normalized.startswith((
            "approve_", "issue_", "execute_", "process_", "send_", "make_", "refund_",
        ))
    )
    employee_classification_action = (
        normalized.startswith(("classify_", "set_classification_"))
        and bool(tokens & {"employee", "worker", "employment"})
    )
    return (
        destructive_account_action
        or bank_or_payment_account_setup
        or exceptional_quote_action
        or refund_action
        or employee_classification_action
        or normalized.startswith(("sign_", "purchase_", "pay_", "hire_", "publish_"))
        or normalized.endswith(("_filing", "_formation"))
        or normalized.startswith((
            "accept_terms_", "open_account_", "connect_account_", "verify_identity_",
            "attest_license_", "choose_insurance_", "select_insurance_",
            "raise_budget_", "increase_budget_", "bypass_approval_",
            "approve_claim_", "approve_public_claim_", "authorize_spend_", "file_entity_",
            "file_filing_", "file_formation_", "file_tax_",
            "buy_domain_", "register_domain_", "accept_contract_", "agree_to_terms_",
            "agree_to_contract_", "execute_contract_", "setup_bank_", "setup_payment_",
            "open_bank_", "open_payment_", "connect_bank_", "connect_payment_",
            "submit_formation_", "approve_exceptional_quote_", "approve_exception_quote_",
            "approve_refund_", "issue_refund_", "execute_refund_", "refund_payment_",
            "public_launch_", "launch_public_", "classify_employee_",
            "employee_classification_", "delete_account_", "destroy_account_",
            "close_account_", "launch_paid_", "run_paid_", "start_paid_", "activate_paid_",
            "buy_ad_", "purchase_ad_",
            "transfer_funds_", "wire_funds_", "remit_payment_",
        ))
    )


class StalePolicyAuthority(PermissionError):
    """An idempotent evaluation replay no longer has active role authority."""


class WorkforcePolicyService:
    """Evaluates product-role policy; it never executes a capability."""

    def __init__(
        self,
        repository: InMemoryWorkforceRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[str], str] | None = None,
        authority_verifier: Callable[[ManagementAuthorityProof, str], bool] | None = None,
    ) -> None:
        self.repository = repository
        self.clock = clock
        self._counter = 0
        self.id_factory = id_factory or self._id
        self.authority_verifier = authority_verifier

    def _id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter:06d}"

    def create_role(
        self, definition: RoleDefinition, *, authority: ManagementAuthorityProof,
        idempotency_key: str,
    ) -> RoleDefinition:
        command_digest = digest({"action": "create", "definition": definition.to_projection()})
        self._require_management_authority(authority, definition.tenant_id, definition.company_id, command_digest)
        replay = self.repository.get_command(
            definition.tenant_id, definition.company_id, idempotency_key, command_digest
        )
        if replay:
            return replay
        if (
            definition.version != 1 or definition.authority_epoch != 1
            or definition.state is not RoleState.ACTIVE
        ):
            raise ValueError("new roles must begin as active version 1 and authority epoch 1")
        if self.repository.current_definition(definition.tenant_id, definition.company_id, definition.role_id):
            raise ValueError("role already exists; use revise_role")
        self.repository.add_definition(definition)
        self._audit(definition, "workforce.role.created", authority.actor_id, idempotency_key, None, definition.to_projection())
        return self.repository.save_command(
            definition.tenant_id, definition.company_id, idempotency_key, command_digest, definition
        )

    def revise_role(
        self,
        definition: RoleDefinition,
        *,
        expected_version: int,
        authority: ManagementAuthorityProof,
        idempotency_key: str,
    ) -> RoleDefinition:
        command_digest = digest({
            "action": "revise", "definition": definition.to_projection(),
            "expected_version": expected_version,
        })
        self._require_management_authority(authority, definition.tenant_id, definition.company_id, command_digest)
        replay = self.repository.get_command(
            definition.tenant_id, definition.company_id, idempotency_key, command_digest
        )
        if replay:
            return replay
        current = self.repository.current_definition(definition.tenant_id, definition.company_id, definition.role_id)
        if not current:
            raise LookupError("role definition not found in tenant/company scope")
        if current.version != expected_version:
            raise ValueError("stale role version")
        if current.state is RoleState.REVOKED:
            raise PermissionError("revoked roles cannot be revised")
        if definition.version != expected_version + 1 or definition.state is not RoleState.ACTIVE:
            raise ValueError("revision must be the next active version")
        if (definition.tenant_id, definition.company_id, definition.role_id) != (
            current.tenant_id, current.company_id, current.role_id
        ):
            raise PermissionError("role identity and tenant scope are immutable")
        next_epoch = current.authority_epoch + 1
        effective = replace(definition, authority_epoch=next_epoch)
        self.repository.replace_definition(replace(
            current, state=RoleState.SUPERSEDED, authority_epoch=next_epoch,
        ))
        self.repository.add_definition(effective)
        self._audit(effective, "workforce.role.revised", authority.actor_id, idempotency_key, current.to_projection(), effective.to_projection())
        return self.repository.save_command(
            effective.tenant_id, effective.company_id, idempotency_key, command_digest, effective
        )

    def pause(self, tenant_id: str, company_id: str, role_id: str, *, expected_version: int, authority: ManagementAuthorityProof, idempotency_key: str) -> RoleDefinition:
        return self._state_change(tenant_id, company_id, role_id, expected_version, RoleState.PAUSED, authority, idempotency_key)

    def resume(self, tenant_id: str, company_id: str, role_id: str, *, expected_version: int, authority: ManagementAuthorityProof, idempotency_key: str) -> RoleDefinition:
        return self._state_change(tenant_id, company_id, role_id, expected_version, RoleState.ACTIVE, authority, idempotency_key)

    def revoke(self, tenant_id: str, company_id: str, role_id: str, *, expected_version: int, authority: ManagementAuthorityProof, idempotency_key: str) -> RoleDefinition:
        return self._state_change(tenant_id, company_id, role_id, expected_version, RoleState.REVOKED, authority, idempotency_key)

    def _state_change(self, tenant_id: str, company_id: str, role_id: str, expected_version: int, target: RoleState, authority: ManagementAuthorityProof, idempotency_key: str) -> RoleDefinition:
        command_digest = digest({
            "action": target.value, "tenant_id": tenant_id, "company_id": company_id,
            "role_id": role_id, "expected_version": expected_version,
        })
        self._require_management_authority(authority, tenant_id, company_id, command_digest)
        replay = self.repository.get_command(tenant_id, company_id, idempotency_key, command_digest)
        if replay:
            return replay
        current = self.repository.current_definition(tenant_id, company_id, role_id)
        if not current:
            raise LookupError("role definition not found in tenant/company scope")
        if current.version != expected_version:
            raise ValueError("stale role version")
        if current.state is RoleState.REVOKED:
            if target is RoleState.REVOKED:
                return current
            raise PermissionError("revocation is permanent")
        if target is RoleState.ACTIVE and current.state is not RoleState.PAUSED:
            raise ValueError("only paused roles can resume")
        if target is RoleState.PAUSED and current.state is not RoleState.ACTIVE:
            raise ValueError("only active roles can pause")
        before = current.to_projection()
        changed = replace(current, state=target, authority_epoch=current.authority_epoch + 1)
        self.repository.replace_definition(changed)
        self._audit(changed, f"workforce.role.{target.value}", authority.actor_id, idempotency_key, before, changed.to_projection())
        return self.repository.save_command(
            tenant_id, company_id, idempotency_key, command_digest, changed
        )

    def evaluate(self, request: ActionRequest) -> PolicyEvaluation:
        existing = self.repository.get_evaluation(request.tenant_id, request.company_id, request.idempotency_key)
        if existing:
            if existing.request_digest != request.request_digest:
                raise IdempotencyConflict("idempotency key was reused for a different request")
            role = self.repository.get_definition(
                request.tenant_id, request.company_id, request.role_id, request.role_version
            )
            current = self.repository.current_definition(
                request.tenant_id, request.company_id, request.role_id
            )
            if (
                role is None or current is None or current.version != request.role_version
                or role.state is not RoleState.ACTIVE
                or existing.authority_epoch != role.authority_epoch
                or existing.definition_digest != role.definition_digest
            ):
                raise StalePolicyAuthority("cached evaluation is no longer authorized by the current active role")
            return existing
        role = self.repository.get_definition(
            request.tenant_id, request.company_id, request.role_id, request.role_version
        )
        current = self.repository.current_definition(request.tenant_id, request.company_id, request.role_id)
        if (
            role is not None and role.state is RoleState.ACTIVE
            and (current is None or current.version != request.role_version)
        ):
            decision, code, reason, required_role = (
                PolicyDecision.DENY, "role_not_current", "Requested role version is not current", None
            )
        else:
            decision, code, reason, required_role = self._decide(role, request)
        evaluation = PolicyEvaluation(
            evaluation_id=self.id_factory("evaluation"),
            tenant_id=request.tenant_id,
            company_id=request.company_id,
            role_id=request.role_id,
            role_version=request.role_version,
            authority_epoch=role.authority_epoch if role else None,
            request_id=request.request_id,
            request_digest=request.request_digest,
            decision=decision,
            reason_code=code,
            reason=reason,
            required_role=required_role,
            estimated_minor=request.estimated_minor,
            definition_digest=role.definition_digest if role else None,
            evaluated_at=self.clock(),
        )
        saved = self.repository.save_evaluation(request.idempotency_key, evaluation)
        if saved is evaluation:
            self._audit_evaluation(evaluation)
        return saved

    @staticmethod
    def _decide(role: RoleDefinition | None, request: ActionRequest) -> tuple[PolicyDecision, str, str, str | None]:
        if role is None:
            return PolicyDecision.DENY, "role_not_found", "No role exists in this tenant/company scope", None
        if role.state is not RoleState.ACTIVE:
            return PolicyDecision.DENY, f"role_{role.state.value}", "Role is not active", None
        if is_founder_bound_action(request.action):
            return PolicyDecision.DENY, "founder_bound_action", "AI workers can never perform this founder-bound action", "founder"
        if request.action in role.denied_actions:
            return PolicyDecision.DENY, "explicitly_denied", "Action is explicitly denied by the role definition", None
        escalation = next(
            (rule for rule in role.escalation_rules if rule.trigger == request.action or rule.trigger in request.context_flags),
            None,
        )
        if escalation:
            return PolicyDecision.ESCALATE, "escalation_required", escalation.reason, escalation.required_role
        grant = next(
            (grant for grant in role.grants if grant.capability == request.capability and request.action in grant.actions),
            None,
        )
        if grant is None:
            return PolicyDecision.DENY, "not_granted", "No exact capability/action grant exists", None
        if request.currency != role.budget.currency:
            return PolicyDecision.DENY, "currency_mismatch", "Request currency does not match the role budget", None
        if request.estimated_minor > role.budget.per_action_minor:
            return PolicyDecision.ESCALATE, "per_action_budget_exceeded", "Requested cost exceeds the role ceiling; the AI cannot raise it", "founder"
        if request.period_spend_minor + request.estimated_minor > role.budget.period_minor:
            return PolicyDecision.ESCALATE, "period_budget_exceeded", "Period ceiling would be exceeded; the AI cannot raise it", "founder"
        if request.action in role.approval_actions:
            return PolicyDecision.APPROVAL_REQUIRED, "founder_approval_required", "A digest-bound founder approval is required before execution", "founder"
        return PolicyDecision.ALLOW, "explicit_grant", "Exact role capability/action grant and budget checks passed", None

    def _require_management_authority(
        self, authority: ManagementAuthorityProof, tenant_id: str, company_id: str,
        command_digest: str,
    ) -> None:
        if (authority.tenant_id, authority.company_id) != (tenant_id, company_id):
            raise PermissionError("management authority proof is outside the tenant/company scope")
        if self.authority_verifier is None or not self.authority_verifier(authority, command_digest):
            raise PermissionError("verified founder or authorized-manager authority is required")

    def _audit(self, role: RoleDefinition, action: str, actor_id: str, reason: str, before: object, after: object) -> None:
        self.repository.append_audit(AuditRecord(
            audit_id=self.id_factory("audit"), tenant_id=role.tenant_id, company_id=role.company_id,
            action=action, target_id=role.role_id, actor_id=actor_id, reason=reason,
            before_digest=digest(before) if before is not None else None,
            after_digest=digest(after) if after is not None else None,
            occurred_at=self.clock(),
        ))

    def _audit_evaluation(self, evaluation: PolicyEvaluation) -> None:
        self.repository.append_audit(AuditRecord(
            audit_id=self.id_factory("audit"), tenant_id=evaluation.tenant_id,
            company_id=evaluation.company_id, action="workforce.policy.evaluated",
            target_id=evaluation.evaluation_id, actor_id=evaluation.role_id,
            reason=evaluation.reason_code, before_digest=None,
            after_digest=digest({"decision": evaluation.decision.value, "request": evaluation.request_digest}),
            occurred_at=self.clock(),
        ))
