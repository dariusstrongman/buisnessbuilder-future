from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from businessbuilder.identity import (
    AuthorizationContext,
    FakeDevAuthenticationProvider,
    IdentityService,
    InMemoryIdentityRepository,
    Permission,
    PrincipalContextAuthority,
    Role,
    SessionService,
    UserStatus,
)
from businessbuilder.integration import IdentityApprovalPrincipalVerifier
from businessbuilder.runtime.capabilities import CapabilityRegistry
from businessbuilder.runtime.fakes import FakeCapability
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.runtime.models import ApprovalMode, Budget, Money
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.runtime.ports import FakeCompanyStateReader, RecordingVerificationPort
from businessbuilder.runtime.storage import SQLiteRuntimeRepository


START = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)


class RuntimeTrustBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = START
        self.ids = DeterministicIds()
        self.identity_repository = InMemoryIdentityRepository()
        self.identity = IdentityService(
            self.identity_repository, id_factory=self.ids, clock=lambda: self.now
        )
        self.owner, _ = self.identity.register_founder(
            "owner@example.test", "Fixture Owner"
        )
        self.tenant, self.organization, _ = self.identity.create_account(
            self.owner.user_id, "Fixture Organization"
        )
        self.company_id = "company_runtime_trust"
        self.owner_context = AuthorizationContext(
            self.owner.user_id, self.tenant.tenant_id, self.company_id
        )
        self.identity.attach_company(
            AuthorizationContext(self.owner.user_id, self.tenant.tenant_id),
            self.company_id,
        )
        self.authentication = FakeDevAuthenticationProvider()
        self.sessions = SessionService(
            self.identity_repository,
            self.authentication,
            id_factory=self.ids,
            clock=lambda: self.now,
        )
        self.authority = PrincipalContextAuthority(
            self.identity_repository,
            clock=lambda: self.now,
            signing_key=b"runtime-trust-test-key-not-a-secret",
        )
        self.owner_principal = self._principal(
            self.owner.user_id, self.owner.email, self.tenant.tenant_id, self.company_id
        )

        self.temporary = tempfile.TemporaryDirectory()
        self.runtime_path = str(Path(self.temporary.name) / "runtime.sqlite")
        self.runtime_repository = SQLiteRuntimeRepository(self.runtime_path)
        self.companies = FakeCompanyStateReader(
            {(self.tenant.tenant_id, self.company_id)}
        )
        registry = CapabilityRegistry()
        registry.register(FakeCapability("fake.secure.action", estimate_minor=1))
        self.runtime = JobOrchestrator(
            repository=self.runtime_repository,
            registry=registry,
            company_reader=self.companies,
            verification=RecordingVerificationPort(),
            id_factory=self.ids,
            clock=lambda: self.now,
            approval_principals=IdentityApprovalPrincipalVerifier(self.authority),
        )
        self.runtime.budgets.create(
            Budget(
                "budget_runtime_trust",
                self.tenant.tenant_id,
                self.company_id,
                Money("USD", 100),
            ),
            "correlation_runtime_trust",
        )
        self.job_count = 0

    def tearDown(self) -> None:
        self.runtime_repository.close()
        self.temporary.cleanup()

    def _principal(self, user_id, email, tenant_id, company_id, **kwargs):
        assertion = f"proof-{user_id}"
        self.authentication.register(user_id, email, assertion)
        _, raw_token = self.sessions.sign_in(email, assertion)
        return self.authority.issue(
            raw_token,
            tenant_id=tenant_id,
            company_id=company_id,
            **kwargs,
        )

    def _add_member(self, email: str, role: Role):
        user = self.identity.register_user(email)
        invitation = self.identity.invite_member(self.owner_context, user.user_id, role)
        self.identity.accept_membership(invitation.membership_id, user.user_id)
        return user

    def _job(self, *, mode=ApprovalMode.FOUNDER_ONLY, tenant_id=None, company_id=None):
        self.job_count += 1
        tenant_id = tenant_id or self.tenant.tenant_id
        company_id = company_id or self.company_id
        return self.runtime.create_job(
            tenant_id=tenant_id,
            company_id=company_id,
            capability="fake.secure.action",
            inputs={"objective": "test trusted approval"},
            budget_ref="budget_runtime_trust",
            per_job_ceiling=Money("USD", 1),
            idempotency_key=f"runtime-trust-boundary-{self.job_count:04d}",
            correlation_id=f"correlation-trust-{self.job_count}",
            approval_mode=mode,
        )

    def _approve(self, job, principal):
        return self.runtime.approve_job(
            tenant_id=job.tenant_id,
            company_id=job.company_id,
            job_id=job.job_id,
            approval_id=job.approval_ids[0],
            principal=principal,
        )

    def _assert_denied_and_audited(self, job, principal) -> None:
        with self.assertRaises(PermissionError):
            self._approve(job, principal)
        records = self.runtime_repository.list_audit(job.tenant_id, job.company_id)
        denied = [item for item in records if item["action"] == "authorization.denied"]
        self.assertTrue(denied)
        self.assertEqual("runtime.approval.decide", denied[-1]["permission"])
        self.assertEqual("businessbuilder.runtime.approvals", denied[-1]["source"])
        self.assertEqual(job.company_id, denied[-1]["company_id"])

    def test_founder_approval_uses_repository_derived_identity(self) -> None:
        approved = self._approve(self._job(), self.owner_principal)
        self.assertEqual("runnable", approved.status.value)
        approval = self.runtime_repository.get_approval(
            approved.tenant_id, approved.company_id, approved.approval_ids[0]
        )
        self.assertEqual(self.owner.user_id, approval.decided_by)
        self.assertEqual("founder", approval.decided_by_role)

    def test_forged_actor_role_invalidates_signed_principal(self) -> None:
        admin = self._add_member("admin@example.test", Role.ADMIN)
        principal = self._principal(
            admin.user_id, admin.email, self.tenant.tenant_id, self.company_id
        )
        forged = replace(principal, role=Role.OWNER)
        self._assert_denied_and_audited(self._job(), forged)

    def test_forged_tenant_and_company_contexts_are_rejected(self) -> None:
        job = self._job()
        self._assert_denied_and_audited(
            job, replace(self.owner_principal, company_id="company_forged")
        )
        second = self._job()
        self._assert_denied_and_audited(
            second, replace(self.owner_principal, tenant_id="tenant_forged")
        )

    def test_deactivated_user_is_revalidated_at_decision_time(self) -> None:
        admin = self._add_member("disabled@example.test", Role.ADMIN)
        principal = self._principal(
            admin.user_id, admin.email, self.tenant.tenant_id, self.company_id
        )
        self.identity_repository.save_user(
            replace(admin, status=UserStatus.DEACTIVATED, deactivated_at=self.now)
        )
        self._assert_denied_and_audited(
            self._job(mode=ApprovalMode.APPROVAL_REQUIRED), principal
        )

    def test_owner_admin_member_and_support_boundaries_are_derived(self) -> None:
        admin = self._add_member("boundary-admin@example.test", Role.ADMIN)
        admin_principal = self._principal(
            admin.user_id, admin.email, self.tenant.tenant_id, self.company_id
        )
        approved = self._approve(
            self._job(mode=ApprovalMode.APPROVAL_REQUIRED), admin_principal
        )
        self.assertEqual("runnable", approved.status.value)

        member = self._add_member("boundary-member@example.test", Role.MEMBER)
        member_principal = self._principal(
            member.user_id, member.email, self.tenant.tenant_id, self.company_id
        )
        self._assert_denied_and_audited(
            self._job(mode=ApprovalMode.APPROVAL_REQUIRED), member_principal
        )

        support = self._add_member("boundary-support@example.test", Role.SUPPORT)
        grant = self.identity.grant_support_access(
            self.owner_context,
            support.user_id,
            frozenset({Permission.VIEW_COMPANY_STATE}),
            timedelta(minutes=5),
            "active support boundary test",
            company_id=self.company_id,
        )
        support_session = self.identity.start_support_session(
            support.user_id, grant.grant_id, "active diagnostic session"
        )
        support_principal = self._principal(
            support.user_id,
            support.email,
            self.tenant.tenant_id,
            self.company_id,
            support_impersonation_session_id=support_session.impersonation_session_id,
        )
        self._assert_denied_and_audited(
            self._job(mode=ApprovalMode.APPROVAL_REQUIRED), support_principal
        )

    def test_suspended_organization_is_revalidated_at_decision_time(self) -> None:
        job = self._job()
        self.identity.suspend_organization(
            self.owner_context, "security response test"
        )
        self._assert_denied_and_audited(job, self.owner_principal)

    def test_expired_support_impersonation_is_rejected(self) -> None:
        support = self._add_member("support@example.test", Role.SUPPORT)
        grant = self.identity.grant_support_access(
            self.owner_context,
            support.user_id,
            frozenset({Permission.VIEW_COMPANY_STATE}),
            timedelta(seconds=1),
            "time-boxed support test",
            company_id=self.company_id,
        )
        support_session = self.identity.start_support_session(
            support.user_id, grant.grant_id, "diagnostic session"
        )
        principal = self._principal(
            support.user_id,
            support.email,
            self.tenant.tenant_id,
            self.company_id,
            support_impersonation_session_id=support_session.impersonation_session_id,
        )
        self.now += timedelta(seconds=2)
        self._assert_denied_and_audited(
            self._job(mode=ApprovalMode.APPROVAL_REQUIRED), principal
        )

    def test_cross_tenant_approval_is_rejected(self) -> None:
        second_owner, _ = self.identity.register_founder(
            "second@example.test", "Second Owner"
        )
        second_tenant, _, _ = self.identity.create_account(
            second_owner.user_id, "Second Organization"
        )
        second_company = "company_second_tenant"
        self.identity.attach_company(
            AuthorizationContext(second_owner.user_id, second_tenant.tenant_id),
            second_company,
        )
        self.companies.add(second_tenant.tenant_id, second_company)
        job = self._job(tenant_id=second_tenant.tenant_id, company_id=second_company)
        self._assert_denied_and_audited(job, self.owner_principal)

    def test_unsigned_context_is_rejected_and_denial_survives_restart(self) -> None:
        job = self._job()
        self._assert_denied_and_audited(
            job,
            {
                "user_id": self.owner.user_id,
                "tenant_id": self.tenant.tenant_id,
                "company_id": self.company_id,
                "role": "owner",
            },
        )
        self.runtime_repository.close()
        reopened = SQLiteRuntimeRepository(self.runtime_path)
        records = reopened.list_audit(job.tenant_id, job.company_id)
        self.assertTrue(any(item["action"] == "authorization.denied" for item in records))
        reopened.close()
        self.runtime_repository = SQLiteRuntimeRepository(self.runtime_path)
