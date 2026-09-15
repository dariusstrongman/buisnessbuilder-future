from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import parse_qs, urlparse
import secrets
import tempfile
import unittest

from businessbuilder.access_broker import (
    ConnectionStatus, InMemorySecretStore, JobSecretRef,
)
from businessbuilder.access_broker.models import stable_id
from businessbuilder.identity import (
    AuthorizationContext, AuthorizationDenied, AuthorizationPolicy,
    FakeDevAuthenticationProvider, IdentityService, PrincipalContextAuthority,
    Role, SessionService, SQLiteIdentityRepository,
)
from businessbuilder.provider_connection import (
    OAuthFlowDenied, ProviderConnectionService, ProviderFailureClass,
    SandboxCalendarProvider, SandboxEmailProvider, SandboxScopedKeyProvider, classify_provider_failure,
)
from businessbuilder.provider_connection.connections import connection_state, safe_connection
from businessbuilder.provider_connection.models import ConnectionHealthState
from businessbuilder.runtime import SQLiteRuntimeRepository
from businessbuilder.runtime.audit import AuditLog
from businessbuilder.runtime.ids import DeterministicIds


NOW = datetime(2026, 9, 13, 22, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self): self.now = NOW
    def __call__(self): return self.now


class LifecycleBroker:
    def __init__(self, repository):
        self.repository = repository

    def register_external_account(self, value, *, actor_id):
        del actor_id
        self.repository.save_broker_record("external_account", value.account_id, value.tenant_id, value.company_id, value)

    def register_connection(self, value, *, actor_id):
        del actor_id
        self.repository.save_broker_record("provider_connection", value.connection_id, value.tenant_id, value.company_id, value)

    def register_credential_ref(self, value, *, actor_id):
        del actor_id
        self.repository.save_broker_record("external_credential_ref", value.secret_ref, value.tenant_id, value.company_id, value)

    def register_capability_grant(self, value, *, actor_id):
        del actor_id
        self.repository.save_broker_record("capability_grant", value.grant_id, value.tenant_id, value.company_id, value)

    def revoke_connection(self, tenant_id, company_id, connection_id, *, status, reason, actor_id):
        del actor_id
        current = self.repository.get_broker_record("provider_connection", tenant_id, company_id, connection_id)
        changed = replace(current, status=status, revoked_reason=reason, updated_at=NOW)
        self.repository.save_broker_record("provider_connection", connection_id, tenant_id, company_id, changed)
        return changed

    def _authorize_envelope(self, envelope):
        if envelope.expires_at <= NOW:
            raise PermissionError("expired")

    def _authorize_secret_record(self, envelope, reference, operation):
        del operation
        if (reference.tenant_id, reference.company_id) != (envelope.tenant_id, envelope.company_id):
            raise PermissionError("scope mismatch")
        credential = self.repository.get_broker_record("external_credential_ref", envelope.tenant_id, envelope.company_id, reference.secret_ref)
        connection = self.repository.get_broker_record("provider_connection", envelope.tenant_id, envelope.company_id, credential.connection_id)
        if connection.status is not ConnectionStatus.ACTIVE:
            raise PermissionError("connection inactive")
        return credential, connection


class Envelope:
    def __init__(self, tenant_id, company_id):
        self.tenant_id = tenant_id
        self.company_id = company_id
        self.capability = "communications.email"
        self.agent_role = "role_inbox_assistant"
        self.expires_at = NOW + timedelta(hours=1)


class ProviderConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.ids = DeterministicIds()
        self.identity_repo = SQLiteIdentityRepository(self.tmp.name + "/identity.sqlite")
        self.identity = IdentityService(self.identity_repo, id_factory=self.ids, clock=self.clock)
        self.owner, _ = self.identity.register_founder("oauth-owner@example.test", "OAuth Owner")
        self.tenant, self.organization, _ = self.identity.create_account(self.owner.user_id, "OAuth Org")
        self.company = "company_oauth_test"
        self.identity.attach_company(AuthorizationContext(self.owner.user_id, self.tenant.tenant_id), self.company)
        self.auth = FakeDevAuthenticationProvider()
        self.sessions = SessionService(self.identity_repo, self.auth, id_factory=self.ids, clock=self.clock)
        self.owner_token = self._token(self.owner.user_id, self.owner.email)
        self.authority = PrincipalContextAuthority(self.identity_repo, clock=self.clock,
            signing_key=secrets.token_bytes(32))
        self.principal = self.authority.issue(self.owner_token, tenant_id=self.tenant.tenant_id, company_id=self.company)
        self.repository = SQLiteRuntimeRepository(self.tmp.name + "/runtime.sqlite")
        self.store = InMemorySecretStore()
        self.provider = SandboxEmailProvider(clock=self.clock)
        self.calendar_provider = SandboxCalendarProvider(clock=self.clock)
        self.key_provider = SandboxScopedKeyProvider()
        self.broker = LifecycleBroker(self.repository)
        self.service = ProviderConnectionService(
            repository=self.repository, principal_authority=self.authority,
            authorization=AuthorizationPolicy(self.identity_repo), broker=self.broker,
            secret_store=self.store, providers={
                self.provider.provider: self.provider,
                self.calendar_provider.provider: self.calendar_provider,
            },
            scoped_key_providers={self.key_provider.provider: self.key_provider},
            audit=AuditLog(self.repository, self.ids, self.clock), clock=self.clock,
            id_factory=self.ids,
        )

    def tearDown(self):
        self.repository.close(); self.identity_repo.close(); self.tmp.cleanup()

    def _token(self, user_id, email):
        assertion = secrets.token_urlsafe(20)
        self.auth.register(user_id, email, assertion)
        return self.sessions.sign_in(email, assertion)[1]

    def _start(self, *, principal=None, scopes=frozenset({"mail.read", "mail.send"}), redirect="https://app.example.test/oauth/callback"):
        started = self.service.start(principal or self.principal, tenant_id=self.tenant.tenant_id,
            company_id=self.company, provider_name=self.provider.provider,
            redirect_uri=redirect, scopes=scopes)
        query = parse_qs(urlparse(started.authorization_url).query)
        return started, query

    def _complete(self, started, query, *, token=None, redirect="https://app.example.test/oauth/callback"):
        code = self.provider.issue_test_code(code_challenge=query["code_challenge"][0],
            redirect_uri=redirect, scopes=frozenset(query["scope"][0].split()))
        return self.service.complete(token or self.owner_token, state=query["state"][0], code=code,
            pkce_verifier=started.pkce_verifier, redirect_uri=redirect)

    def test_oauth_pkce_activation_and_domain_records_contain_references_only(self):
        started, query = self._start()
        connection = self._complete(started, query)
        self.assertIs(ConnectionStatus.ACTIVE, connection.status)
        self.assertEqual(frozenset({"mail.read", "mail.send"}), connection.scopes_granted)
        self.assertEqual(1, len(connection.secret_ref_ids))
        record_json = json.dumps([repr(connection), *map(repr, self.repository.list_broker_records(
            "external_credential_ref", self.tenant.tenant_id, self.company))])
        self.assertNotIn("access_token", record_json)
        self.assertNotIn("refresh_token", record_json)
        self.assertEqual(2, len(self.repository.list_broker_records("capability_grant", self.tenant.tenant_id, self.company)))

    def test_dashboard_health_is_durable_safe_and_worker_denies_degraded_connection(self):
        started, query = self._start()
        connection = self._complete(started, query)
        tenant = self.tenant.tenant_id
        healthy = self.service.dashboard_connections(self.principal, tenant_id=tenant, company_id=self.company)
        self.assertEqual("HEALTHY", healthy[0]["health"])
        self.assertEqual("EMAIL", healthy[0]["capability"])
        for forbidden in ("secret_ref", "secret_locator", "access_token", "refresh_token", "tenant_id"):
            self.assertNotIn(forbidden, json.dumps(healthy))
        signal = self.service.record_operational_signal(
            tenant_id=tenant, company_id=self.company, connection_id=connection.connection_id,
            error_code="quota_exceeded", usable=False, action_required="top_up",
        )
        self.assertIs(ConnectionHealthState.BILLING_OR_CREDITS, signal.state)
        degraded = self.service.dashboard_connections(self.principal, tenant_id=tenant, company_id=self.company)
        self.assertIn("billing or credits", degraded[0]["message"])
        self.assertNotIn("quota_exceeded", json.dumps(degraded))
        with self.assertRaises(PermissionError):
            self.service.refresh_for_job(
                Envelope(tenant, self.company),
                JobSecretRef(connection.secret_ref_ids[0], connection.provider, "communications.email", tenant, self.company),
                operation="read_message",
            )
        self.repository.close()
        self.repository = SQLiteRuntimeRepository(self.tmp.name + "/runtime.sqlite")
        self.service.repository = self.repository
        self.assertEqual("BILLING_OR_CREDITS", self.service.dashboard_connections(
            self.principal, tenant_id=tenant, company_id=self.company)[0]["health"])

    def test_quota_warning_requires_measured_value_and_explicit_threshold(self):
        started, query = self._start()
        connection = self._complete(started, query)
        tenant = self.tenant.tenant_id
        with self.assertRaises(ValueError):
            self.service.record_operational_signal(
                tenant_id=tenant, company_id=self.company, connection_id=connection.connection_id,
                error_code=None, usable=True, quota_warning_threshold=5,
            )
        self.service.record_operational_signal(
            tenant_id=tenant, company_id=self.company, connection_id=connection.connection_id,
            error_code=None, usable=True, quota_remaining=3, quota_unit="requests",
            quota_warning_threshold=5,
        )
        view = self.service.dashboard_connections(self.principal, tenant_id=tenant, company_id=self.company)[0]
        self.assertEqual("WARNING", view["health"])
        self.assertEqual(3, view["quota"]["remaining"])
        self.assertIsNone(safe_connection(connection, None, at=NOW)["quota"])
        self.assertIs(ConnectionHealthState.UNKNOWN, connection_state(connection, None, at=NOW))

    def test_expired_access_token_refreshes_but_revoked_grant_denies(self):
        started, query = self._start()
        connection = self._complete(started, query)
        tenant = self.tenant.tenant_id
        reference = JobSecretRef(connection.secret_ref_ids[0], connection.provider,
                                 "communications.email", tenant, self.company)
        self.clock.now = NOW + timedelta(minutes=21)
        self.principal = self.authority.issue(self.owner_token, tenant_id=tenant, company_id=self.company)
        self.assertTrue(self.service.refresh_for_job(
            Envelope(tenant, self.company), reference, operation="read_message"))
        self.assertEqual("HEALTHY", self.service.dashboard_connections(
            self.principal, tenant_id=tenant, company_id=self.company)[0]["health"])
        self.service.disconnect(self.principal, tenant_id=tenant, company_id=self.company,
                                connection_id=connection.connection_id)
        with self.assertRaises(PermissionError):
            self.service.refresh_for_job(Envelope(tenant, self.company), reference,
                                         operation="read_message")

    def test_cross_tenant_health_and_connection_reference_fail_closed(self):
        started, query = self._start()
        connection = self._complete(started, query)
        with self.assertRaises(Exception):
            self.service.dashboard_connections(self.principal, tenant_id="tenant_other", company_id=self.company)
        with self.assertRaises(LookupError):
            self.service.record_operational_signal(
                tenant_id="tenant_other", company_id=self.company, connection_id=connection.connection_id,
                error_code=None, usable=True,
            )
        self.assertEqual((), self.repository.list_broker_records("provider_health", "tenant_other", self.company))

    def test_calendar_oauth_connection_is_provider_neutral_without_worker_grant(self):
        started = self.service.start(self.principal, tenant_id=self.tenant.tenant_id,
            company_id=self.company, provider_name=self.calendar_provider.provider,
            redirect_uri="https://app.example.test/connections/callback",
            scopes=frozenset({"calendar.read"}))
        query = parse_qs(urlparse(started.authorization_url).query)
        code = self.calendar_provider.issue_test_code(
            code_challenge=query["code_challenge"][0],
            redirect_uri="https://app.example.test/connections/callback",
            scopes=frozenset({"calendar.read"}),
        )
        connection = self.service.complete(self.owner_token, state=query["state"][0], code=code,
            pkce_verifier=started.pkce_verifier,
            redirect_uri="https://app.example.test/connections/callback")
        self.assertEqual("CALENDAR", connection.capability)
        self.assertEqual("calendar.oauth", self.repository.get_broker_record(
            "external_credential_ref", self.tenant.tenant_id, self.company,
            connection.secret_ref_ids[0]).secret_type)
        self.assertEqual((), self.repository.list_broker_records(
            "capability_grant", self.tenant.tenant_id, self.company))
        self.assertEqual("HEALTHY", self.service.dashboard_connections(
            self.principal, tenant_id=self.tenant.tenant_id, company_id=self.company)[0]["health"])

    def test_scoped_key_reference_is_deduplicated_and_revoked_without_leaking_value(self):
        key = self.key_provider.issue_test_key()
        tenant = self.tenant.tenant_id
        connection = self.service.connect_scoped_key(
            self.principal, tenant_id=tenant, company_id=self.company,
            provider_name=self.key_provider.provider, credential=key,
        )
        duplicate = self.service.connect_scoped_key(
            self.principal, tenant_id=tenant, company_id=self.company,
            provider_name=self.key_provider.provider, credential=key,
        )
        self.assertEqual(connection.connection_id, duplicate.connection_id)
        self.assertEqual(1, len(self.repository.list_broker_records("provider_connection", tenant, self.company)))
        self.assertEqual("CRM", connection.capability)
        self.assertEqual("scoped_api_key", connection.auth_method)
        view = self.service.dashboard_connections(self.principal, tenant_id=tenant, company_id=self.company)
        self.assertNotIn(key.decode(), json.dumps(view))
        self.assertNotIn(key.decode(), json.dumps([repr(item) for item in self.repository.list_broker_records(
            "external_credential_ref", tenant, self.company)]))
        self.assertEqual((), self.repository.list_broker_records("capability_grant", tenant, self.company))
        self.service.disconnect(self.principal, tenant_id=tenant, company_id=self.company,
                                connection_id=connection.connection_id)
        self.assertEqual("DISCONNECTED", self.service.dashboard_connections(
            self.principal, tenant_id=tenant, company_id=self.company)[0]["health"])
        with self.assertRaises(LookupError):
            self.service.record_operational_signal(
                tenant_id="tenant_other", company_id=self.company,
                connection_id=connection.connection_id, error_code=None, usable=True,
            )
        with self.assertRaises(PermissionError):
            self.service.connect_scoped_key(
                self.principal, tenant_id=tenant, company_id=self.company,
                provider_name=self.key_provider.provider, credential=b"invalid-credential-length",
            )

    def test_concurrent_scoped_key_connection_and_rollback_leave_one_safe_record(self):
        key = self.key_provider.issue_test_key()
        tenant = self.tenant.tenant_id
        original = self.broker.register_connection
        self.broker.register_connection = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("domain write failed"))
        try:
            with self.assertRaises(RuntimeError):
                self.service.connect_scoped_key(self.principal, tenant_id=tenant,
                    company_id=self.company, provider_name=self.key_provider.provider, credential=key)
        finally:
            self.broker.register_connection = original
        self.assertEqual((), self.repository.list_broker_records("provider_connection", tenant, self.company))
        connection_id = stable_id("connection", tenant, self.company,
                                  self.key_provider.provider, "sandbox_business_crm_001")
        with self.assertRaises(LookupError):
            self.store.resolve(self.service.secret_locator_factory(tenant, self.company, connection_id))
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: self.service.connect_scoped_key(
                self.principal, tenant_id=tenant, company_id=self.company,
                provider_name=self.key_provider.provider, credential=key), range(2)))
        self.assertEqual(values[0].connection_id, values[1].connection_id)
        self.assertEqual(1, len(self.repository.list_broker_records("provider_connection", tenant, self.company)))
        self.assertEqual(1, len(self.repository.list_broker_records("external_credential_ref", tenant, self.company)))

    def test_scoped_key_idempotency_collision_denies_without_replacing_credential(self):
        key = self.key_provider.issue_test_key()
        tenant = self.tenant.tenant_id
        command_key = "connection-collision-proof-001"
        first = self.service.connect_scoped_key(self.principal, tenant_id=tenant,
            company_id=self.company, provider_name=self.key_provider.provider,
            credential=key, idempotency_key=command_key)
        original = self.key_provider.validate_key
        self.key_provider.validate_key = lambda value: "sandbox_business_crm_002"
        try:
            with self.assertRaises(OAuthFlowDenied):
                self.service.connect_scoped_key(self.principal, tenant_id=tenant,
                    company_id=self.company, provider_name=self.key_provider.provider,
                    credential=key, idempotency_key=command_key)
        finally:
            self.key_provider.validate_key = original
        self.assertEqual(1, len(self.repository.list_broker_records("provider_connection", tenant, self.company)))
        command = self.repository.list_broker_records("connection_command", tenant, self.company)[0]
        self.assertEqual(first.connection_id, command.connection_id)
        self.assertNotIn(key.decode(), repr(command))

    def test_state_is_one_time_expiring_session_bound_and_redirect_pkce_bound(self):
        started, query = self._start()
        self._complete(started, query)
        with self.assertRaises(OAuthFlowDenied):
            self._complete(started, query)

        started, query = self._start()
        other = self.identity.register_user("oauth-member@example.test")
        invited = self.identity.invite_member(AuthorizationContext(self.owner.user_id, self.tenant.tenant_id, self.company),
                                               other.user_id, Role.ADMIN, reason="test")
        self.identity.accept_membership(invited.membership_id, other.user_id)
        other_token = self._token(other.user_id, other.email)
        with self.assertRaises(OAuthFlowDenied):
            self._complete(started, query, token=other_token)

        outsider, _ = self.identity.register_founder("oauth-outsider@example.test", "OAuth Outsider")
        other_tenant, _, _ = self.identity.create_account(outsider.user_id, "Other OAuth Org")
        other_company = "company_oauth_other_tenant"
        self.identity.attach_company(AuthorizationContext(outsider.user_id, other_tenant.tenant_id), other_company)
        outsider_token = self._token(outsider.user_id, outsider.email)
        with self.assertRaises(AuthorizationDenied):
            self._complete(started, query, token=outsider_token)

        started, query = self._start()
        self.clock.now += timedelta(minutes=11)
        with self.assertRaises(OAuthFlowDenied):
            self._complete(started, query)
        self.clock.now = NOW

        started, query = self._start()
        with self.assertRaises(OAuthFlowDenied):
            self.service.complete(self.owner_token, state=query["state"][0], code="sandbox_code_unusable",
                pkce_verifier=started.pkce_verifier, redirect_uri="https://wrong.example.test/callback")
        with self.assertRaises(OAuthFlowDenied):
            self.service.complete(self.owner_token, state=query["state"][0], code="sandbox_code_unusable",
                pkce_verifier="", redirect_uri="https://app.example.test/oauth/callback")
        code = self.provider.issue_test_code(code_challenge=query["code_challenge"][0],
            redirect_uri="https://app.example.test/oauth/callback", scopes=frozenset({"mail.read", "mail.send"}))
        with self.assertRaises(OAuthFlowDenied):
            self.service.complete(self.owner_token, state=query["state"][0], code=code,
                pkce_verifier="forged-verifier", redirect_uri="https://app.example.test/oauth/callback")

    def test_owner_admin_allowed_member_support_and_cross_company_denied(self):
        admin = self.identity.register_user("oauth-admin@example.test")
        invited = self.identity.invite_member(AuthorizationContext(self.owner.user_id, self.tenant.tenant_id, self.company), admin.user_id, Role.ADMIN, reason="test")
        self.identity.accept_membership(invited.membership_id, admin.user_id)
        admin_principal = self.authority.issue(self._token(admin.user_id, admin.email), tenant_id=self.tenant.tenant_id, company_id=self.company)
        self._start(principal=admin_principal)

        member = self.identity.register_user("oauth-member2@example.test")
        invited = self.identity.invite_member(AuthorizationContext(self.owner.user_id, self.tenant.tenant_id, self.company), member.user_id, Role.MEMBER, reason="test")
        self.identity.accept_membership(invited.membership_id, member.user_id)
        member_principal = self.authority.issue(self._token(member.user_id, member.email), tenant_id=self.tenant.tenant_id, company_id=self.company)
        with self.assertRaises(AuthorizationDenied):
            self._start(principal=member_principal)
        with self.assertRaises(AuthorizationDenied):
            self.service.start(self.principal, tenant_id=self.tenant.tenant_id,
                company_id="company_other", provider_name=self.provider.provider,
                redirect_uri="https://app.example.test/oauth/callback", scopes=frozenset({"mail.read"}))

    def test_scope_denial_refresh_lock_rotation_disconnect_and_reconciliation(self):
        with self.assertRaises(OAuthFlowDenied):
            self._start(scopes=frozenset({"contacts.admin"}))
        started, query = self._start()
        connection = self._complete(started, query)
        secret_ref = JobSecretRef(connection.secret_ref_ids[0], connection.provider,
                                  "communications.email", connection.tenant_id, connection.company_id)
        self.clock.now += timedelta(minutes=16)
        envelope = Envelope(connection.tenant_id, connection.company_id)
        self.assertTrue(self.repository.claim_provider_refresh(
            connection.tenant_id, connection.company_id, connection.connection_id,
            "racing_worker", at=self.clock.now, lease=timedelta(seconds=30)))
        self.assertFalse(self.service.refresh_for_job(
            envelope, secret_ref, operation="send_preapproved_reply"))
        self.repository.release_provider_refresh(connection.tenant_id, connection.company_id,
                                                 connection.connection_id, "racing_worker")
        self.assertTrue(self.service.refresh_for_job(
            envelope, secret_ref, operation="send_preapproved_reply"))
        self.assertFalse(self.service.refresh_for_job(
            envelope, secret_ref, operation="send_preapproved_reply"))
        self.assertEqual(1, self.provider.refresh_count)

        self.provider.revoke_tokens()
        reconciled = self.service.reconcile(tenant_id=connection.tenant_id,
            company_id=connection.company_id, connection_id=connection.connection_id)
        self.assertIs(ConnectionStatus.RECONNECT_REQUIRED, reconciled.status)
        with self.assertRaises(LookupError):
            self.store.resolve(self.repository.get_broker_record(
                "external_credential_ref", connection.tenant_id, connection.company_id,
                connection.secret_ref_ids[0]).secret_locator)
        with self.assertRaises(PermissionError):
            self.broker._authorize_secret_record(envelope, secret_ref, "send_preapproved_reply")

    def test_signed_provider_callback_replay_and_forgery(self):
        started, query = self._start(); connection = self._complete(started, query)
        body = json.dumps({"event_id": "event_sandbox_revoke_001",
            "connection_id": connection.connection_id, "event_type": "revoked",
            "occurred_at": NOW.isoformat(), "reason": "sandbox revoke"}, sort_keys=True).encode()
        with self.assertRaises(PermissionError):
            self.service.handle_provider_callback(provider_name=self.provider.provider, body=body, signature="forged")
        denied = self.identity_repo.list_audit("unresolved")
        self.assertTrue(any(event.action == "provider.callback.denied" for event in denied))
        self.assertNotIn(body.decode(), repr(denied))
        changed = self.service.handle_provider_callback(provider_name=self.provider.provider,
            body=body, signature=self.provider.sign_callback(body))
        self.assertIs(ConnectionStatus.REVOKED, changed.status)
        with self.assertRaises(OAuthFlowDenied):
            self.service.handle_provider_callback(provider_name=self.provider.provider,
                body=body, signature=self.provider.sign_callback(body))

    def test_failure_classes_and_same_tenant_company_isolation(self):
        self.assertIs(ProviderFailureClass.RECONNECT_REQUIRED, classify_provider_failure("invalid_grant"))
        self.assertIs(ProviderFailureClass.RETRYABLE, classify_provider_failure("timeout"))
        self.assertIs(ProviderFailureClass.PROVIDER_UNHEALTHY, classify_provider_failure("provider_unavailable"))
        self.assertIs(ProviderFailureClass.PERMANENTLY_DENIED, classify_provider_failure("scope_missing"))
        started, query = self._start(); connection = self._complete(started, query)
        second_company = "company_oauth_second"
        self.identity.attach_company(AuthorizationContext(self.owner.user_id, self.tenant.tenant_id), second_company)
        second_principal = self.authority.issue(self.owner_token, tenant_id=self.tenant.tenant_id,
                                                company_id=second_company)
        with self.assertRaises(LookupError):
            self.service.get_connection(second_principal, tenant_id=self.tenant.tenant_id,
                                        company_id=second_company, connection_id=connection.connection_id)

    def test_sandbox_action_is_local_scoped_idempotent_and_scheduled_reconciliation_is_durable(self):
        started, query = self._start(); connection = self._complete(started, query)
        credential = self.repository.get_broker_record(
            "external_credential_ref", connection.tenant_id, connection.company_id,
            connection.secret_ref_ids[0],
        )
        with self.store.resolve(credential.secret_locator) as first:
            result_one = self.provider.execute(operation="send_preapproved_reply",
                provider_request_id="provider_request_sandbox_001",
                idempotency_key="provider_action_sandbox_001", credential=first, artifacts=())
        with self.store.resolve(credential.secret_locator) as second:
            result_two = self.provider.execute(operation="send_preapproved_reply",
                provider_request_id="provider_request_sandbox_001",
                idempotency_key="provider_action_sandbox_001", credential=second, artifacts=())
        self.assertEqual(result_one, result_two)
        self.assertEqual(1, self.provider.action_count)
        with self.store.resolve(credential.secret_locator) as value:
            with self.assertRaises(PermissionError):
                self.provider.execute(operation="contacts_admin",
                    provider_request_id="provider_request_sandbox_002",
                    idempotency_key="provider_action_sandbox_002", credential=value, artifacts=())
        schedule = self.service.reconciliation_schedule(connection,
            next_due_at=self.clock.now + timedelta(hours=1))
        self.repository.save_runtime_schedule(schedule)
        self.assertEqual(schedule, self.repository.get_runtime_schedule(
            connection.tenant_id, connection.company_id, schedule.schedule_id))
