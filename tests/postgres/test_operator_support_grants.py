"""Durable proof that operator grant discovery stays per-operator and per-tenant.

The operator preview lets an operator find the grants issued *to them*. That
listing has to survive a process restart and must never widen across operators
or tenants when it is rebuilt from PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import uuid4

from businessbuilder.identity import (
    AuthorizationContext,
    IdentityService,
    Permission,
    Role,
)
from businessbuilder.postgres import PostgresIdentityRepository
from businessbuilder.runtime.ids import DeterministicIds


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
READ_ONLY = frozenset({Permission.VIEW_COMPANY_STATE, Permission.ACCESS_ARTIFACTS})


@unittest.skipUnless(os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"), "requires isolated PostgreSQL")
class PostgresOperatorSupportGrantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        self.schema = f"{prefix}_opgrants_{uuid4().hex[:10]}"
        self.ids = DeterministicIds()

    def _service(self, repository):
        return IdentityService(repository, id_factory=self.ids, clock=lambda: NOW)

    def _tenant_with_operator(self, identity, owner_email, org_name, company_id, operator_email):
        owner, _ = identity.register_founder(owner_email, "Owner")
        tenant, _, _ = identity.create_account(owner.user_id, org_name)
        context = AuthorizationContext(owner.user_id, tenant.tenant_id)
        identity.attach_company(context, company_id)
        operator = identity.register_user(operator_email)
        invited = identity.invite_member(context, operator.user_id, Role.SUPPORT, reason="operator preview")
        identity.accept_membership(invited.membership_id, operator.user_id)
        grant = identity.grant_support_access(
            context, operator.user_id, READ_ONLY, timedelta(hours=1),
            "owner-approved operator preview", company_id=company_id,
        )
        return operator, grant

    def test_grant_listing_is_per_operator_and_survives_a_restart(self) -> None:
        repository = PostgresIdentityRepository(self.dsn, schema=self.schema)
        identity = self._service(repository)

        first_operator, first_grant = self._tenant_with_operator(
            identity, "owner-a@example.test", "Org A", "company_pg_op_a", "operator-a@example.test",
        )
        second_operator, second_grant = self._tenant_with_operator(
            identity, "owner-b@example.test", "Org B", "company_pg_op_b", "operator-b@example.test",
        )
        repository.close()

        reopened = PostgresIdentityRepository(self.dsn, schema=self.schema)
        try:
            mine = reopened.list_support_grants_for_user(first_operator.user_id)
            theirs = reopened.list_support_grants_for_user(second_operator.user_id)
            self.assertEqual([first_grant.grant_id], [item.grant_id for item in mine])
            self.assertEqual([second_grant.grant_id], [item.grant_id for item in theirs])
            self.assertEqual("company_pg_op_a", mine[0].company_id)
            self.assertTrue(mine[0].active_at(NOW))
            self.assertEqual((), reopened.list_support_grants_for_user("user_does_not_exist"))
        finally:
            reopened.close()

    def test_expired_grants_are_still_listed_but_report_themselves_inactive(self) -> None:
        repository = PostgresIdentityRepository(self.dsn, schema=self.schema)
        identity = self._service(repository)
        operator, grant = self._tenant_with_operator(
            identity, "owner-c@example.test", "Org C", "company_pg_op_c", "operator-c@example.test",
        )
        repository.close()

        reopened = PostgresIdentityRepository(self.dsn, schema=self.schema)
        try:
            listed = reopened.list_support_grants_for_user(operator.user_id)
            self.assertEqual([grant.grant_id], [item.grant_id for item in listed])
            self.assertFalse(listed[0].active_at(NOW + timedelta(hours=2)))
        finally:
            reopened.close()
