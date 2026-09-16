from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import uuid4

from businessbuilder.company_brain import (
    Company,
    CompanyBrainService,
    EntityRef,
    LifecycleState,
    Provenance,
    Scope,
)
from businessbuilder.customer_api.bootstrap import create_postgres_customer_api
from businessbuilder.identity import (
    AuthorizationContext,
    FakeDevAuthenticationProvider,
    IdentityService,
    Permission,
    Role,
    SessionService,
)
from businessbuilder.postgres import (
    PostgresCompanyBrainRepository,
    PostgresIdentityRepository,
)
from businessbuilder.runtime.ids import DeterministicIds


NOW = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)


@unittest.skipUnless(
    os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"),
    "requires an isolated PostgreSQL test database",
)
class PostgresCustomerApiTests(unittest.TestCase):
    def test_authenticated_scope_and_denial_audit_survive_restart(self) -> None:
        dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        schema = f"{prefix}_api_{uuid4().hex[:10]}"
        ids = DeterministicIds()

        identity_repository = PostgresIdentityRepository(dsn, schema=schema)
        identity = IdentityService(
            identity_repository, id_factory=ids, clock=lambda: NOW
        )
        owner, _ = identity.register_founder(
            "postgres-customer-api@example.test", "PostgreSQL API"
        )
        tenant, _, _ = identity.create_account(owner.user_id, "PostgreSQL API")
        company_id = "company_postgres_customer_api"
        identity.attach_company(
            AuthorizationContext(owner.user_id, tenant.tenant_id), company_id
        )
        provider = FakeDevAuthenticationProvider()
        provider.register(owner.user_id, owner.email, "offline-proof")
        sessions = SessionService(
            identity_repository, provider, id_factory=ids, clock=lambda: NOW
        )
        _, token = sessions.sign_in(owner.email, "offline-proof")

        brain_repository = PostgresCompanyBrainRepository(dsn, schema=schema)
        brain = CompanyBrainService(brain_repository)
        owner_ref = EntityRef("user", owner.user_id)
        brain.create_company(
            Company(
                Scope(tenant.tenant_id, company_id),
                "PostgreSQL Customer API",
                "mobile_service",
                {"country": "US", "region": "TX"},
                (owner_ref,),
                lifecycle=LifecycleState.ASSEMBLY,
                provenance=(
                    Provenance(
                        "test",
                        NOW.isoformat().replace("+00:00", "Z"),
                        owner_ref,
                        source_ref="test://postgres-customer-api",
                    ),
                ),
            )
        )
        identity_repository.close()
        brain_repository.close()

        application = create_postgres_customer_api(
            signing_key=b"postgres-customer-api-key-32-bytes-minimum",
            dsn=dsn,
            schema=schema,
            clock=lambda: NOW,
        )
        headers = {"Authorization": f"Bearer {token}"}
        me = application.handle(
            method="GET", path="/api/v1/me", headers=headers, query={}, body=None,
            request_id="request-test", correlation_id="correlation-test",
        )
        company = application.handle(
            method="GET", path=f"/api/v1/companies/{company_id}", headers=headers,
            query={}, body=None, request_id="req-postgres-company",
            correlation_id="corr-postgres-company",
        )
        readiness = application.handle(
            method="GET", path=f"/api/v1/companies/{company_id}/readiness",
            headers=headers, query={}, body=None, request_id="req-postgres-ready",
            correlation_id="corr-postgres-ready",
        )
        denied = application.handle(
            method="GET", path="/api/v1/companies/company_not_owned",
            headers=headers, query={}, body=None, request_id="req-postgres-denied",
            correlation_id="corr-postgres-denied",
        )
        self.assertEqual((200, 200, 200, 404), (me.status, company.status, readiness.status, denied.status))
        self.assertEqual("verification", readiness.body["readiness"]["authority"])
        application.close()

        reopened = PostgresIdentityRepository(dsn, schema=schema)
        audit = reopened.list_audit("unresolved")
        self.assertTrue(any(item.action == "authorization.denied" for item in audit))
        reopened.close()


    def test_operator_preview_is_read_only_scoped_and_durably_audited(self) -> None:
        dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        schema = f"{prefix}_preview_{uuid4().hex[:10]}"
        ids = DeterministicIds()

        identity_repository = PostgresIdentityRepository(dsn, schema=schema)
        identity = IdentityService(identity_repository, id_factory=ids, clock=lambda: NOW)
        owner, _ = identity.register_founder("pg-preview-owner@example.test", "Preview Owner")
        tenant, _, _ = identity.create_account(owner.user_id, "Preview Org")
        context = AuthorizationContext(owner.user_id, tenant.tenant_id)
        company_id = "company_postgres_preview"
        identity.attach_company(context, company_id)

        operator = identity.register_user("pg-preview-operator@example.test")
        invited = identity.invite_member(context, operator.user_id, Role.SUPPORT, reason="operator preview")
        identity.accept_membership(invited.membership_id, operator.user_id)
        grant = identity.grant_support_access(
            context,
            operator.user_id,
            frozenset({Permission.VIEW_COMPANY_STATE, Permission.ACCESS_ARTIFACTS}),
            timedelta(hours=1),
            "owner-approved operator preview",
            company_id=company_id,
        )
        support_session = identity.start_support_session(
            operator.user_id, grant.grant_id, "inspect the founder dashboard"
        )

        provider = FakeDevAuthenticationProvider()
        provider.register(operator.user_id, operator.email, "offline-proof")
        sessions = SessionService(identity_repository, provider, id_factory=ids, clock=lambda: NOW)
        _, operator_token = sessions.sign_in(operator.email, "offline-proof")

        brain_repository = PostgresCompanyBrainRepository(dsn, schema=schema)
        brain = CompanyBrainService(brain_repository)
        owner_ref = EntityRef("user", owner.user_id)
        brain.create_company(
            Company(
                Scope(tenant.tenant_id, company_id),
                "PostgreSQL Preview Company",
                "mobile_service",
                {"country": "US", "region": "TX"},
                (owner_ref,),
                lifecycle=LifecycleState.ASSEMBLY,
                provenance=(
                    Provenance(
                        "test",
                        NOW.isoformat().replace("+00:00", "Z"),
                        owner_ref,
                        source_ref="test://postgres-preview",
                    ),
                ),
            )
        )
        identity_repository.close()
        brain_repository.close()

        application = create_postgres_customer_api(
            signing_key=b"postgres-customer-api-key-32-bytes-minimum",
            dsn=dsn,
            schema=schema,
            clock=lambda: NOW,
        )
        headers = {
            "Authorization": f"Bearer {operator_token}",
            "X-Support-Impersonation-Session": support_session.impersonation_session_id,
        }

        def call(method, path, **kwargs):
            return application.handle(
                method=method, path=path, headers=headers, query=kwargs.get("query", {}),
                body=kwargs.get("body"), request_id="req-pg-preview",
                correlation_id="corr-pg-preview",
            )

        grants = call("GET", "/api/v1/operator/support-grants")
        companies = call("GET", "/api/v1/operator/companies")
        preview = call("GET", f"/api/v1/operator/companies/{company_id}/dashboard-preview")
        mutation = call("POST", f"/api/v1/operator/companies/{company_id}/dashboard-preview", body={})
        other_company = call("GET", "/api/v1/operator/companies/company_not_in_scope/dashboard-preview")

        self.assertEqual((200, 200, 200, 405, 403), (
            grants.status, companies.status, preview.status, mutation.status, other_company.status,
        ))
        self.assertEqual([grant.grant_id], [item["grant_id"] for item in grants.body["support_grants"]])
        self.assertEqual([company_id], [item["company_id"] for item in companies.body["companies"]])
        body = preview.body["operator_preview"]
        self.assertEqual("ADMIN PREVIEW", body["label"])
        self.assertTrue(body["read_only"])
        self.assertFalse(body["impersonation"])
        self.assertEqual("support", body["viewer"]["role"])
        self.assertNotIn(tenant.tenant_id, str(preview.body))
        application.close()

        reopened = PostgresIdentityRepository(dsn, schema=schema)
        try:
            actions = [item.action for item in reopened.list_audit(tenant.tenant_id)]
            self.assertIn("operator_preview.grants_listed", actions)
            self.assertIn("operator_preview.companies_listed", actions)
            self.assertIn("operator_preview.opened", actions)
            opened = next(
                item for item in reopened.list_audit(tenant.tenant_id)
                if item.action == "operator_preview.opened"
            )
            self.assertEqual(operator.user_id, opened.actor_user_id)
            self.assertEqual(company_id, opened.company_id)
            self.assertEqual(grant.grant_id, opened.metadata["grant_id"])
            self.assertEqual(
                LifecycleState.ASSEMBLY,
                CompanyBrainService(
                    PostgresCompanyBrainRepository(dsn, schema=schema)
                ).get_company(Scope(tenant.tenant_id, company_id)).lifecycle,
            )
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
