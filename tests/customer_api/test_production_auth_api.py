from __future__ import annotations

from datetime import datetime, timezone

from businessbuilder.identity import (
    AuthorizationDenied,
    Permission,
    ProductionFounderSessionService,
    Role,
    VerifiedExternalIdentity,
)

from . import test_customer_api as customer_api_tests


class Provider:
    name = "cognito"

    def verify_access_token(self, token: str) -> VerifiedExternalIdentity:
        if token == "verified-support-token-value-long-enough-for-validation":
            return VerifiedExternalIdentity(
                "cognito", "support-subject", "support@example.test", True,
                "Support", datetime(2026, 9, 14, tzinfo=timezone.utc), "provider-session-s",
            )
        if token == "unverified-provider-token-value-long-enough-for-validation":
            return VerifiedExternalIdentity(
                "cognito", "unverified", "waiting@example.test", False,
                "Waiting", datetime(2026, 9, 14, tzinfo=timezone.utc), "provider-session-u",
            )
        if token != "verified-provider-token-value-long-enough-for-validation":
            raise AuthorizationDenied("invalid assertion")
        return VerifiedExternalIdentity(
            "cognito", "new-founder", "new-founder@example.test", True,
            "New Founder", datetime(2026, 9, 14, tzinfo=timezone.utc), "provider-session-v",
        )


