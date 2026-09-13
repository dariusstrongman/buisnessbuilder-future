from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from businessbuilder.identity import (
    AuthorizationContext,
    AuthorizationDenied,
    FakeDevAuthenticationProvider,
    IdentityService,
    InMemoryIdentityRepository,
    InvalidIdentityTransition,
    MembershipStatus,
    OrganizationStatus,
    Permission,
    Role,
    SQLiteIdentityRepository,
    SessionService,
    TenantStatus,
    UserStatus,
)
from businessbuilder.runtime.ids import DeterministicIds


NOW = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


class IdentityAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = InMemoryIdentityRepository()
        self.ids = DeterministicIds()
        self.clock = MutableClock()
        self.service = IdentityService(self.repo, id_factory=self.ids, clock=self.clock)
        self.owner, self.profile = self.service.register_founder("Billy@example.com", "Billy Bob")
        self.tenant, self.organization, self.owner_membership = self.service.create_account(self.owner.user_id, "Billy Bob Ventures")
        self.context = AuthorizationContext(self.owner.user_id, self.tenant.tenant_id, "co_billy")
        self.service.attach_company(AuthorizationContext(self.owner.user_id, self.tenant.tenant_id), "co_billy")

    def add_member(self, email: str, role: Role):
        user = self.service.register_user(email)
        invited = self.service.invite_member(self.context, user.user_id, role)
        return user, self.service.accept_membership(invited.membership_id, user.user_id)

    def test_identity_founder_tenant_organization_and_company_are_distinct(self):
        self.assertNotEqual(self.owner.user_id, self.profile.founder_profile_id)
        self.assertNotEqual(self.tenant.tenant_id, self.organization.organization_id)
        self.assertEqual(("co_billy",), self.repo.get_organization_by_tenant(self.tenant.tenant_id).company_ids)
        second = self.service.attach_company(self.context, "co_billy_second")
        self.assertEqual({"co_billy", "co_billy_second"}, set(second.company_ids))

    def test_member_invitation_acceptance_removal_and_audit(self):
        user, membership = self.add_member("worker@example.com", Role.MEMBER)
        self.assertIsNone(self.repo.get_founder_profile_for_user(user.user_id))
        self.assertEqual(MembershipStatus.ACTIVE, membership.status)
        removed = self.service.remove_member(self.context, membership.membership_id, "Left team")
        self.assertEqual(MembershipStatus.REMOVED, removed.status)
        actions = [item.action for item in self.repo.list_audit(self.tenant.tenant_id)]
        self.assertIn("membership.invited", actions)
        self.assertIn("membership.accepted", actions)
        self.assertIn("membership.removed", actions)
        self.assertIsNone(self.repo.get_active_membership(self.tenant.tenant_id, user.user_id))

    def test_role_permissions_are_explicit_and_default_deny(self):
        admin, _ = self.add_member("admin@example.com", Role.ADMIN)
        member, _ = self.add_member("member@example.com", Role.MEMBER)
        admin_context = AuthorizationContext(admin.user_id, self.tenant.tenant_id, "co_billy")
        member_context = AuthorizationContext(member.user_id, self.tenant.tenant_id, "co_billy")
        self.assertTrue(self.service.authorization.allows(admin_context, Permission.MANAGE_MEMBERS, at=NOW))
        self.assertFalse(self.service.authorization.allows(admin_context, Permission.AUTHORIZE_SPEND, at=NOW))
        self.assertTrue(self.service.authorization.allows(member_context, Permission.INTERACT_AI_WORKFORCE, at=NOW))
        self.assertFalse(self.service.authorization.allows(member_context, Permission.VIEW_BILLING, at=NOW))

    def test_cross_tenant_and_unknown_company_fail_closed(self):
        stranger, _ = self.service.register_founder("stranger@example.com", "Stranger")
        other_tenant, _, _ = self.service.create_account(stranger.user_id, "Other")
        with self.assertRaises(AuthorizationDenied):
            self.service.authorization.require(
                AuthorizationContext(stranger.user_id, self.tenant.tenant_id, "co_billy"),
                Permission.VIEW_COMPANY_STATE,
                at=NOW,
            )
        with self.assertRaises(AuthorizationDenied):
            self.service.authorization.require(
                AuthorizationContext(self.owner.user_id, self.tenant.tenant_id, "co_not_owned"),
                Permission.VIEW_COMPANY_STATE,
                at=NOW,
            )
        self.assertNotEqual(other_tenant.tenant_id, self.tenant.tenant_id)

    def test_ownership_transfer_is_explicit_and_old_owner_cannot_spend(self):
        new_owner, membership = self.add_member("new-owner@example.com", Role.ADMIN)
        self.service.create_founder_profile(new_owner.user_id, "New Owner")
        changed = self.service.transfer_ownership(self.context, new_owner.user_id, "Founder-approved transfer")
        self.assertEqual(new_owner.user_id, changed.owner_user_id)
        self.assertEqual(Role.OWNER, self.repo.get_membership(membership.membership_id).role)
        self.assertFalse(self.service.authorization.allows(self.context, Permission.AUTHORIZE_SPEND, at=NOW))

    def test_owner_must_transfer_before_deactivation_and_suspension_denies_all(self):
        with self.assertRaises(InvalidIdentityTransition):
            self.service.deactivate_user(self.context, self.owner.user_id, "Close account")
        self.service.suspend_tenant(self.context, "Security review")
        self.assertEqual(TenantStatus.SUSPENDED, self.repo.get_tenant(self.tenant.tenant_id).status)
        self.assertFalse(self.service.authorization.allows(self.context, Permission.VIEW_COMPANY_STATE, at=NOW))

    def test_suspended_organization_denies_access_without_rewriting_tenant(self):
        changed = self.service.suspend_organization(self.context, "Commercial security review")
        self.assertEqual(OrganizationStatus.SUSPENDED, changed.status)
        self.assertEqual(TenantStatus.ACTIVE, self.repo.get_tenant(self.tenant.tenant_id).status)
        self.assertFalse(self.service.authorization.allows(self.context, Permission.VIEW_COMPANY_STATE, at=NOW))

    def test_role_change_is_bounded_and_audited(self):
        user, membership = self.add_member("promote@example.com", Role.MEMBER)
        changed = self.service.change_member_role(self.context, membership.membership_id, Role.ADMIN, "Team lead promotion")
        self.assertEqual(Role.ADMIN, changed.role)
        self.assertTrue(any(item.action == "membership.role_changed" for item in self.repo.list_audit(self.tenant.tenant_id)))
        with self.assertRaises(InvalidIdentityTransition):
            self.service.change_member_role(self.context, changed.membership_id, Role.OWNER, "Invalid ownership shortcut")

    def test_deactivated_non_owner_loses_access(self):
        user, _ = self.add_member("former@example.com", Role.MEMBER)
        member_context = AuthorizationContext(user.user_id, self.tenant.tenant_id, "co_billy")
        self.service.deactivate_user(self.context, user.user_id, "Employment ended")
        self.assertEqual(UserStatus.DEACTIVATED, self.repo.get_user(user.user_id).status)
        self.assertFalse(self.service.authorization.allows(member_context, Permission.VIEW_COMPANY_STATE, at=NOW))

    def test_support_requires_grant_session_scope_and_audits_every_action(self):
        support, _ = self.add_member("support@example.com", Role.SUPPORT)
        with self.assertRaises(AuthorizationDenied):
            self.service.authorization.require(
                AuthorizationContext(support.user_id, self.tenant.tenant_id, "co_billy"),
                Permission.VIEW_COMPANY_STATE,
                at=NOW,
            )
        grant = self.service.grant_support_access(
            self.context, support.user_id, frozenset({Permission.VIEW_COMPANY_STATE}),
            timedelta(hours=1), "Investigate display issue", company_id="co_billy",
        )
        session = self.service.start_support_session(support.user_id, grant.grant_id, "Ticket T-100")
        support_context = AuthorizationContext(support.user_id, self.tenant.tenant_id, "co_billy", session.impersonation_session_id)
        self.service.record_support_action(support_context, Permission.VIEW_COMPANY_STATE, "support.company_viewed", "company", "co_billy", "Inspect ticket T-100")
        self.assertTrue(self.service.authorization.allows(support_context, Permission.VIEW_COMPANY_STATE, at=NOW))
        self.assertFalse(self.service.authorization.allows(support_context, Permission.AUTHORIZE_SPEND, at=NOW))
        self.assertTrue(any(item.action == "support.company_viewed" and item.actor_user_id == support.user_id for item in self.repo.list_audit(self.tenant.tenant_id)))
        self.service.end_support_session(session.impersonation_session_id, support.user_id, "Ticket complete")
        self.assertFalse(self.service.authorization.allows(support_context, Permission.VIEW_COMPANY_STATE, at=NOW))

    def test_support_grant_cannot_include_protected_customer_authority(self):
        support, _ = self.add_member("support2@example.com", Role.SUPPORT)
        for permission in (
            Permission.APPROVE_FOUNDER_DECISIONS,
            Permission.AUTHORIZE_SPEND,
            Permission.CHANGE_SUBSCRIPTION,
            Permission.PERFORM_HANDOFF,
            Permission.INTERACT_AI_WORKFORCE,
        ):
            with self.assertRaises(AuthorizationDenied):
                self.service.grant_support_access(self.context, support.user_id, frozenset({permission}), timedelta(hours=1), "Not allowed", company_id="co_billy")


