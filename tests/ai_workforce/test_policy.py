from __future__ import annotations

from dataclasses import replace
import unittest

from businessbuilder.ai_workforce import (
    ActionRequest,
    BILLY_COMPANY_ID,
    BILLY_FOUNDER_AUTHORITY,
    BILLY_TENANT_ID,
    CapabilityGrant,
    FOUNDER_BOUND_ACTIONS,
    IdempotencyConflict,
    InMemoryWorkforceRepository,
    ManagementAuthorityProof,
    PolicyDecision,
    RoleState,
    StalePolicyAuthority,
    WorkforcePolicyService,
    billy_bob_roles,
    load_billy_bob_workforce,
    verify_billy_management_authority,
)
from businessbuilder.ai_workforce.fixtures import FIXED_NOW


class WorkforceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryWorkforceRepository()
        self.service = WorkforcePolicyService(
            self.repository,
            clock=lambda: FIXED_NOW,
            authority_verifier=verify_billy_management_authority,
        )
        load_billy_bob_workforce(self.service)

    def request(self, **overrides) -> ActionRequest:
        values = {
            "request_id": "request_001",
            "idempotency_key": "idem_001",
            "tenant_id": BILLY_TENANT_ID,
            "company_id": BILLY_COMPANY_ID,
            "role_id": "role_inbox_assistant",
            "role_version": 1,
            "capability": "communications.email",
            "action": "classify_message",
            "estimated_minor": 0,
            "period_spend_minor": 0,
        }
        values.update(overrides)
        return ActionRequest(**values)


