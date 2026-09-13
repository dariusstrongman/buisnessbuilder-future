from __future__ import annotations

from datetime import datetime, timezone
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


if __name__ == "__main__":
    unittest.main()
