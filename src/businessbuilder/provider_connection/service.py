from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
import secrets
from typing import Callable

from businessbuilder.access_broker import (
    CapabilityGrant, ConnectionStatus, CredentialScope, ExternalAccount,
    ExternalCredentialRef, JobSecretRef, ProviderConnection,
)
from businessbuilder.access_broker.ports import SecretStorePort
from businessbuilder.access_broker.service import BrokerDenied, SecretArtifactBroker
from businessbuilder.agent_runtime.models import AgentJobEnvelope, RuntimeSchedule
from businessbuilder.identity import (
    AuthenticatedPrincipal, AuthorizationContext, AuthorizationPolicy, IdentityAuditEvent, Permission,
    PrincipalContextAuthority,
)
from businessbuilder.runtime.audit import AuditLog
from businessbuilder.runtime.storage import RuntimeRepository

from .models import (
    AuthorizationStart, OAuthTokenMaterial, OAuthTransaction, ProviderCallbackEvent,
    ProviderFailureClass, ProviderHealth, digest_text,
)
from .ports import OAuthProviderPort


ROLE_OPERATION_SCOPES = {
    "role_inbox_assistant": frozenset({
        "read_message", "classify_message", "draft_reply", "send_preapproved_reply",
        "attachment_metadata",
    }),
    "role_review_followup_assistant": frozenset({
        "draft_review_request", "send_preapproved_review_request",
    }),
}


class OAuthFlowDenied(PermissionError):
    pass