class DefinitionTests(WorkforceTestCase):
    def test_fixture_defines_all_four_customer_roles(self):
        names = {role.name for role in self.repository.list_current(BILLY_TENANT_ID, BILLY_COMPANY_ID)}
        self.assertEqual({"Intake Assistant", "Quote Drafting Assistant", "Inbox Assistant", "Review Follow-up Assistant"}, names)
        for role in billy_bob_roles():
            self.assertTrue(role.objective)
            self.assertTrue(role.grants)
            self.assertTrue(role.denied_actions)
            self.assertTrue(role.escalation_rules)
            self.assertGreaterEqual(role.budget.period_minor, role.budget.per_action_minor)

    def test_every_founder_bound_action_is_denied_for_every_role(self):
        for role in billy_bob_roles():
            for index, action in enumerate(sorted(FOUNDER_BOUND_ACTIONS)):
                result = self.service.evaluate(self.request(
                    request_id=f"forbidden_{role.role_id}_{index}",
                    idempotency_key=f"forbidden_{role.role_id}_{index}",
                    role_id=role.role_id,
                    capability="adversarial.capability",
                    action=action,
                ))
                self.assertEqual(PolicyDecision.DENY, result.decision)
                self.assertEqual("founder_bound_action", result.reason_code)
                self.assertFalse(result.permits_execution)

    def test_founder_action_synonyms_are_normalized_and_denied_even_if_granted(self):
        role = self.repository.current_definition(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant"
        )
        for index, action in enumerate((
            "Purchase-Domain", "file.entity", "SIGN CONTRACT", "pay_vendor",
            "connect account banking", "submit_state_filing",
        )):
            expanded = replace(
                role,
                version=role.version + 1,
                grants=role.grants + (CapabilityGrant("adversarial.capability", frozenset({action})),),
            )
            self.service.revise_role(
                expanded, expected_version=role.version, authority=BILLY_FOUNDER_AUTHORITY,
                idempotency_key=f"adversarial-grant-{index}",
            )
            result = self.service.evaluate(self.request(
                request_id=f"synonym-{index}", idempotency_key=f"synonym-{index}",
                role_version=expanded.version, capability="adversarial.capability", action=action,
            ))
            self.assertEqual(PolicyDecision.DENY, result.decision)
            self.assertEqual("founder_bound_action", result.reason_code)
            role = expanded

    def test_authoritative_founder_actions_deny_even_when_explicitly_granted(self):
        actions = (
            "buy_domain", "accept_contract", "accept_provider_terms",
            "setup_bank_account", "open_payment_account", "connect_business_bank_account",
            "submit_formation", "file_llc_formation", "approve_exceptional_quote",
            "execute_exceptional_quote", "issue_refund", "process_customer_refund",
            "publish_website", "public_launch", "classify_employee",
            "set_classification_for_worker", "hire_worker", "delete_account",
            "destroy_bank_account", "launch_paid_media", "authorize_spend",
        )
        role = self.repository.current_definition(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant"
        )
        expanded = replace(
            role,
            version=role.version + 1,
            grants=(CapabilityGrant("adversarial.capability", frozenset(actions)),),
            denied_actions=frozenset(),
            approval_actions=frozenset(),
        )
        expanded = self.service.revise_role(
            expanded, expected_version=role.version, authority=BILLY_FOUNDER_AUTHORITY,
            idempotency_key="grant-authoritative-founder-actions",
        )
        for index, action in enumerate(actions):
            with self.subTest(action=action):
                result = self.service.evaluate(self.request(
                    request_id=f"authoritative-{index}",
                    idempotency_key=f"authoritative-{index}",
                    role_version=expanded.version,
                    capability="adversarial.capability",
                    action=action,
                ))
                self.assertEqual(PolicyDecision.DENY, result.decision)
                self.assertEqual("founder_bound_action", result.reason_code)
                self.assertFalse(result.permits_execution)

    def test_arbitrary_actor_cannot_create_or_self_escalate_role(self):
        attacker = ManagementAuthorityProof(
            proof_ref="forged-worker-proof", tenant_id=BILLY_TENANT_ID,
            company_id=BILLY_COMPANY_ID, actor_id="role_inbox_assistant",
            actor_role="authorized_manager",
        )
        role = self.repository.current_definition(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant"
        )
        expanded = replace(
            role, version=2,
            grants=role.grants + (CapabilityGrant("customer.data", frozenset({"exfiltrate_contacts"})),),
        )
        with self.assertRaises(PermissionError):
            self.service.revise_role(
                expanded, expected_version=1, authority=attacker,
                idempotency_key="self-escalate",
            )
        new_role = replace(
            role, role_id="role_attacker", version=1,
            grants=(CapabilityGrant("domains", frozenset({"purchase_domain"})),),
            denied_actions=frozenset(),
        )
        with self.assertRaises(PermissionError):
            self.service.create_role(
                new_role, authority=attacker, idempotency_key="attacker-create",
            )

    def test_role_creation_cannot_bypass_version_lifecycle(self):
        role = self.repository.current_definition(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant"
        )
        with self.assertRaises(ValueError):
            self.service.create_role(
                replace(role, version=2), authority=BILLY_FOUNDER_AUTHORITY,
                idempotency_key="create-v2-bypass",
            )

    def test_explicitly_authorized_manager_can_manage_role(self):
        manager = ManagementAuthorityProof(
            proof_ref="trusted-manager-receipt", tenant_id=BILLY_TENANT_ID,
            company_id=BILLY_COMPANY_ID, actor_id="manager_jane",
            actor_role="authorized_manager",
        )
        repository = InMemoryWorkforceRepository()
        service = WorkforcePolicyService(
            repository, clock=lambda: FIXED_NOW,
            authority_verifier=lambda proof, command_digest: (
                proof == manager and command_digest.startswith("sha256:")
            ),
        )
        role = replace(billy_bob_roles()[0], role_id="role_manager_created")
        created = service.create_role(
            role, authority=manager, idempotency_key="manager-create",
        )
        self.assertEqual(role, created)
        self.assertEqual(
            "manager_jane",
            repository.list_audit(BILLY_TENANT_ID, BILLY_COMPANY_ID)[0].actor_id,
        )


class PermissionTests(WorkforceTestCase):
    def test_exact_grant_allows_and_unknown_action_denies_by_default(self):
        allowed = self.service.evaluate(self.request())
        denied = self.service.evaluate(self.request(
            request_id="request_unknown", idempotency_key="idem_unknown", action="forward_all_mail"
        ))
        self.assertEqual(PolicyDecision.ALLOW, allowed.decision)
        self.assertEqual(PolicyDecision.DENY, denied.decision)
        self.assertEqual("not_granted", denied.reason_code)

    def test_capability_mismatch_denies_even_when_action_name_is_granted(self):
        result = self.service.evaluate(self.request(
            capability="customer.quote", action="classify_message"
        ))
        self.assertEqual(PolicyDecision.DENY, result.decision)

    def test_explicit_deny_beats_any_other_path(self):
        result = self.service.evaluate(self.request(action="issue_refund"))
        self.assertEqual(PolicyDecision.DENY, result.decision)
        self.assertEqual("founder_bound_action", result.reason_code)

    def test_approval_action_is_not_execution_permission(self):
        result = self.service.evaluate(self.request(
            role_id="role_quote_drafting_assistant", capability="customer.quote",
            action="draft_exception_quote",
        ))
        self.assertEqual(PolicyDecision.APPROVAL_REQUIRED, result.decision)
        self.assertEqual("founder", result.required_role)
        self.assertFalse(result.permits_execution)

    def test_context_escalation_precedes_grant(self):
        result = self.service.evaluate(self.request(context_flags=frozenset({"legal_threat"})))
        self.assertEqual(PolicyDecision.ESCALATE, result.decision)
        self.assertEqual("founder", result.required_role)
        self.assertFalse(result.permits_execution)