class SessionBoundaryTests(unittest.TestCase):
    def test_session_recovery_email_verification_and_no_raw_secret_storage(self):
        repo = InMemoryIdentityRepository()
        ids = DeterministicIds()
        clock = MutableClock()
        identity = IdentityService(repo, id_factory=ids, clock=clock)
        user, _ = identity.register_founder("billy@example.com", "Billy")
        provider = FakeDevAuthenticationProvider()
        provider.register(user.user_id, user.email, "dev-assertion")
        sessions = SessionService(repo, provider, id_factory=ids, clock=clock)
        session, raw = sessions.sign_in(user.email, "dev-assertion")
        self.assertNotEqual(raw, session.token_digest)
        self.assertEqual(user.user_id, sessions.validate_token(raw).user_id)
        sessions.sign_out(raw)
        with self.assertRaises(AuthorizationDenied):
            sessions.validate_token(raw)
        recovery = sessions.request_recovery(user.email)
        self.assertIsNotNone(recovery)
        self.assertNotIn(recovery, repr(repo.recoveries))
        sessions.complete_recovery(recovery, "new-assertion")
        challenge = sessions.begin_email_verification(user.user_id)
        verified = sessions.complete_email_verification(user.user_id, challenge, "offline-proof")
        self.assertEqual(NOW, verified.email_verified_at)

    def test_email_challenge_cannot_verify_a_different_user(self):
        repo = InMemoryIdentityRepository()
        ids = DeterministicIds()
        identity = IdentityService(repo, id_factory=ids, clock=lambda: NOW)
        first = identity.register_user("first@example.com")
        second = identity.register_user("second@example.com")
        provider = FakeDevAuthenticationProvider()
        sessions = SessionService(repo, provider, id_factory=ids, clock=lambda: NOW)
        challenge = sessions.begin_email_verification(first.user_id)
        with self.assertRaises(AuthorizationDenied):
            sessions.complete_email_verification(second.user_id, challenge, "proof")


class SQLiteIdentityTests(unittest.TestCase):
    def test_records_persist_and_audit_is_database_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "identity.sqlite")
            repo = SQLiteIdentityRepository(path)
            ids = DeterministicIds()
            service = IdentityService(repo, id_factory=ids, clock=lambda: NOW)
            user, _ = service.register_founder("persist@example.com", "Persist")
            tenant, _, _ = service.create_account(user.user_id, "Persistent Org")
            repo.close()
            reopened = SQLiteIdentityRepository(path)
            self.assertEqual("Persistent Org", reopened.get_organization_by_tenant(tenant.tenant_id).display_name)
            self.assertTrue(reopened.list_audit(tenant.tenant_id))
            with self.assertRaises(sqlite3.IntegrityError):
                reopened.connection.execute("DELETE FROM identity_audit_events")
            reopened.close()


if __name__ == "__main__":
    unittest.main()
