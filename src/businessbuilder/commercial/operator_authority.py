"""Server-created, scoped commercial operator authority; SUPPORT alone is inert."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
import hmac
import json
import secrets
from typing import Callable

from businessbuilder.identity import AuthorizationDenied, IdentityRepository
from businessbuilder.identity.models import (
    OrganizationStatus, Role, TenantStatus, UserStatus,
)

from .models import CommercialOperatorGrant, OrderAuditEvent
from .repository import CommercialRepository


OPERATOR_ACTIONS = frozenset({"existing_scope.publish", "quote.publish", "payment.release"})


@dataclass(frozen=True, slots=True)
class CommercialOperatorPrincipal:
    principal_id: str
    grant_id: str
    session_id: str
    operator_user_id: str
    tenant_id: str
    company_id: str
    issued_at: datetime
    expires_at: datetime
    signature: str


class CommercialOperatorAuthority:
    def __init__(
        self, identity: IdentityRepository, commercial: CommercialRepository, *,
        signing_key: bytes, clock: Callable[[], datetime],
        privileged_provisioner_verifier: Callable[[object], str] | None = None,
        lifetime: timedelta = timedelta(minutes=5),
    ) -> None:
        if len(signing_key) < 32 or lifetime <= timedelta(0):
            raise ValueError("strong signing key and positive lifetime required")
        self.identity = identity
        self.commercial = commercial
        self._signing_key = signing_key
        self.clock = clock
        self.privileged_provisioner_verifier = privileged_provisioner_verifier
        self.lifetime = lifetime

    def provision(
        self, provisioning_context: object, *, grant_id: str,
        operator_user_id: str, tenant_id: str, company_id: str,
        actions: frozenset[str], starts_at: datetime, ends_at: datetime,
        reason_code: str,
    ) -> CommercialOperatorGrant:
        """Only an injected privileged server-side verifier can appoint operators."""
        actor = self.privileged_provisioner_verifier(provisioning_context) if self.privileged_provisioner_verifier else ""
        if not actor or actor == operator_user_id or not actions or not actions <= OPERATOR_ACTIONS:
            raise AuthorizationDenied("privileged, non-self operator appointment required")
        if starts_at > self.clock() or ends_at <= starts_at or ends_at > starts_at + timedelta(days=1):
            raise AuthorizationDenied("operator appointment must be short-lived")
        self._validate_identity(operator_user_id, tenant_id, company_id)
        grant = CommercialOperatorGrant(
            grant_id, tenant_id, company_id, operator_user_id, actor,
            actions, starts_at, ends_at, reason_code,
        )
        with self.commercial.transaction():
            self.commercial.append_operator_grant(grant)
            self._audit(tenant_id, company_id, actor, "commercial.operator.appointed", "operator_grant", grant_id, reason_code)
        return grant

    def revoke(self, provisioning_context: object, *, tenant_id: str, company_id: str,
               grant_id: str, reason_code: str) -> CommercialOperatorGrant:
        actor = self.privileged_provisioner_verifier(provisioning_context) if self.privileged_provisioner_verifier else ""
        if not actor:
            raise AuthorizationDenied("privileged operator revocation required")
        grant = self.commercial.get_operator_grant(tenant_id, company_id, grant_id)
        if grant.revoked_at is not None:
            return grant
        changed = replace(grant, revoked_at=self.clock(), version=grant.version + 1)
        with self.commercial.transaction():
            self.commercial.append_operator_grant(changed)
            self._audit(tenant_id, company_id, actor, "commercial.operator.revoked", "operator_grant", grant_id, reason_code)
        return changed

    def issue(self, raw_session: str, *, tenant_id: str, company_id: str,
              grant_id: str) -> CommercialOperatorPrincipal:
        session = self.identity.get_session_by_digest("sha256:" + sha256(raw_session.encode()).hexdigest())
        now = self.clock()
        if session is None or not session.active_at(now) or session.provider_name is None:
            raise AuthorizationDenied("current provider-backed session required")
        user = self._validate_identity(session.user_id, tenant_id, company_id)
        grant = self.commercial.get_operator_grant(tenant_id, company_id, grant_id)
        if grant.operator_user_id != user.user_id or not grant.active_at(now):
            raise AuthorizationDenied("current scoped operator appointment required")
        expires = min(session.expires_at, grant.ends_at, now + self.lifetime)
        unsigned = {
            "principal_id": "", "grant_id": grant_id, "session_id": session.session_id,
            "operator_user_id": user.user_id, "tenant_id": tenant_id, "company_id": company_id,
            "issued_at": now, "expires_at": expires,
        }
        unsigned["principal_id"] = "commercial_principal_" + sha256(self._canonical(unsigned).encode()).hexdigest()[:24]
        return CommercialOperatorPrincipal(**unsigned, signature=self._signature(unsigned))

    def verify(self, principal: object, *, tenant_id: str, company_id: str,
               action: str, order_id: str | None = None) -> CommercialOperatorPrincipal:
        if not isinstance(principal, CommercialOperatorPrincipal) or action not in OPERATOR_ACTIONS:
            raise AuthorizationDenied("trusted commercial operator principal required")
        try:
            unsigned = asdict(principal)
            signature = unsigned.pop("signature")
            now = self.clock()
            if not hmac.compare_digest(signature, self._signature(unsigned)):
                raise AuthorizationDenied("commercial operator signature mismatch")
            if principal.tenant_id != tenant_id or principal.company_id != company_id:
                raise AuthorizationDenied("commercial operator scope mismatch")
            if principal.issued_at > now or principal.expires_at <= now:
                raise AuthorizationDenied("commercial operator principal expired")
            session = self.identity.get_session(principal.session_id)
            if session is None or not session.active_at(now) or session.user_id != principal.operator_user_id or session.provider_name is None:
                raise AuthorizationDenied("commercial operator session stale")
            self._validate_identity(principal.operator_user_id, tenant_id, company_id)
            grant = self.commercial.get_operator_grant(tenant_id, company_id, principal.grant_id)
            if grant.operator_user_id != principal.operator_user_id or not grant.active_at(now) or action not in grant.actions:
                raise AuthorizationDenied("commercial operator action not appointed")
            return principal
        except (AuthorizationDenied, LookupError):
            self._audit(
                "platform_security", "commercial_denials", principal.operator_user_id,
                "commercial.operator.denied", "order" if order_id else "operator_grant",
                sha256((order_id or principal.grant_id).encode()).hexdigest()[:24],
                "operator_identity_or_scope_invalid",
            )
            raise AuthorizationDenied("commercial operator action denied") from None

    def record_durable_denial(self, *, operation: str, order_id: str | None) -> None:
        """Called only after a failed commercial transaction has rolled back."""
        target = sha256((order_id or "missing_order").encode()).hexdigest()[:24]
        with self.commercial.transaction():
            self._audit(
                "platform_security", "commercial_denials", "untrusted",
                "commercial.operator.denied", "order", target,
                "operator_" + operation + "_authorization_failed",
            )

    def _validate_identity(self, operator_user_id: str, tenant_id: str, company_id: str):
        user = self.identity.get_user(operator_user_id)
        tenant = self.identity.get_tenant(tenant_id)
        organization = self.identity.get_organization_by_tenant(tenant_id)
        membership = self.identity.get_active_membership(tenant_id, operator_user_id)
        if (user.status is not UserStatus.ACTIVE or user.email_verified_at is None
            or not user.provider_subject_digest or not user.authentication_provider):
            raise AuthorizationDenied("active provider-verified operator required")
        if tenant.status is not TenantStatus.ACTIVE or organization.status is not OrganizationStatus.ACTIVE:
            raise AuthorizationDenied("active tenant and organization required")
        if company_id not in organization.company_ids or membership is None or membership.role is not Role.SUPPORT:
            raise AuthorizationDenied("current support membership and company scope required")
        return user

    @staticmethod
    def _canonical(value: dict) -> str:
        def convert(item):
            return item.isoformat() if isinstance(item, datetime) else item
        return json.dumps({key: convert(item) for key, item in value.items()}, sort_keys=True, separators=(",", ":"))

    def _signature(self, unsigned: dict) -> str:
        return hmac.new(self._signing_key, self._canonical(unsigned).encode(), "sha256").hexdigest()

    def _audit(self, tenant_id: str, company_id: str, actor: str, action: str,
               target_type: str, target_id: str, reason_code: str) -> None:
        self.commercial.append_order_audit(OrderAuditEvent(
            "commercial_audit_" + secrets.token_hex(16), tenant_id, company_id,
            actor, action, target_type, target_id, self.clock(),
            reason_code, "commercial_operator_authority", {},
        ))