class ScopeBudgetAndLifecycleTests(WorkforceTestCase):
    def test_cross_tenant_and_cross_company_access_fails_closed(self):
        for tenant, company, key in (
            ("tenant_other", BILLY_COMPANY_ID, "cross_tenant"),
            (BILLY_TENANT_ID, "company_other", "cross_company"),
        ):
            result = self.service.evaluate(self.request(
                request_id=key, idempotency_key=key, tenant_id=tenant, company_id=company
            ))
            self.assertEqual(PolicyDecision.DENY, result.decision)
            self.assertEqual("role_not_found", result.reason_code)
            self.assertIsNone(result.definition_digest)

    def test_budget_is_fail_closed_and_never_auto_raised(self):
        per_action = self.service.evaluate(self.request(
            request_id="cost_1", idempotency_key="cost_1", action="send_preapproved_reply", estimated_minor=6
        ))
        period = self.service.evaluate(self.request(
            request_id="cost_2", idempotency_key="cost_2", action="send_preapproved_reply",
            estimated_minor=5, period_spend_minor=96,
        ))
        currency = self.service.evaluate(self.request(
            request_id="cost_3", idempotency_key="cost_3", action="send_preapproved_reply",
            currency="EUR",
        ))
        self.assertEqual("per_action_budget_exceeded", per_action.reason_code)
        self.assertEqual("period_budget_exceeded", period.reason_code)
        self.assertEqual(PolicyDecision.DENY, currency.decision)
        self.assertTrue(all(not result.permits_execution for result in (per_action, period, currency)))

    def test_pause_resume_revoke_and_stale_version_behavior(self):
        paused = self.service.pause(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant",
            expected_version=1, authority=BILLY_FOUNDER_AUTHORITY, idempotency_key="pause_1",
        )
        self.assertEqual(RoleState.PAUSED, paused.state)
        self.assertIs(paused, self.service.pause(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant",
            expected_version=1, authority=BILLY_FOUNDER_AUTHORITY, idempotency_key="pause_1",
        ))
        denied = self.service.evaluate(self.request())
        self.assertEqual("role_paused", denied.reason_code)
        resumed = self.service.resume(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant",
            expected_version=1, authority=BILLY_FOUNDER_AUTHORITY, idempotency_key="resume_1",
        )
        self.assertEqual(RoleState.ACTIVE, resumed.state)
        revoked = self.service.revoke(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant",
            expected_version=1, authority=BILLY_FOUNDER_AUTHORITY, idempotency_key="revoke_1",
        )
        self.assertEqual(RoleState.REVOKED, revoked.state)
        with self.assertRaises(PermissionError):
            self.service.resume(
                BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant",
                expected_version=1, authority=BILLY_FOUNDER_AUTHORITY, idempotency_key="resume_after_revoke",
            )

    def test_lifecycle_idempotency_key_conflict_fails(self):
        self.service.pause(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant",
            expected_version=1, authority=BILLY_FOUNDER_AUTHORITY, idempotency_key="lifecycle_same",
        )
        with self.assertRaises(IdempotencyConflict):
            self.service.resume(
                BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant",
                expected_version=1, authority=BILLY_FOUNDER_AUTHORITY, idempotency_key="lifecycle_same",
            )

    def test_new_version_supersedes_old_and_old_requests_fail_closed(self):
        old = self.repository.current_definition(BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant")
        new = replace(old, version=2, objective=old.objective + " Version two.")
        revised = self.service.revise_role(new, expected_version=1, authority=BILLY_FOUNDER_AUTHORITY, idempotency_key="revise_2")
        self.assertEqual(old.authority_epoch + 1, revised.authority_epoch)
        stale = self.service.evaluate(self.request())
        current = self.service.evaluate(self.request(
            request_id="request_v2", idempotency_key="idem_v2", role_version=2
        ))
        self.assertEqual("role_superseded", stale.reason_code)
        self.assertEqual(PolicyDecision.ALLOW, current.decision)
        with self.assertRaises(ValueError):
            self.service.revise_role(replace(new, version=3), expected_version=1, authority=BILLY_FOUNDER_AUTHORITY, idempotency_key="stale_revision")

    def test_cached_allow_fails_closed_after_pause_or_revoke(self):
        request = self.request()
        self.assertTrue(self.service.evaluate(request).permits_execution)
        self.service.pause(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant",
            expected_version=1, authority=BILLY_FOUNDER_AUTHORITY,
            idempotency_key="pause-after-allow",
        )
        with self.assertRaises(StalePolicyAuthority):
            self.service.evaluate(request)
        self.service.resume(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant",
            expected_version=1, authority=BILLY_FOUNDER_AUTHORITY,
            idempotency_key="resume-after-allow",
        )
        with self.assertRaises(StalePolicyAuthority):
            self.service.evaluate(request)
        self.service.revoke(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant",
            expected_version=1, authority=BILLY_FOUNDER_AUTHORITY,
            idempotency_key="revoke-after-allow",
        )
        with self.assertRaises(StalePolicyAuthority):
            self.service.evaluate(request)

    def test_new_evaluation_after_resume_uses_new_authority_epoch(self):
        old_request = self.request()
        old = self.service.evaluate(old_request)
        self.service.pause(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant",
            expected_version=1, authority=BILLY_FOUNDER_AUTHORITY,
            idempotency_key="epoch-pause",
        )
        resumed = self.service.resume(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant",
            expected_version=1, authority=BILLY_FOUNDER_AUTHORITY,
            idempotency_key="epoch-resume",
        )
        self.assertGreater(resumed.authority_epoch, old.authority_epoch)
        fresh = self.service.evaluate(self.request(
            request_id="fresh-after-resume", idempotency_key="fresh-after-resume",
        ))
        self.assertEqual(PolicyDecision.ALLOW, fresh.decision)
        self.assertEqual(resumed.authority_epoch, fresh.authority_epoch)

    def test_cached_allow_fails_closed_after_version_superseded(self):
        request = self.request()
        self.assertTrue(self.service.evaluate(request).permits_execution)
        role = self.repository.current_definition(
            BILLY_TENANT_ID, BILLY_COMPANY_ID, "role_inbox_assistant"
        )
        self.service.revise_role(
            replace(role, version=2, objective=role.objective + " Revised."),
            expected_version=1, authority=BILLY_FOUNDER_AUTHORITY,
            idempotency_key="revise-after-allow",
        )
        with self.assertRaises(StalePolicyAuthority):
            self.service.evaluate(request)


class IdempotencyAuditProjectionTests(WorkforceTestCase):
    def test_evaluation_is_idempotent_and_conflicting_reuse_is_rejected(self):
        first = self.service.evaluate(self.request())
        second = self.service.evaluate(self.request())
        self.assertIs(first, second)
        with self.assertRaises(IdempotencyConflict):
            self.service.evaluate(self.request(action="archive_message"))
        policy_audits = [a for a in self.repository.list_audit(BILLY_TENANT_ID, BILLY_COMPANY_ID) if a.action == "workforce.policy.evaluated"]
        self.assertEqual(1, len(policy_audits))

    def test_audit_and_projection_are_tenant_scoped(self):
        self.service.evaluate(self.request())
        projection = self.repository.projection(BILLY_TENANT_ID, BILLY_COMPANY_ID)
        other = self.repository.projection("tenant_other", BILLY_COMPANY_ID)
        self.assertEqual(4, len(projection["roles"]))
        self.assertEqual(1, projection["decision_counts"]["allow"])
        self.assertEqual(1, projection["evaluation_count"])
        self.assertEqual(0, other["evaluation_count"])
        self.assertTrue(all(item.append_only for item in self.repository.list_audit(BILLY_TENANT_ID, BILLY_COMPANY_ID)))


if __name__ == "__main__":
    unittest.main()