class TestProductionAuthApi:
    def setup_method(self) -> None:
        self.fixture = customer_api_tests.CustomerApiTests(methodName="runTest")
        self.fixture.setUp()
        self.fixture.api.founder_authentication = ProductionFounderSessionService(
            self.fixture.identity_repository,
            Provider(),
            id_factory=self.fixture.ids,
            clock=lambda: self.fixture.now,
        )

    def teardown_method(self) -> None:
        self.fixture.tearDown()

    def request(self, path, *, body, authorization=None, headers=None):
        values = dict(headers or {})
        if authorization:
            values["Authorization"] = f"Bearer {authorization}"
        return self.fixture.api.handle(
            method="POST", path=path, headers=values, query={}, body=body,
            request_id="request-auth", correlation_id="correlation-auth",
        )

    def test_session_establishment_rotation_and_revocation(self) -> None:
        created = self.request(
            "/api/v1/auth/sessions",
            body={"access_token": "verified-provider-token-value-long-enough-for-validation"},
        )
        assert created.status == 201
        assert created.body["user"]["email_verified"] is True
        raw = created.body["session_token"]

        rotated = self.request(
            "/api/v1/auth/sessions/rotate",
            authorization=raw,
            body={"access_token": "verified-provider-token-value-long-enough-for-validation"},
        )
        assert rotated.status == 200
        replacement = rotated.body["session_token"]
        denied = self.fixture.api.handle(
            method="GET", path="/api/v1/me", headers={"Authorization": f"Bearer {raw}"},
            query={}, body=None, request_id="old", correlation_id="old",
        )
        assert denied.status == 401

        revoked = self.request(
            "/api/v1/auth/sessions/revoke", authorization=replacement, body={}
        )
        assert revoked.status == 200
        replay = self.fixture.api.handle(
            method="GET", path="/api/v1/me",
            headers={"Authorization": f"Bearer {replacement}"}, query={}, body=None,
            request_id="replay", correlation_id="replay",
        )
        assert replay.status == 401

    def test_unverified_and_forged_authority_are_denied(self) -> None:
        unverified = self.request(
            "/api/v1/auth/sessions",
            body={"access_token": "unverified-provider-token-value-long-enough-for-validation"},
        )
        assert unverified.status == 401
        forged = self.request(
            "/api/v1/auth/sessions",
            body={"access_token": "verified-provider-token-value-long-enough-for-validation"},
            headers={"X-Actor-Role": "support", "X-Tenant-ID": "tenant-forged"},
        )
        assert forged.status == 403

    def test_duplicate_session_exchange_reuses_identity_without_creating_authority(self) -> None:
        first = self.request(
            "/api/v1/auth/sessions",
            body={"access_token": "verified-provider-token-value-long-enough-for-validation"},
        )
        second = self.request(
            "/api/v1/auth/sessions",
            body={"access_token": "verified-provider-token-value-long-enough-for-validation"},
        )
        assert first.body["user"]["user_id"] == second.body["user"]["user_id"]
        user_id = first.body["user"]["user_id"]
        assert self.fixture.identity_repository.list_user_memberships(user_id) == ()
        assert all(
            membership.role.value != "support"
            for membership in self.fixture.identity_repository.list_user_memberships(user_id)
        )

    def test_support_session_requires_existing_role_scoped_grant_and_expiry(self) -> None:
        from datetime import timedelta

        support, _ = self.fixture._member(Role.SUPPORT, "support")
        signed_in = self.request(
            "/api/v1/auth/sessions",
            body={"access_token": "verified-support-token-value-long-enough-for-validation"},
        )
        raw = signed_in.body["session_token"]
        denied = self.request(
            "/api/v1/auth/support-sessions", authorization=raw,
            body={"grant_id": "support_grant_forged", "reason": "review"},
        )
        assert denied.status == 404
        grant = self.fixture.identity.grant_support_access(
            self.fixture.owner_context, support.user_id,
            frozenset({Permission.VIEW_COMPANY_STATE, Permission.ACCESS_ARTIFACTS}),
            timedelta(minutes=5), "operator review", company_id=self.fixture.company_id,
        )
        active = self.request(
            "/api/v1/auth/support-sessions", authorization=raw,
            body={"grant_id": grant.grant_id, "reason": "review evidence"},
        )
        assert active.status == 201
        assert active.body["company_id"] == self.fixture.company_id
        self.fixture.now += timedelta(minutes=6)
        expired = self.request(
            "/api/v1/auth/support-sessions", authorization=raw,
            body={"grant_id": grant.grant_id, "reason": "stale retry"},
        )
        assert expired.status in {401, 403}

    def test_founder_grant_route_derives_owner_and_narrows_company_scope(self) -> None:
        support, support_token = self.fixture._member(Role.SUPPORT, "support")
        path = (
            f"/api/v1/companies/{self.fixture.company_id}/"
            "residential-cleaning-pilot/support-grants"
        )
        body = {
            "support_user_id": support.user_id,
            "reason": "Review pilot evidence with my consent",
            "duration_minutes": 60,
        }
        forged = self.request(
            path, authorization=self.fixture.owner_token,
            headers={"X-Tenant-ID": "tenant_forged", "X-Actor-Role": "owner"},
            body=body,
        )
        assert forged.status == 403
        denied_support = self.request(path, authorization=support_token, body=body)
        assert denied_support.status in {403, 404}
        wrong_company = self.request(
            "/api/v1/companies/company_other_tenant/residential-cleaning-pilot/support-grants",
            authorization=self.fixture.owner_token, body=body,
        )
        assert wrong_company.status in {403, 404}
        forged_permissions = self.request(
            path, authorization=self.fixture.owner_token,
            body={**body, "permissions": ["billing.view", "founder_decision.approve"]},
        )
        assert forged_permissions.status == 400
        granted = self.request(path, authorization=self.fixture.owner_token, body=body)
        assert granted.status == 201
        assert granted.body["permissions"] == ["artifacts.access", "company.view"]
        persisted = self.fixture.identity_repository.get_support_grant(granted.body["grant_id"])
        assert persisted.approved_by_user_id == self.fixture.owner.user_id
        assert persisted.company_id == self.fixture.company_id
        assert persisted.support_user_id == support.user_id
        assert Permission.APPROVE_FOUNDER_DECISIONS not in persisted.permissions
        assert Permission.VIEW_BILLING not in persisted.permissions
        assert any(
            item.action == "authorization.denied"
            and item.target_id == Permission.REQUEST_SUPPORT.value
            for item in self.fixture.identity_repository.audit_events
        )
