from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import staging_server

from businessbuilder.commercial import (
    CommercialService,
    InMemoryCommercialRepository,
    ProductCode,
    RecordingCommercialEventSink,
    seed_default_catalog,
)
from businessbuilder.commercial.pricing import OfferCode
from businessbuilder.commercial.stripe_webhooks import StripeWebhookIngress
from tests.commercial.test_supervised_checkout import provider
from businessbuilder.access_broker import InMemorySecretStore
from businessbuilder.provider_connection import ProviderConnectionService, SandboxEmailProvider, SandboxScopedKeyProvider
from businessbuilder.identity import AuthorizationPolicy
from businessbuilder.company_brain import (
    Company,
    CompanyBrainService,
    EntityRef,
    LifecycleState,
    Provenance,
    Scope,
    SQLiteCompanyBrainRepository,
)
from businessbuilder.customer_api import CustomerApi
from businessbuilder.identity import (
    AuthorizationContext,
    FakeDevAuthenticationProvider,
    IdentityService,
    Permission,
    PrincipalContextAuthority,
    Role,
    SessionService,
    SQLiteIdentityRepository,
    UserStatus,
)
from businessbuilder.integration import (
    CompanyBrainVerificationAdapter,
    IdentityApprovalPrincipalVerifier,
)
from businessbuilder.runtime.capabilities import CapabilityRegistry
from businessbuilder.runtime.fakes import FakeCapability
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.runtime.models import ApprovalMode, Budget, Money
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.runtime.ports import FakeCompanyStateReader, RecordingVerificationPort
from businessbuilder.runtime.storage import SQLiteRuntimeRepository
from businessbuilder.verification import (
    InMemoryVerificationRepository,
    ReadinessEvaluator,
    VerificationService,
    billy_bob_policy,
    default_registry,
)


NOW = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)


class CustomerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.ids = DeterministicIds()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.identity_repository = SQLiteIdentityRepository(str(root / "identity.sqlite"))
        self.identity = IdentityService(
            self.identity_repository, id_factory=self.ids, clock=lambda: self.now
        )
        self.auth_provider = FakeDevAuthenticationProvider()
        self.sessions = SessionService(
            self.identity_repository,
            self.auth_provider,
            id_factory=self.ids,
            clock=lambda: self.now,
        )
        self.authority = PrincipalContextAuthority(
            self.identity_repository,
            clock=lambda: self.now,
            signing_key=b"customer-api-test-key-at-least-32-bytes",
        )

        self.owner, _ = self.identity.register_founder("owner@example.test", "Owner")
        self.tenant, self.organization, _ = self.identity.create_account(
            self.owner.user_id, "Customer Organization"
        )
        self.company_id = "company_customer_api"
        self.owner_context = AuthorizationContext(
            self.owner.user_id, self.tenant.tenant_id, self.company_id
        )
        self.identity.attach_company(
            AuthorizationContext(self.owner.user_id, self.tenant.tenant_id),
            self.company_id,
        )
        self.owner_token = self._token(self.owner.user_id, self.owner.email)

        self.brain_repository = SQLiteCompanyBrainRepository(str(root / "brain.sqlite"))
        self.brain_repository.migrate()
        self.brain = CompanyBrainService(self.brain_repository)
        self._create_brain_company(
            self.tenant.tenant_id, self.company_id, self.owner.user_id,
            lifecycle=LifecycleState.ASSEMBLY,
        )
        self.runtime_repository = SQLiteRuntimeRepository(str(root / "runtime.sqlite"))
        registry = CapabilityRegistry()
        registry.register(FakeCapability("fake.customer.api", estimate_minor=1))
        self.runtime = JobOrchestrator(
            repository=self.runtime_repository,
            registry=registry,
            company_reader=FakeCompanyStateReader({(self.tenant.tenant_id, self.company_id)}),
            verification=RecordingVerificationPort(),
            id_factory=self.ids,
            clock=lambda: self.now,
            approval_principals=IdentityApprovalPrincipalVerifier(self.authority),
        )
        self.runtime.budgets.create(
            Budget("budget_customer_api", self.tenant.tenant_id, self.company_id, Money("USD", 100)),
            "correlation-api",
        )
        self.verification_repository = InMemoryVerificationRepository()
        self.verification = VerificationService(
            self.verification_repository, default_registry()
        )
        self.snapshots = CompanyBrainVerificationAdapter(self.brain)
        self.commercial_repository = InMemoryCommercialRepository()
        seed_default_catalog(self.commercial_repository, effective_at=self.now)
        self.commercial = CommercialService(
            self.commercial_repository,
            self.identity.authorization,
            RecordingCommercialEventSink(),
            id_factory=self.ids,
            clock=lambda: self.now,
            auto_dispatch_outbox=False,
        )
        self.api = CustomerApi(
            identity_repository=self.identity_repository,
            principal_authority=self.authority,
            company_brain=self.brain,
            runtime=self.runtime,
            runtime_repository=self.runtime_repository,
            verification=self.verification,
            readiness=ReadinessEvaluator(self.verification_repository, billy_bob_policy()),
            company_snapshots=self.snapshots,
            commercial=self.commercial,
            commercial_repository=self.commercial_repository,
            id_factory=self.ids,
            clock=lambda: self.now,
        )
        self.secret_store = InMemorySecretStore()
        self.email_provider = SandboxEmailProvider(clock=lambda: self.now)
        self.key_provider = SandboxScopedKeyProvider()
        lifecycle_broker = Mock()
        lifecycle_broker.register_external_account.side_effect = lambda value, actor_id: self.runtime_repository.save_broker_record(
            "external_account", value.account_id, value.tenant_id, value.company_id, value)
        lifecycle_broker.register_connection.side_effect = lambda value, actor_id: self.runtime_repository.save_broker_record(
            "provider_connection", value.connection_id, value.tenant_id, value.company_id, value)
        lifecycle_broker.register_credential_ref.side_effect = lambda value, actor_id: self.runtime_repository.save_broker_record(
            "external_credential_ref", value.secret_ref, value.tenant_id, value.company_id, value)
        lifecycle_broker.register_capability_grant.side_effect = lambda value, actor_id: self.runtime_repository.save_broker_record(
            "capability_grant", value.grant_id, value.tenant_id, value.company_id, value)
        def revoke(tenant_id, company_id, connection_id, *, status, reason, actor_id):
            del actor_id
            current = self.runtime_repository.get_broker_record("provider_connection", tenant_id, company_id, connection_id)
            changed = replace(current, status=status, revoked_reason=reason, updated_at=self.now)
            self.runtime_repository.save_broker_record("provider_connection", connection_id, tenant_id, company_id, changed)
            return changed
        lifecycle_broker.revoke_connection.side_effect = revoke
        self.provider_connections = ProviderConnectionService(
            repository=self.runtime_repository, principal_authority=self.authority,
            authorization=AuthorizationPolicy(self.identity_repository), broker=lifecycle_broker,
            secret_store=self.secret_store,
            providers={self.email_provider.provider: self.email_provider},
            scoped_key_providers={self.key_provider.provider: self.key_provider},
            company_brain=self.brain,
            audit=self.runtime.audit, clock=lambda: self.now, id_factory=self.ids,
        )
        self.api.provider_connections = self.provider_connections

    def tearDown(self) -> None:
        self.runtime_repository.close()
        self.brain_repository.close()
        self.identity_repository.close()
        self.temporary.cleanup()

    def _token(self, user_id, email, *, lifetime=timedelta(hours=1)):
        proof = f"proof-{user_id}"
        self.auth_provider.register(user_id, email, proof)
        return self.sessions.sign_in(email, proof, lifetime=lifetime)[1]

    def _member(self, role: Role, name: str):
        user = self.identity.register_user(f"{name}@example.test")
        invited = self.identity.invite_member(
            self.owner_context, user.user_id, role, reason="API role test"
        )
        self.identity.accept_membership(invited.membership_id, user.user_id)
        return user, self._token(user.user_id, user.email)

    def _create_brain_company(self, tenant_id, company_id, owner_id, *, lifecycle):
        stamp = self.now.isoformat().replace("+00:00", "Z")
        owner_ref = EntityRef("user", owner_id)
        return self.brain.create_company(
            Company(
                Scope(tenant_id, company_id),
                "Customer API Company",
                "mobile_service",
                {"country": "US", "region": "TX"},
                (owner_ref,),
                lifecycle=lifecycle,
                provenance=(Provenance("test", stamp, owner_ref, source_ref="test://customer-api"),),
            )
        )

    def request(self, method, path, *, token=None, body=None, query=None, headers=None):
        supplied = dict(headers or {})
        if token:
            supplied["Authorization"] = f"Bearer {token}"
        return self.api.handle(
            method=method,
            path=path,
            headers=supplied,
            query=query or {},
            body=body,
            request_id="req_customer_api_test",
            correlation_id="corr_customer_api_test",
        )

    def test_identity_routes_require_active_authentication(self) -> None:
        self.assertEqual(401, self.request("GET", "/api/v1/me").status)
        response = self.request("GET", "/api/v1/me", token=self.owner_token)
        self.assertEqual(200, response.status)
        self.assertEqual(self.owner.user_id, response.body["user"]["user_id"])
        self.assertNotIn("token", str(response.body).lower())

        expired = self._token(self.owner.user_id, self.owner.email, lifetime=timedelta(seconds=1))
        self.now += timedelta(seconds=2)
        self.assertEqual(401, self.request("GET", "/api/v1/me", token=expired).status)

    def test_deactivated_user_suspended_organization_and_missing_membership_deny(self) -> None:
        member, token = self._member(Role.MEMBER, "inactive")
        self.identity_repository.save_user(
            replace(member, status=UserStatus.DEACTIVATED, deactivated_at=self.now)
        )
        self.assertEqual(401, self.request("GET", "/api/v1/me", token=token).status)

        self.identity.suspend_organization(self.owner_context, "security test")
        self.assertEqual(
            403,
            self.request("GET", f"/api/v1/companies/{self.company_id}", token=self.owner_token).status,
        )

        outsider = self.identity.register_user("outsider@example.test")
        outsider_token = self._token(outsider.user_id, outsider.email)
        self.assertEqual(
            404,
            self.request("GET", f"/api/v1/companies/{self.company_id}", token=outsider_token).status,
        )

    def test_forged_authority_inputs_are_rejected_and_audited(self) -> None:
        for header in ("X-Actor-Role", "X-Tenant-ID", "X-Company-ID"):
            with self.subTest(header=header):
                response = self.request(
                    "GET",
                    f"/api/v1/companies/{self.company_id}",
                    token=self.owner_token,
                    headers={header: "owner" if "Role" in header else "forged"},
                )
                self.assertEqual(403, response.status)
        forged_body = self.request(
            "POST",
            "/api/v1/orders",
            token=self.owner_token,
            body={
                "company_id": self.company_id,
                "product_version_id": f"product_version_{ProductCode.BUILD_BUSINESS.value.lower()}_v1",
                "role": "owner",
            },
        )
        self.assertEqual(400, forged_body.status)
        self.assertTrue(
            any(item.action == "authorization.denied" for item in self.identity_repository.list_audit("unresolved"))
        )

    def test_direct_object_and_cross_tenant_company_attacks_fail_closed(self) -> None:
        second_owner, _ = self.identity.register_founder("second@example.test", "Second")
        second_tenant, _, _ = self.identity.create_account(second_owner.user_id, "Second Org")
        second_company = "company_second_customer"
        self.identity.attach_company(
            AuthorizationContext(second_owner.user_id, second_tenant.tenant_id), second_company
        )
        self._create_brain_company(second_tenant.tenant_id, second_company, second_owner.user_id, lifecycle=LifecycleState.ASSEMBLY)
        response = self.request("GET", f"/api/v1/companies/{second_company}", token=self.owner_token)
        self.assertEqual(404, response.status)
        build_room_attack = self.request(
            "GET", f"/api/v1/companies/{second_company}/build-room", token=self.owner_token
        )
        self.assertEqual(404, build_room_attack.status)
        order = self.commercial.create_order(
            self.owner_context,
            f"product_version_{ProductCode.BUILD_BUSINESS.value.lower()}_v1",
        )
        wrong_scope = self.request(
            "GET",
            f"/api/v1/orders/{order.order_id}",
            token=self.owner_token,
            query={"company_id": [second_company]},
        )
        self.assertNotEqual(200, wrong_scope.status)

    def test_owner_admin_member_commercial_boundaries(self) -> None:
        admin, admin_token = self._member(Role.ADMIN, "admin")
        member, member_token = self._member(Role.MEMBER, "member")
        for token in (self.owner_token, admin_token, member_token):
            self.assertEqual(200, self.request("GET", f"/api/v1/companies/{self.company_id}", token=token).status)
        product = f"product_version_{ProductCode.BUILD_BUSINESS.value.lower()}_v1"
        created = self.request(
            "POST", "/api/v1/orders", token=self.owner_token,
            body={"company_id": self.company_id, "product_version_id": product},
        )
        self.assertEqual(201, created.status)
        self.assertEqual(
            403,
            self.request("POST", "/api/v1/orders", token=admin_token, body={"company_id": self.company_id, "product_version_id": product}).status,
        )
        self.assertEqual(
            200,
            self.request("GET", "/api/v1/orders", token=admin_token, query={"company_id": [self.company_id]}).status,
        )
        self.assertEqual(
            403,
            self.request("GET", "/api/v1/orders", token=member_token, query={"company_id": [self.company_id]}).status,
        )

    def test_supervised_pricing_and_payment_routes_do_not_accept_browser_authority(self) -> None:
        pricing = self.request("GET", "/api/v1/pricing")
        self.assertEqual(200, pricing.status)
        existing = next(item for item in pricing.body["offers"] if item["offer_code"] == "existing_business_run_v1")
        self.assertEqual("quote_required", existing["price_kind"])
        self.assertIsNone(existing["upfront_minor"])
        order = self.commercial.create_order(
            self.owner_context, "product_version_build_business_v1", offer_code=OfferCode.BUSINESS,
        )
        stripe, _ = provider()
        self.api.payment_provider = stripe
        self.api.payment_webhooks = StripeWebhookIngress(self.commercial_repository, self.commercial, stripe)
        self.api.checkout_success_url = "https://pilot.example.test/success"
        self.api.checkout_cancel_url = "https://pilot.example.test/cancel"
        path = f"/api/v1/orders/{order.order_id}/checkout"
        selector = {"company_id": [self.company_id]}
        delayed = self.request("POST", path, token=self.owner_token, query=selector,
                               body={"idempotency_key": "founder-checkout-retry-0001"})
        self.assertEqual(409, delayed.status)
        self.assertFalse(self.commercial_repository.get_current_entitlement_grants(self.tenant.tenant_id, self.company_id))
        forged = self.request("POST", path, token=self.owner_token, query=selector,
                              headers={"X-Tenant-ID": "tenant_other"},
                              body={"idempotency_key": "founder-checkout-retry-0001"})
        self.assertEqual(403, forged.status)
        invalid_webhook = self.request(
            "POST", "/api/v1/payment-webhooks/stripe", body=b"{}",
            headers={"Stripe-Signature": "t=0,v1=forged"},
        )
        self.assertEqual(400, invalid_webhook.status)
        self.assertFalse(self.commercial_repository.get_current_entitlement_grants(self.tenant.tenant_id, self.company_id))

    def test_complete_customer_route_inventory_uses_safe_projections(self) -> None:
        for path, key in (
            ("/api/v1/organizations", "organizations"),
            ("/api/v1/memberships", "memberships"),
            ("/api/v1/companies", "companies"),
            (f"/api/v1/companies/{self.company_id}", "company"),
            (f"/api/v1/companies/{self.company_id}/founder-actions", "founder_actions"),
            (f"/api/v1/companies/{self.company_id}/readiness", "readiness"),
            (f"/api/v1/companies/{self.company_id}/handoff", "handoff"),
            ("/api/v1/subscriptions", "subscriptions"),
            ("/api/v1/entitlements", "entitlements"),
        ):
            with self.subTest(path=path):
                query = {"company_id": [self.company_id]} if path in {
                    "/api/v1/subscriptions", "/api/v1/entitlements"
                } else {}
                response = self.request("GET", path, token=self.owner_token, query=query)
                self.assertEqual(200, response.status)
                self.assertIn(key, response.body)

        created = self.request(
            "POST",
            "/api/v1/companies",
            token=self.owner_token,
            body={
                "display_name": "Second Customer Company",
                "archetype": "mobile_service",
                "jurisdiction": {"country": "US", "region": "TX"},
                "organization_id": self.organization.organization_id,
            },
        )
        self.assertEqual(201, created.status)
        self.assertNotIn("tenant", str(created.body).lower())

        product = f"product_version_{ProductCode.BUILD_BUSINESS.value.lower()}_v1"
        order = self.request(
            "POST", "/api/v1/orders", token=self.owner_token,
            body={"company_id": self.company_id, "product_version_id": product},
        ).body["order"]
        detail = self.request(
            "GET",
            f"/api/v1/orders/{order['order_id']}",
            token=self.owner_token,
            query={"company_id": [self.company_id]},
        )
        self.assertEqual(200, detail.status)
        handoff = self.request(
            "POST",
            f"/api/v1/companies/{self.company_id}/handoff",
            token=self.owner_token,
            body={},
        )
        self.assertEqual(409, handoff.status)
        self.assertEqual("handoff_not_available", handoff.body["error"])

    def test_support_requires_scoped_unexpired_session_and_has_no_privileged_bypass(self) -> None:
        support, token = self._member(Role.SUPPORT, "support")
        without = self.request("GET", f"/api/v1/companies/{self.company_id}", token=token)
        self.assertEqual(403, without.status)
        grant = self.identity.grant_support_access(
            self.owner_context,
            support.user_id,
            frozenset({Permission.VIEW_COMPANY_STATE, Permission.ACCESS_ARTIFACTS}),
            timedelta(minutes=5),
            "customer support test",
            company_id=self.company_id,
        )
        support_session = self.identity.start_support_session(
            support.user_id, grant.grant_id, "customer support session"
        )
        headers = {"X-Support-Impersonation-Session": support_session.impersonation_session_id}
        self.assertEqual(
            200,
            self.request("GET", f"/api/v1/companies/{self.company_id}", token=token, headers=headers).status,
        )
        self.assertEqual(
            403,
            self.request("GET", "/api/v1/orders", token=token, query={"company_id": [self.company_id]}, headers=headers).status,
        )
        self.assertEqual(
            403,
            self.request("POST", f"/api/v1/companies/{self.company_id}/handoff", token=token, body={}, headers=headers).status,
        )
        self.now += timedelta(minutes=6)
        self.assertEqual(
            403,
            self.request("GET", f"/api/v1/companies/{self.company_id}", token=token, headers=headers).status,
        )

    def test_approval_route_uses_trusted_principal_only(self) -> None:
        job = self.runtime.create_job(
            tenant_id=self.tenant.tenant_id,
            company_id=self.company_id,
            capability="fake.customer.api",
            inputs={"objective": "trusted API approval"},
            budget_ref="budget_customer_api",
            per_job_ceiling=Money("USD", 1),
            idempotency_key="customer-api-approval-0001",
            correlation_id="correlation-customer-api",
            approval_mode=ApprovalMode.FOUNDER_ONLY,
        )
        forged = self.request(
            "POST",
            f"/api/v1/companies/{self.company_id}/approvals/{job.approval_ids[0]}",
            token=self.owner_token,
            body={"decision": "granted", "actor_role": "founder"},
        )
        self.assertEqual(400, forged.status)
        approved = self.request(
            "POST",
            f"/api/v1/companies/{self.company_id}/approvals/{job.approval_ids[0]}",
            token=self.owner_token,
            body={"decision": "granted"},
        )
        self.assertEqual(200, approved.status)
        self.assertEqual(self.owner.user_id, approved.body["approval"]["decided_by"])

    def test_admin_cannot_use_customer_api_to_grant_founder_approval(self) -> None:
        _, admin_token = self._member(Role.ADMIN, "approval-admin")
        job = self.runtime.create_job(
            tenant_id=self.tenant.tenant_id,
            company_id=self.company_id,
            capability="fake.customer.api",
            inputs={"objective": "founder-only API boundary"},
            budget_ref="budget_customer_api",
            per_job_ceiling=Money("USD", 1),
            idempotency_key="customer-api-admin-approval-0001",
            correlation_id="correlation-customer-api-admin",
            approval_mode=ApprovalMode.FOUNDER_ONLY,
        )
        denied = self.request(
            "POST",
            f"/api/v1/companies/{self.company_id}/approvals/{job.approval_ids[0]}",
            token=admin_token,
            body={"decision": "granted"},
        )
        self.assertEqual(403, denied.status)
        audit = self.runtime_repository.list_audit(self.tenant.tenant_id, self.company_id)
        self.assertTrue(any(item["action"] == "authorization.denied" for item in audit))

    def test_build_room_is_tenant_scoped_and_readiness_is_verification_only(self) -> None:
        company = self.brain.get_company(Scope(self.tenant.tenant_id, self.company_id))
        self.brain_repository.save_company(
            replace(company, readiness="fully_set", version=company.version + 1),
            expected_version=company.version,
        )
        readiness = self.request(
            "GET", f"/api/v1/companies/{self.company_id}/readiness", token=self.owner_token
        )
        self.assertEqual("verification", readiness.body["readiness"]["authority"])
        self.assertFalse(readiness.body["readiness"]["ready"])
        build_room = self.request(
            "GET", f"/api/v1/companies/{self.company_id}/build-room", token=self.owner_token
        )
        self.assertEqual(200, build_room.status)
        serialized = str(build_room.body).lower()
        for forbidden in (
            "tenant_id", "token_digest", "provider_ref", "subject_digest", "provenance"
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertFalse(build_room.body["build_room"]["readiness"]["ready"])
        self.assertEqual([], build_room.body["build_room"]["commercial"]["subscriptions"])

    def test_denial_audit_is_durable_and_contains_no_request_payload(self) -> None:
        self.request(
            "GET", f"/api/v1/companies/company_not_owned", token=self.owner_token
        )
        self.identity_repository.close()
        reopened = SQLiteIdentityRepository(str(Path(self.temporary.name) / "identity.sqlite"))
        denied = reopened.list_audit("unresolved")
        self.assertTrue(denied)
        event = denied[-1]
        self.assertEqual("authorization.denied", event.action)
        self.assertEqual("businessbuilder.customer_api", event.source)
        self.assertIn("request_id", event.metadata)
        self.assertNotIn("authorization", str(event.metadata).lower())
        reopened.close()
        self.identity_repository = SQLiteIdentityRepository(str(Path(self.temporary.name) / "identity.sqlite"))

    def test_http_transport_preserves_hardening_and_request_correlation_ids(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), staging_server.StagingHandler)
        server.customer_api = self.api
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection(*server.server_address, timeout=2)
            connection.request(
                "GET",
                "/api/v1/me",
                headers={
                    "Authorization": f"Bearer {self.owner_token}",
                    "X-Request-ID": "req-http-safe",
                    "X-Correlation-ID": "corr-http-safe",
                    "Origin": "https://attacker.invalid",
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            headers = dict(response.getheaders())
            connection.close()
            self.assertEqual(200, response.status)
            self.assertIn("user", payload)
            self.assertEqual("req-http-safe", headers["X-Request-ID"])
            self.assertEqual("corr-http-safe", headers["X-Correlation-ID"])
            self.assertEqual("DENY", headers["X-Frame-Options"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)

            connection = HTTPConnection(*server.server_address, timeout=2)
            connection.request(
                "PUT", f"/api/v1/companies/{self.company_id}", body=b"{}",
                headers={"Authorization": f"Bearer {self.owner_token}", "Content-Type": "application/json"},
            )
            denied = connection.getresponse()
            denied.read()
            allow = denied.getheader("Allow")
            connection.close()
            self.assertEqual(405, denied.status)
            self.assertEqual("GET, HEAD, OPTIONS", allow)

            connection = HTTPConnection(*server.server_address, timeout=2)
            connection.request(
                "POST", "/api/v1/orders", body=b"{bad json",
                headers={"Authorization": f"Bearer {self.owner_token}", "Content-Type": "application/json"},
            )
            malformed = connection.getresponse()
            malformed_payload = json.loads(malformed.read())
            connection.close()
            self.assertEqual(400, malformed.status)
            self.assertEqual("invalid_json", malformed_payload["error"])
        finally:
            server.shutdown()
            server.server_close()

    def test_provider_connection_api_is_customer_safe_and_lifecycle_complete(self) -> None:
        path = f"/api/v1/companies/{self.company_id}/provider-connections"
        started = self.request("POST", path, token=self.owner_token, body={
            "provider": "sandbox-email",
            "redirect_uri": "https://app.example.test/oauth/callback",
            "scopes": ["mail.read", "mail.send"],
        })
        self.assertEqual(201, started.status)
        authorization = started.body["authorization"]
        query = parse_qs(urlparse(authorization["authorization_url"]).query)
        code = self.email_provider.issue_test_code(
            code_challenge=query["code_challenge"][0],
            redirect_uri="https://app.example.test/oauth/callback",
            scopes=frozenset({"mail.read", "mail.send"}),
        )
        callback = self.request(
            "POST", "/api/v1/provider-connections/oauth/callback",
            token=self.owner_token,
            body={"state": query["state"][0], "code": code,
                  "pkce_verifier": authorization["pkce_verifier"],
                  "redirect_uri": "https://app.example.test/oauth/callback"},
        )
        self.assertEqual(200, callback.status)
        connection_id = callback.body["provider_connection"]["provider_connection_id"]
        serialized = json.dumps(callback.body).lower()
        for forbidden in ("secret_ref", "secret_locator", "access_token", "refresh_token", "arn:aws", "tenant_id"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(1, len(self.request("GET", path, token=self.owner_token).body["provider_connections"]))
        connections_path = f"/api/v1/companies/{self.company_id}/connections"
        connections = self.request("GET", connections_path, token=self.owner_token)
        self.assertEqual(200, connections.status)
        self.assertEqual("HEALTHY", connections.body["connections"][0]["health"])
        self.assertNotIn("secret_ref", json.dumps(connections.body))
        room = self.request("GET", f"/api/v1/companies/{self.company_id}/build-room", token=self.owner_token)
        self.assertEqual("EMAIL", room.body["build_room"]["connections"][0]["capability"])
        self.assertEqual("runtime", room.body["build_room"]["ai_usage"]["authority"])
        self.assertEqual(401, self.request("GET", connections_path).status)
        self.assertEqual(405, self.request("POST", connections_path, token=self.owner_token, body={"health": "HEALTHY"}).status)
        status_path = f"{path}/{connection_id}"
        self.assertEqual(200, self.request("GET", status_path, token=self.owner_token).status)
        disconnected = self.request("POST", status_path + "/disconnect", token=self.owner_token, body={})
        self.assertEqual("disconnected", disconnected.body["provider_connection"]["status"])
        reconnect = self.request("POST", status_path + "/reconnect", token=self.owner_token,
            body={"redirect_uri": "https://app.example.test/oauth/callback", "scopes": ["mail.read"]})
        self.assertEqual(200, reconnect.status)

    def test_scoped_key_api_never_returns_credential_and_rejects_client_health_forgery(self) -> None:
        key = self.key_provider.issue_test_key().decode()
        command_key = "connection-command-customer-api-001"
        path = f"/api/v1/companies/{self.company_id}/scoped-key-connections"
        self.assertEqual(401, self.request("POST", path, body={"provider": self.key_provider.provider, "credential": key,
            "idempotency_key": command_key}).status)
        created = self.request("POST", path, token=self.owner_token,
            body={"provider": self.key_provider.provider, "credential": key,
                  "idempotency_key": command_key})
        self.assertEqual(201, created.status)
        self.assertEqual("CRM", created.body["provider_connection"]["capability"])
        self.assertNotIn(key, json.dumps(created.body))
        self.assertNotIn("secret_ref", json.dumps(created.body))
        duplicate = self.request("POST", path, token=self.owner_token,
            body={"provider": self.key_provider.provider, "credential": key,
                  "idempotency_key": command_key})
        self.assertEqual(created.body["provider_connection"]["provider_connection_id"],
                         duplicate.body["provider_connection"]["provider_connection_id"])
        forged = self.request("POST", path, token=self.owner_token,
            body={"provider": self.key_provider.provider, "credential": key,
                  "idempotency_key": command_key, "health": "HEALTHY"})
        self.assertEqual(400, forged.status)
        self.assertNotIn(key, json.dumps(forged.body))

        connection_id = created.body["provider_connection"]["provider_connection_id"]
        self.provider_connections.record_operational_signal(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            connection_id=connection_id, error_code="billing_required", usable=False,
            action_required="top_up",
        )
        room = self.request("GET", f"/api/v1/companies/{self.company_id}/build-room", token=self.owner_token)
        actions = [item for item in room.body["build_room"]["founder_actions"]
                   if item["founder_action_id"].startswith("founder_action_connection_")]
        self.assertEqual(1, len(actions))
        self.assertEqual("prepared", actions[0]["state"])
        self.assertEqual(["provider_authorization_result"], actions[0]["required_evidence_kinds"])
        self.provider_connections.record_operational_signal(
            tenant_id=self.tenant.tenant_id, company_id=self.company_id,
            connection_id=connection_id, error_code=None, usable=True,
        )
        latest = self.request("GET", f"/api/v1/companies/{self.company_id}/founder-actions", token=self.owner_token)
        self.assertEqual("prepared", [item for item in latest.body["founder_actions"]
            if item["founder_action_id"] == actions[0]["founder_action_id"]][0]["state"])

    def test_provider_api_auth_scope_methods_and_member_boundary(self) -> None:
        path = f"/api/v1/companies/{self.company_id}/provider-connections"
        self.assertEqual(401, self.request("GET", path).status)
        _, token = self._member(Role.MEMBER, "provider-member")
        self.assertEqual(403, self.request("POST", path, token=token, body={
            "provider": "sandbox-email",
            "redirect_uri": "https://app.example.test/oauth/callback",
            "scopes": ["mail.read"],
        }).status)
        self.assertEqual(404, self.request("GET",
            "/api/v1/companies/company_not_owned/provider-connections", token=self.owner_token).status)
        self.assertEqual(405, self.request("PUT", path, token=self.owner_token, body={}).status)


if __name__ == "__main__":
    unittest.main()
