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
from businessbuilder.access_broker.ports import EphemeralSecret
from businessbuilder.access_broker.models import stable_id
from businessbuilder.company_brain import CompanyBrainService, EntityRef, Provenance, RecordKind, Scope
from businessbuilder.company_brain.errors import ConflictError, NotFoundError
from businessbuilder.access_broker.service import BrokerDenied, SecretArtifactBroker
from businessbuilder.agent_runtime.models import AgentJobEnvelope, RuntimeSchedule
from businessbuilder.identity import (
    AuthenticatedPrincipal, AuthorizationContext, AuthorizationPolicy, IdentityAuditEvent, Permission,
    PrincipalContextAuthority,
)
from businessbuilder.runtime.audit import AuditLog
from businessbuilder.runtime.storage import RuntimeRepository

from .models import (
    AuthorizationStart, ConnectionCommand, OAuthTokenMaterial, OAuthTransaction, ProviderCallbackEvent,
    ConnectionHealthState, ProviderFailureClass, ProviderHealth, digest_text,
)
from .connections import connection_state, normalize_health, safe_connection
from .ports import OAuthProviderPort, ScopedKeyProviderPort


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
                 scoped_key_providers: dict[str, ScopedKeyProviderPort] | None = None,
                 company_brain: CompanyBrainService | None = None,
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
        self.scoped_key_providers = scoped_key_providers or {}
        self.company_brain = company_brain
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
        capability = getattr(provider, "capability", "EMAIL")
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
                f"pending_{connection_id}", "Pending business connection",
                ConnectionStatus.PENDING_AUTHORIZATION, now, now,
            ), actor_id=trusted.user_id)
        pending = ProviderConnection(
            connection_id, account_id, tenant_id, company_id, provider_name,
            ConnectionStatus.PENDING_AUTHORIZATION, now, now,
            account_type=capability.lower(), scopes_requested=scopes, auth_method="oauth2_pkce",
            capability=capability, account_ownership="unverified",
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
                    connection.company_id, connection.provider, f"{connection.capability.lower()}.oauth", locator,
                    ConnectionStatus.ACTIVE, now, now, material.expires_at,
                )
                active = replace(
                    connection, status=ConnectionStatus.ACTIVE, updated_at=now,
                    provider_account_id=material.provider_account_id,
                    scopes_granted=material.scopes, connected_at=now,
                    expires_at=material.expires_at, provider_metadata=material.safe_metadata,
                    secret_ref_ids=(secret_ref,), revoked_reason=None, revoked_at=None,
                    account_ownership=getattr(provider, "account_ownership", "unverified"),
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
                        label="Connected business account", status=ConnectionStatus.ACTIVE, updated_at=now),
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

    def dashboard_connections(self, principal, *, tenant_id: str, company_id: str) -> tuple[dict, ...]:
        """Tenant-scoped, credential-free summaries for the permanent founder dashboard."""
        connections = self.list_connections(principal, tenant_id=tenant_id, company_id=company_id)
        at = self.clock()
        return tuple(safe_connection(connection, self.repository.get_broker_record(
            "provider_health", tenant_id, company_id, connection.connection_id
        ), at=at) for connection in connections)

    def configured_providers(self, principal, *, tenant_id: str, company_id: str) -> tuple[dict, ...]:
        self._require_customer(principal, tenant_id, company_id, permission=Permission.VIEW_COMPANY_STATE)
        oauth = tuple({
            "provider": name,
            "capability": getattr(provider, "capability", "EMAIL"),
            "scopes": sorted(provider.allowed_scopes),
            "environment": getattr(provider, "environment", "unknown"),
            "auth_method": "oauth2_pkce",
        } for name, provider in sorted(self.providers.items()))
        scoped = tuple({
            "provider": name, "capability": provider.capability, "scopes": [],
            "environment": provider.environment, "auth_method": "scoped_api_key",
        } for name, provider in sorted(self.scoped_key_providers.items()))
        return oauth + scoped

    def connect_scoped_key(self, principal, *, tenant_id: str, company_id: str,
                           provider_name: str, credential: bytes,
                           idempotency_key: str | None = None) -> ProviderConnection:
        """Founder-authorized test/scoped-key path; plaintext exists only during validation/vault write."""
        trusted = self._require_customer(principal, tenant_id, company_id)
        provider = self.scoped_key_providers.get(provider_name)
        if provider is None or provider.provider != provider_name or provider.environment != "sandbox":
            raise OAuthFlowDenied("scoped-key provider is not configured for this safe environment")
        if not 16 <= len(credential) <= 4096:
            raise ValueError("credential length is invalid")
        if idempotency_key is not None and (
            not 16 <= len(idempotency_key) <= 160
            or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-" for character in idempotency_key)
        ):
            raise ValueError("connection idempotency key is invalid")
        with EphemeralSecret(credential) as ephemeral:
            account_ref = ephemeral.use(provider.validate_key)
            connection_id = stable_id("connection", tenant_id, company_id, provider_name, account_ref)
            now = self.clock()
            account_id = stable_id("external_account", tenant_id, company_id, provider_name, account_ref)
            locator = self.secret_locator_factory(tenant_id, company_id, connection_id)
            secret_ref = stable_id("secretref", tenant_id, company_id, connection_id)
            command_id = stable_id("connection_command", tenant_id, company_id, idempotency_key) if idempotency_key else None
            account = ExternalAccount(account_id, tenant_id, company_id, provider_name, account_ref,
                                      "Dedicated business account", ConnectionStatus.ACTIVE, now, now)
            connection = ProviderConnection(
                connection_id, account_id, tenant_id, company_id, provider_name, ConnectionStatus.ACTIVE,
                now, now, account_type=provider.capability.lower(), provider_account_id=account_ref,
                auth_method="scoped_api_key", connected_by=trusted.user_id, connected_at=now,
                secret_ref_ids=(secret_ref,), capability=provider.capability,
                account_ownership=getattr(provider, "account_ownership", "unverified"),
            )
            ref = ExternalCredentialRef(
                secret_ref, connection_id, tenant_id, company_id, provider_name,
                f"{provider.capability.lower()}.api_key", locator, ConnectionStatus.ACTIVE, now, now,
            )
            stored = False
            try:
                with self.repository.transaction():
                    prior_command = self.repository.get_broker_record(
                        "connection_command", tenant_id, company_id, command_id
                    ) if command_id else None
                    if prior_command and (prior_command.provider, prior_command.account_ref, prior_command.connection_id) != (
                        provider_name, account_ref, connection_id
                    ):
                        raise OAuthFlowDenied("connection idempotency key conflicts with a different account")
                    existing = self.repository.get_broker_record(
                        "provider_connection", tenant_id, company_id, connection_id
                    )
                    if existing and existing.status is ConnectionStatus.ACTIVE:
                        if command_id and prior_command is None:
                            self.repository.save_broker_record("connection_command", command_id,
                                tenant_id, company_id, ConnectionCommand(tenant_id, company_id,
                                idempotency_key, provider_name, account_ref, connection_id, now))
                        return existing  # concurrent duplicate cannot overwrite a healthy credential.
                    ephemeral.use(lambda value: self.secret_store.store(locator, bytes(value)))
                    stored = True
                    if existing is None:
                        self.broker.register_external_account(account, actor_id=trusted.user_id)
                        self.broker.register_connection(connection, actor_id=trusted.user_id)
                    else:
                        self.repository.save_broker_record("external_account", account_id, tenant_id, company_id, account)
                        self.repository.save_broker_record("provider_connection", connection_id, tenant_id, company_id, connection)
                    self.broker.register_credential_ref(ref, actor_id=trusted.user_id)
                    if command_id and prior_command is None:
                        self.repository.save_broker_record("connection_command", command_id,
                            tenant_id, company_id, ConnectionCommand(tenant_id, company_id,
                            idempotency_key, provider_name, account_ref, connection_id, now))
                    self._health(connection, usable=True, auth_success=True)
                    self._audit(trusted, "provider.connection.scoped_key_activated", connection_id,
                                "scoped credential reference activated", {"provider": provider_name,
                                "capability": provider.capability})
                return connection
            except Exception:
                if stored:
                    self.secret_store.revoke(locator)  # compensate a vault write if domain persistence failed.
                raise

    def record_operational_signal(self, *, tenant_id: str, company_id: str, connection_id: str,
                                  error_code: str | None, usable: bool,
                                  quota_remaining: int | None = None, quota_unit: str | None = None,
                                  quota_warning_threshold: int | None = None,
                                  action_required: str | None = None) -> ProviderHealth:
        """Adapter/operator-only signal; never callable with a browser-supplied health value."""
        connection = self.repository.get_broker_record("provider_connection", tenant_id, company_id, connection_id)
        if connection is None:
            raise LookupError("provider connection not found in scope")
        if quota_remaining is not None and (quota_remaining < 0 or not quota_unit or len(quota_unit) > 32):
            raise ValueError("quota signal is invalid")
        if quota_warning_threshold is not None and (quota_warning_threshold < 0 or quota_remaining is None):
            raise ValueError("quota warning threshold is invalid")
        if error_code and (len(error_code) > 80 or not error_code.replace("_", "").isalnum()):
            raise ValueError("provider error must be a normalized code, not a raw payload")
        if action_required and action_required not in {"reconnect", "review_permissions", "top_up", "contact_provider"}:
            raise ValueError("founder action signal is invalid")
        now = self.clock()
        prior = self.repository.get_broker_record("provider_health", tenant_id, company_id, connection_id)
        state = normalize_health(error_code, usable=usable)
        if connection.status is not ConnectionStatus.ACTIVE:
            state = connection_state(connection, prior, at=now)
            usable = False
        elif quota_warning_threshold is not None and usable and quota_remaining <= quota_warning_threshold:
            state = ConnectionHealthState.WARNING
        value = ProviderHealth(
            connection_id, tenant_id, company_id, usable,
            prior.auth_successes if prior else 0, prior.auth_failures if prior else 0,
            prior.refresh_successes if prior else 0, prior.refresh_failures if prior else 0,
            error_code[:80] if error_code else None,
            prior.rate_limited_until if prior else None,
            prior.last_reconciled_at if prior else None, now,
            state, now if usable else (prior.last_successful_check_at if prior else None),
            now if error_code else (prior.last_failure_at if prior else None), action_required,
            quota_remaining, quota_unit, now if quota_remaining is not None else None,
        )
        if action_required:
            self._ensure_founder_action(connection, action_required)
        self.repository.save_broker_record("provider_health", connection_id, tenant_id, company_id, value)
        self._audit_system(connection, "provider.connection.health_changed", connection_id,
                           {"health": state.value, "founder_action": action_required})
        return value

    def _ensure_founder_action(self, connection: ProviderConnection, action_kind: str) -> None:
        if self.company_brain is None:
            return
        scope = Scope(connection.tenant_id, connection.company_id)
        action_id = "founder_action_connection_" + sha256(connection.connection_id.encode()).hexdigest()[:20]
        try:
            self.company_brain.repository.get_record(scope, action_id)
            return  # health improvement or duplicate signal may never silently complete the action.
        except NotFoundError:
            pass
        action = {
            "title": f"Restore {connection.provider.replace('-', ' ').title()} access",
            "reason": "Only the customer account owner can reconnect, approve scopes or restore provider billing.",
            "instructions": ["Review why this connection needs attention.",
                             "Use the Connections area to complete the provider-owned step.",
                             "Supply the provider authorization/result if Verification requires it."],
            "state": "prepared", "responsibility": "FOUNDER_ACTION",
            "partner_authority": "EXTERNAL_PROVIDER/AUTHORITY",
            "authority_target": connection.provider,
            "required_evidence_kinds": ["provider_authorization_result"],
            "evidence_refs": [], "critical": False, "selected": True,
        }
        try:
            self.company_brain.append_founder_action(
                scope, action_id=action_id, data=action,
                provenance=(Provenance("provider_connection_health", self.clock().isoformat(),
                                       EntityRef("system", "provider_connection"), connection.connection_id),),
                owner_ref=EntityRef("company", connection.company_id),
            )
        except ConflictError:
            return  # another health checker created this same pending action concurrently.
        self._audit_system(connection, "provider.connection.founder_action.created", action_id,
                           {"action_kind": action_kind})

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
        health = self.repository.get_broker_record(
            "provider_health", envelope.tenant_id, envelope.company_id, connection.connection_id
        )
        if connection_state(connection, health, at=now) not in {
            ConnectionHealthState.HEALTHY, ConnectionHealthState.WARNING,
        }:
            raise BrokerDenied("provider connection health blocks this Runtime job")
        if connection.auth_method == "scoped_api_key":
            return False  # health/grants were revalidated; an API key has no OAuth refresh lifecycle.
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
        if connection.capability != "EMAIL":
            return  # A new capability needs an existing AI Workforce role policy first.
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
            normalize_health(error, usable=usable),
            now if usable else (prior.last_successful_check_at if prior else None),
            now if error else (prior.last_failure_at if prior else None),
            "reconnect" if error in {"reconnect_required", "refresh_invalid_grant"} else None,
            prior.quota_remaining if prior else None,
            prior.quota_unit if prior else None,
            prior.quota_checked_at if prior else None,
        )
        if value.action_required:
            self._ensure_founder_action(connection, value.action_required)
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