class ProviderConnectionService:
    """Identity-bound provider lifecycle below Runtime and above the secret broker."""

    def __init__(self, *, repository: RuntimeRepository, principal_authority: PrincipalContextAuthority,
                 authorization: AuthorizationPolicy, broker: SecretArtifactBroker,
                 secret_store: SecretStorePort, providers: dict[str, OAuthProviderPort],
                 audit: AuditLog, clock: Callable[[], datetime], id_factory: Callable[[str], str],
                 secret_locator_factory: Callable[[str, str, str], str] | None = None,
                 state_lifetime: timedelta = timedelta(minutes=10),
                 refresh_window: timedelta = timedelta(minutes=5)) -> None:
        self.repository = repository
        self.principal_authority = principal_authority
        self.authorization = authorization
        self.broker = broker
        self.secret_store = secret_store
        self.providers = providers
        self.audit = audit
        self.clock = clock
        self.id_factory = id_factory
        self.secret_locator_factory = secret_locator_factory or (
            lambda tenant, company, connection: f"vault:{tenant}:{company}:{connection}"
        )
        self.state_lifetime = state_lifetime
        self.refresh_window = refresh_window

    def start(self, principal: AuthenticatedPrincipal, *, tenant_id: str, company_id: str,
              provider_name: str, redirect_uri: str, scopes: frozenset[str],
              reconnect_connection_id: str | None = None) -> AuthorizationStart:
        trusted = self._require_customer(principal, tenant_id, company_id)
        provider = self._provider(provider_name)
        if not scopes or not scopes <= provider.allowed_scopes:
            self._denied(trusted, "provider.oauth.start", "requested_scope_denied")
            raise OAuthFlowDenied("requested OAuth scope is not permitted")
        if not redirect_uri.startswith("https://") or len(redirect_uri) > 500:
            raise ValueError("OAuth redirect URI must be an exact HTTPS URI")
        now = self.clock()
        if reconnect_connection_id:
            current = self.repository.get_broker_record(
                "provider_connection", tenant_id, company_id, reconnect_connection_id
            )
            if current is None or current.provider != provider_name or current.status is ConnectionStatus.ACTIVE:
                raise OAuthFlowDenied("reconnect target is not eligible")
            connection_id, account_id = current.connection_id, current.account_id
        else:
            connection_id = self.id_factory("connection")
            account_id = self.id_factory("external_account")
            self.broker.register_external_account(ExternalAccount(
                account_id, tenant_id, company_id, provider_name,
                f"pending_{connection_id}", "Pending sandbox connection",
                ConnectionStatus.PENDING_AUTHORIZATION, now, now,
            ), actor_id=trusted.user_id)
        pending = ProviderConnection(
            connection_id, account_id, tenant_id, company_id, provider_name,
            ConnectionStatus.PENDING_AUTHORIZATION, now, now,
            account_type="email", scopes_requested=scopes, auth_method="oauth2_pkce",
            connected_by=trusted.user_id,
        )
        if reconnect_connection_id:
            self.repository.save_broker_record("provider_connection", connection_id, tenant_id, company_id, pending)
        else:
            self.broker.register_connection(pending, actor_id=trusted.user_id)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        nonce = secrets.token_urlsafe(24)
        challenge = base64.urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        transaction = OAuthTransaction(
            self.id_factory("oauth_flow"), digest_text(state), digest_text(verifier),
            trusted.session_id, tenant_id, company_id, connection_id, provider_name,
            digest_text(redirect_uri), digest_text(nonce), scopes, now,
            now + self.state_lifetime,
        )
        self.repository.save_oauth_transaction(transaction)
        url = provider.authorization_url(state=state, code_challenge=challenge,
            redirect_uri=redirect_uri, scopes=scopes, nonce=nonce)
        self._audit(trusted, "provider.oauth.started", connection_id, "one-time PKCE authorization started",
                    {"provider": provider_name, "scopes_requested": sorted(scopes), "expires_at": transaction.expires_at})
        return AuthorizationStart(connection_id, url, state, verifier, transaction.expires_at)

    def complete(self, raw_session_token: str, *, state: str, code: str,
                 pkce_verifier: str, redirect_uri: str) -> ProviderConnection:
        now = self.clock()
        user = self.principal_authority.authenticate_session(raw_session_token)
        preview = self.repository.get_oauth_transaction(digest_text(state))
        if preview is None or preview.consumed_at is not None or preview.expires_at <= now:
            self._audit_unscoped_denial(user.user_id, "provider.oauth.callback.denied", "invalid_expired_or_replayed_state")
            raise OAuthFlowDenied("OAuth callback state is invalid, expired, or replayed")
        principal = self.principal_authority.issue(
            raw_session_token, tenant_id=preview.tenant_id, company_id=preview.company_id
        )
        transaction = self.repository.consume_oauth_transaction(
            digest_text(state), principal.session_id, digest_text(redirect_uri),
            digest_text(pkce_verifier), at=now,
        )
        if transaction is None:
            self._audit_unscoped_denial(user.user_id, "provider.oauth.callback.denied", "callback_binding_mismatch")
            raise OAuthFlowDenied("OAuth callback state is invalid, expired, replayed, or session-mismatched")
        trusted = self._require_customer(principal, transaction.tenant_id, transaction.company_id)
        if trusted.session_id != transaction.session_id:
            raise OAuthFlowDenied("OAuth callback session binding failed")
        connection = self.repository.get_broker_record(
            "provider_connection", transaction.tenant_id, transaction.company_id,
            transaction.connection_id,
        )
        if connection is None or connection.status is not ConnectionStatus.PENDING_AUTHORIZATION:
            raise OAuthFlowDenied("provider connection is not awaiting authorization")
        provider = self._provider(transaction.provider)
        try:
            with provider.exchange_code(code=code, pkce_verifier=pkce_verifier,
                                        redirect_uri=redirect_uri) as material:
                if not transaction.scopes_requested <= material.scopes:
                    raise OAuthFlowDenied("provider omitted a required OAuth scope")
                locator = self.secret_locator_factory(transaction.tenant_id,
                    transaction.company_id, transaction.connection_id)
                secret_ref = "secretref_" + sha256(
                    f"{transaction.tenant_id}:{transaction.company_id}:{transaction.connection_id}".encode()
                ).hexdigest()[:24]
                self.secret_store.store(locator, material.encode_for_vault())
                credential = ExternalCredentialRef(
                    secret_ref, connection.connection_id, connection.tenant_id,
                    connection.company_id, connection.provider, "email.oauth", locator,
                    ConnectionStatus.ACTIVE, now, now, material.expires_at,
                )
                active = replace(
                    connection, status=ConnectionStatus.ACTIVE, updated_at=now,
                    provider_account_id=material.provider_account_id,
                    scopes_granted=material.scopes, connected_at=now,
                    expires_at=material.expires_at, provider_metadata=material.safe_metadata,
                    secret_ref_ids=(secret_ref,), revoked_reason=None, revoked_at=None,
                    compromised_at=None,
                )
        except Exception as exc:
            self._health(connection, usable=False, auth_failure=True,
                         error=type(exc).__name__)
            raise
        self.repository.save_broker_record("provider_connection", active.connection_id,
                                           active.tenant_id, active.company_id, active)
        account = self.repository.get_broker_record(
            "external_account", active.tenant_id, active.company_id, active.account_id
        )
        if account:
            self.repository.save_broker_record(
                "external_account", account.account_id, account.tenant_id, account.company_id,
                replace(account, external_account_ref=active.provider_account_id or account.external_account_ref,
                        label="Sandbox email", status=ConnectionStatus.ACTIVE, updated_at=now),
            )
        self.broker.register_credential_ref(credential, actor_id=trusted.user_id)
        self._install_grants(active, credential, actor_id=trusted.user_id)
        self._health(active, usable=True, auth_success=True)
        self._audit(trusted, "provider.oauth.callback.accepted", active.connection_id,
                    "one-time OAuth callback accepted", {"provider": active.provider})
        self._audit(trusted, "provider.connection.activated", active.connection_id,
                    "OAuth connection activated", {"provider": active.provider,
                    "scopes_granted": sorted(active.scopes_granted)})
        return active

    def list_connections(self, principal, *, tenant_id, company_id) -> tuple[ProviderConnection, ...]:
        self._require_customer(principal, tenant_id, company_id, permission=Permission.VIEW_COMPANY_STATE)
        return self.repository.list_broker_records("provider_connection", tenant_id, company_id)

    def get_connection(self, principal, *, tenant_id, company_id, connection_id) -> ProviderConnection:
        self._require_customer(principal, tenant_id, company_id, permission=Permission.VIEW_COMPANY_STATE)
        value = self.repository.get_broker_record("provider_connection", tenant_id, company_id, connection_id)
        if value is None:
            raise LookupError("provider connection not found")
        return value

    def disconnect(self, principal, *, tenant_id, company_id, connection_id, reason="customer_disconnect"):
        trusted = self._require_customer(principal, tenant_id, company_id)
        connection = self.broker.revoke_connection(
            tenant_id, company_id, connection_id, status=ConnectionStatus.DISCONNECTED,
            reason=reason, actor_id=trusted.user_id,
        )
        now = self.clock()
        changed = replace(connection, revoked_at=now)
        self.repository.save_broker_record("provider_connection", connection_id, tenant_id, company_id, changed)
        for secret_ref in connection.secret_ref_ids:
            credential = self.repository.get_broker_record(
                "external_credential_ref", tenant_id, company_id, secret_ref
            )
            if credential:
                self.repository.save_broker_record(
                    "external_credential_ref", secret_ref, tenant_id, company_id,
                    replace(credential, status=ConnectionStatus.DISCONNECTED),
                )
                self.secret_store.revoke(credential.secret_locator)
        self._health(changed, usable=False, error="customer_disconnect")
        self._audit(trusted, "provider.connection.disconnected", connection_id,
                    "customer disconnected provider", {"provider": connection.provider})
        return changed

    def refresh_for_job(self, envelope: AgentJobEnvelope, reference: JobSecretRef,
                        *, operation: str) -> bool:
        self.broker._authorize_envelope(envelope)
        if (reference.tenant_id, reference.company_id, reference.capability) != (
            envelope.tenant_id, envelope.company_id, envelope.capability
        ):
            raise BrokerDenied("refresh reference does not match Runtime job scope")
        credential = self.repository.get_broker_record(
            "external_credential_ref", envelope.tenant_id, envelope.company_id, reference.secret_ref
        )
        connection = self.repository.get_broker_record(
            "provider_connection", envelope.tenant_id, envelope.company_id,
            credential.connection_id if credential else "missing"
        )
        account = self.repository.get_broker_record(
            "external_account", envelope.tenant_id, envelope.company_id,
            connection.account_id if connection else "missing"
        )
        grants = self.repository.list_broker_records(
            "capability_grant", envelope.tenant_id, envelope.company_id
        )
        now = self.clock()
        if (
            credential is None or connection is None or account is None
            or credential.provider != reference.provider
            or credential.status is not ConnectionStatus.ACTIVE
            or connection.status is not ConnectionStatus.ACTIVE
            or account.status is not ConnectionStatus.ACTIVE
            or not any(grant.connection_id == connection.connection_id
                       and grant.agent_role == envelope.agent_role
                       and grant.scope.capability == envelope.capability
                       and operation in grant.scope.operations
                       and grant.revoked_at is None
                       and (grant.expires_at is None or grant.expires_at > now)
                       for grant in grants)
        ):
            raise BrokerDenied("provider refresh authority is inactive or out of scope")
        if credential.expires_at and credential.expires_at > now + self.refresh_window:
            return False
        owner = self.id_factory("refresh_worker")
        if not self.repository.claim_provider_refresh(
            envelope.tenant_id, envelope.company_id, connection.connection_id,
            owner, at=now, lease=timedelta(seconds=30),
        ):
            return False
        try:
            latest = self.repository.get_broker_record(
                "external_credential_ref", envelope.tenant_id, envelope.company_id, reference.secret_ref
            )
            if latest.expires_at and latest.expires_at > self.clock() + self.refresh_window:
                return False
            provider = self._provider(connection.provider)
            with self.secret_store.resolve(latest.secret_locator) as ephemeral:
                raw = ephemeral.use(bytes)
            with OAuthTokenMaterial.decode_from_vault(
                raw, provider_account_id=connection.provider_account_id or "unknown",
                safe_metadata=connection.provider_metadata,
            ) as current:
                refresh_token = current.refresh_token()
            raw_scratch = bytearray(raw)
            for index in range(len(raw_scratch)):
                raw_scratch[index] = 0
            if refresh_token is None:
                raise OAuthFlowDenied("provider connection requires reconnect")
            try:
                with provider.refresh(refresh_token=refresh_token) as rotated:
                    self.secret_store.store(latest.secret_locator, rotated.encode_for_vault())
                    at = self.clock()
                    self.repository.save_broker_record(
                        "external_credential_ref", latest.secret_ref, latest.tenant_id,
                        latest.company_id, replace(latest, rotated_at=at, expires_at=rotated.expires_at),
                    )
                    self.repository.save_broker_record(
                        "provider_connection", connection.connection_id, connection.tenant_id,
                        connection.company_id, replace(connection, updated_at=at, refreshed_at=at,
                            expires_at=rotated.expires_at, scopes_granted=rotated.scopes),
                    )
                self._health(connection, usable=True, refresh_success=True)
                self._audit_system(connection, "provider.token.refreshed", connection.connection_id,
                                   {"expires_at": rotated.expires_at, "scopes": sorted(rotated.scopes)})
                return True
            except Exception as exc:
                self._health(connection, usable=False, refresh_failure=True, error=type(exc).__name__)
                if getattr(exc, "classification", None) is ProviderFailureClass.RECONNECT_REQUIRED:
                    self._system_revoke(connection, ConnectionStatus.RECONNECT_REQUIRED, "refresh_invalid_grant")
                raise
        finally:
            self.repository.release_provider_refresh(envelope.tenant_id, envelope.company_id,
                                                     connection.connection_id, owner)

    def handle_provider_callback(self, *, provider_name: str, body: bytes, signature: str) -> ProviderConnection:
        provider = self._provider(provider_name)
        try:
            event = provider.verify_callback(body=body, signature=signature)
        except Exception as exc:
            self._audit_unscoped_denial("provider", "provider.callback.denied", type(exc).__name__)
            raise
        if event.provider != provider_name or not self.repository.claim_provider_callback(provider_name, event.event_id):
            self._audit_unscoped_denial("provider", "provider.callback.denied", "provider_mismatch_or_replay")
            raise OAuthFlowDenied("provider callback is invalid or replayed")
        connection = self.repository.get_broker_record_by_id("provider_connection", event.connection_id)
        if connection is None or connection.provider != provider_name:
            raise OAuthFlowDenied("provider callback connection is unknown")
        if event.event_type in {"revoked", "disconnected", "account_deleted"}:
            status = ConnectionStatus.REVOKED if event.event_type != "account_deleted" else ConnectionStatus.RECONNECT_REQUIRED
            changed = self._system_revoke(connection, status, event.safe_reason)
        elif event.event_type == "scope_changed":
            changed = self._system_revoke(connection, ConnectionStatus.RECONNECT_REQUIRED, "provider_scope_changed")
        else:
            raise OAuthFlowDenied("provider callback event type is not supported")
        self._audit_system(changed, "provider.callback.accepted", event.event_id,
                           {"event_type": event.event_type})
        return changed

    def reconcile(self, *, tenant_id: str, company_id: str, connection_id: str) -> ProviderConnection:
        connection = self.repository.get_broker_record("provider_connection", tenant_id, company_id, connection_id)
        if connection is None or connection.status is not ConnectionStatus.ACTIVE:
            raise OAuthFlowDenied("connection is not active")
        credential = self.repository.get_broker_record(
            "external_credential_ref", tenant_id, company_id, connection.secret_ref_ids[0]
        )
        provider = self._provider(connection.provider)
        with self.secret_store.resolve(credential.secret_locator) as ephemeral:
            raw = ephemeral.use(bytes)
        with OAuthTokenMaterial.decode_from_vault(raw, provider_account_id=connection.provider_account_id or "unknown") as token:
            reality = provider.reconcile_connection(connection, access_token=token.access_token())
        if not reality.active or not connection.scopes_requested <= reality.scopes_granted:
            changed = self._system_revoke(connection, ConnectionStatus.RECONNECT_REQUIRED,
                                          (reality.failure_class or ProviderFailureClass.RECONNECT_REQUIRED).value)
        else:
            changed = replace(connection, updated_at=self.clock(), scopes_granted=reality.scopes_granted,
                              provider_metadata=reality.safe_metadata)
            self.repository.save_broker_record("provider_connection", connection_id, tenant_id, company_id, changed)
        self._health(changed, usable=changed.status is ConnectionStatus.ACTIVE,
                     reconciled=True, error=None if changed.status is ConnectionStatus.ACTIVE else "reconnect_required")
        self._audit_system(changed, "provider.connection.reconciled", connection_id,
                           {"usable": changed.status is ConnectionStatus.ACTIVE})
        return changed

    def reconciliation_schedule(self, connection: ProviderConnection, *, next_due_at: datetime,
                                interval_seconds: int = 3600) -> RuntimeSchedule:
        return RuntimeSchedule(
            f"provider_reconcile_{connection.connection_id}", connection.tenant_id,
            connection.company_id, "role_inbox_assistant", "communications.email",
            "reconcile_provider_connection", interval_seconds, next_due_at, True,
        )

    def _install_grants(self, connection, credential, *, actor_id):
        for role, operations in ROLE_OPERATION_SCOPES.items():
            allowed = set(operations)
            if "mail.read" not in connection.scopes_granted:
                allowed -= {"read_message", "classify_message", "attachment_metadata"}
            if "mail.send" not in connection.scopes_granted:
                allowed -= {"send_preapproved_reply", "send_preapproved_review_request"}
            if not allowed:
                continue
            self.broker.register_capability_grant(CapabilityGrant(
                f"grant_{connection.connection_id}_{role}", connection.tenant_id,
                connection.company_id, connection.connection_id, role,
                CredentialScope("communications.email", frozenset(allowed), frozenset({credential.secret_type})),
                frozenset(), self.clock(),
            ), actor_id=actor_id)

    def _require_customer(self, principal, tenant_id, company_id,
                          permission=Permission.MANAGE_PROVIDER_CONNECTIONS):
        trusted = self.principal_authority.verify(principal, tenant_id=tenant_id, company_id=company_id)
        self.authorization.require(AuthorizationContext(
            trusted.user_id, tenant_id, company_id,
            trusted.support_impersonation_session_id,
        ), permission, at=self.clock())
        return trusted

    def _provider(self, name):
        provider = self.providers.get(name)
        if provider is None or provider.provider != name:
            raise OAuthFlowDenied("provider is not configured")
        return provider

    def _system_revoke(self, connection, status, reason):
        now = self.clock()
        changed = replace(connection, status=status, updated_at=now, revoked_at=now,
                          revoked_reason=reason[:120])
        self.repository.save_broker_record("provider_connection", connection.connection_id,
                                           connection.tenant_id, connection.company_id, changed)
        for ref_id in connection.secret_ref_ids:
            ref = self.repository.get_broker_record("external_credential_ref", connection.tenant_id,
                                                    connection.company_id, ref_id)
            if ref:
                self.repository.save_broker_record("external_credential_ref", ref_id,
                    connection.tenant_id, connection.company_id, replace(ref, status=status))
                self.secret_store.revoke(ref.secret_locator)
        return changed

    def _health(self, connection, *, usable, auth_success=False, auth_failure=False,
                refresh_success=False, refresh_failure=False, error=None, reconciled=False):
        prior = self.repository.get_broker_record(
            "provider_health", connection.tenant_id, connection.company_id, connection.connection_id
        )
        now = self.clock()
        value = ProviderHealth(
            connection.connection_id, connection.tenant_id, connection.company_id, usable,
            (prior.auth_successes if prior else 0) + int(auth_success),
            (prior.auth_failures if prior else 0) + int(auth_failure),
            (prior.refresh_successes if prior else 0) + int(refresh_success),
            (prior.refresh_failures if prior else 0) + int(refresh_failure),
            error, prior.rate_limited_until if prior else None,
            now if reconciled else (prior.last_reconciled_at if prior else None), now,
        )
        self.repository.save_broker_record("provider_health", connection.connection_id,
                                           connection.tenant_id, connection.company_id, value)

    def _audit(self, principal, action, target_id, reason, details):
        self.audit.record(tenant_id=principal.tenant_id, company_id=principal.company_id,
            actor_type="user", actor_id=principal.user_id, action=action,
            target_type="provider_connection", target_id=target_id,
            correlation_id=self.id_factory("correlation"), reason=reason,
            source="provider_connection", details=details, after={"target_id": target_id, "action": action})

    def _audit_system(self, connection, action, target_id, details):
        self.audit.record(tenant_id=connection.tenant_id, company_id=connection.company_id,
            actor_type="system", actor_id="provider_connection", action=action,
            target_type="provider_connection", target_id=target_id,
            correlation_id=self.id_factory("correlation"), reason="provider lifecycle update",
            source="provider_connection", details=details, after={"target_id": target_id, "action": action})

    def _denied(self, principal, action, reason):
        self.audit.record(tenant_id=principal.tenant_id, company_id=principal.company_id,
            actor_type="user", actor_id=principal.user_id, action=action + ".denied",
            target_type="provider_connection", target_id="unresolved",
            correlation_id=self.id_factory("correlation"), reason=reason,
            permission=Permission.MANAGE_PROVIDER_CONNECTIONS.value,
            source="provider_connection", details={"outcome": "denied"})

    def _audit_unscoped_denial(self, actor_id, action, reason):
        self.authorization.repository.append_audit(IdentityAuditEvent(
            self.id_factory("identity_audit"), "unresolved", actor_id, action,
            "provider_connection", "unresolved", self.clock(), reason,
            "businessbuilder.provider_connection", None,
            {"outcome": "denied"},
        ))
