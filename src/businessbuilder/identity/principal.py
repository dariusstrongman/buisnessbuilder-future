from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import hmac
import json
import secrets
from typing import Callable

from .exceptions import AuthorizationDenied
from .models import (
    MembershipStatus,
    OrganizationStatus,
    Role,
    Session,
    TenantStatus,
    UserStatus,
    User,
)
from .repository import IdentityRepository


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    principal_id: str
    session_id: str
    user_id: str
    tenant_id: str
    organization_id: str
    membership_id: str
    role: Role
    company_id: str | None
    support_impersonation_session_id: str | None
    issued_at: datetime
    expires_at: datetime
    signature: str


class PrincipalContextAuthority:
    """Mints short-lived, process-bound contexts from durable sessions and identity state."""

    def __init__(
        self,
        repository: IdentityRepository,
        *,
        clock: Callable[[], datetime],
        signing_key: bytes | None = None,
        lifetime: timedelta = timedelta(minutes=5),
    ) -> None:
        if lifetime <= timedelta(0):
            raise ValueError("principal lifetime must be positive")
        self.repository = repository
        self.clock = clock
        self._signing_key = signing_key or secrets.token_bytes(32)
        self.lifetime = lifetime

    def issue(
        self,
        raw_session_token: str,
        *,
        tenant_id: str,
        company_id: str | None = None,
        support_impersonation_session_id: str | None = None,
    ) -> AuthenticatedPrincipal:
        now = self.clock()
        session, _ = self._active_session(raw_session_token, now)
        identity = self._derive(
            session.user_id,
            tenant_id,
            company_id,
            support_impersonation_session_id,
            now,
        )
        expires_at = min(session.expires_at, now + self.lifetime)
        if identity["support_ends_at"] is not None:
            expires_at = min(expires_at, identity["support_ends_at"])
        unsigned = {
            "principal_id": "",
            "session_id": session.session_id,
            "user_id": session.user_id,
            "tenant_id": tenant_id,
            "organization_id": identity["organization_id"],
            "membership_id": identity["membership_id"],
            "role": identity["role"],
            "company_id": company_id,
            "support_impersonation_session_id": support_impersonation_session_id,
            "issued_at": now,
            "expires_at": expires_at,
        }
        principal_id = "principal_" + sha256(
            _canonical(unsigned).encode()
        ).hexdigest()[:24]
        unsigned["principal_id"] = principal_id
        signature = self._signature(unsigned)
        return AuthenticatedPrincipal(**unsigned, signature=signature)

    def authenticate_session(self, raw_session_token: str) -> User:
        """Validate an opaque bearer token without selecting client-owned scope."""
        _, user = self._active_session(raw_session_token, self.clock())
        return user

    def verify(
        self,
        principal: object,
        *,
        tenant_id: str,
        company_id: str,
    ) -> AuthenticatedPrincipal:
        if not isinstance(principal, AuthenticatedPrincipal):
            raise AuthorizationDenied("trusted authenticated principal required")
        unsigned = asdict(principal)
        signature = unsigned.pop("signature")
        if not hmac.compare_digest(signature, self._signature(unsigned)):
            raise AuthorizationDenied("principal signature is invalid")
        now = self.clock()
        if principal.issued_at > now or principal.expires_at <= now:
            raise AuthorizationDenied("principal is expired or not yet valid")
        if principal.tenant_id != tenant_id or principal.company_id != company_id:
            raise AuthorizationDenied("principal request scope mismatch")
        session = self.repository.get_session(principal.session_id)
        if (
            session is None
            or session.session_id != principal.session_id
            or session.user_id != principal.user_id
            or not session.active_at(now)
        ):
            raise AuthorizationDenied("principal session is no longer active")
        identity = self._derive(
            principal.user_id,
            principal.tenant_id,
            principal.company_id,
            principal.support_impersonation_session_id,
            now,
        )
        if (
            identity["organization_id"] != principal.organization_id
            or identity["membership_id"] != principal.membership_id
            or identity["role"] is not principal.role
        ):
            raise AuthorizationDenied("principal identity state changed")
        return principal

    def _derive(
        self,
        user_id: str,
        tenant_id: str,
        company_id: str | None,
        support_session_id: str | None,
        at: datetime,
    ) -> dict[str, object]:
        user = self.repository.get_user(user_id)
        tenant = self.repository.get_tenant(tenant_id)
        organization = self.repository.get_organization_by_tenant(tenant_id)
        membership = self.repository.get_active_membership(tenant_id, user_id)
        if user.status is not UserStatus.ACTIVE:
            raise AuthorizationDenied("deactivated user")
        if tenant.status is not TenantStatus.ACTIVE:
            raise AuthorizationDenied("suspended tenant")
        if organization.status is not OrganizationStatus.ACTIVE:
            raise AuthorizationDenied("suspended organization")
        if membership is None or membership.status is not MembershipStatus.ACTIVE:
            raise AuthorizationDenied("active membership required")
        if membership.organization_id != organization.organization_id:
            raise AuthorizationDenied("membership organization mismatch")
        if company_id is not None and company_id not in organization.company_ids:
            raise AuthorizationDenied("company is outside tenant")

        support_ends_at = None
        if membership.role is Role.SUPPORT:
            if not support_session_id:
                raise AuthorizationDenied("support impersonation session required")
            support_session = self.repository.get_support_session(support_session_id)
            grant = self.repository.get_support_grant(support_session.grant_id)
            if not support_session.active_at(at) or not grant.active_at(at):
                raise AuthorizationDenied("support impersonation is expired")
            if (
                support_session.support_user_id != user_id
                or support_session.tenant_id != tenant_id
                or grant.support_user_id != user_id
                or grant.tenant_id != tenant_id
            ):
                raise AuthorizationDenied("support impersonation scope mismatch")
            if support_session.company_id not in {None, company_id}:
                raise AuthorizationDenied("support session company mismatch")
            if grant.company_id not in {None, company_id}:
                raise AuthorizationDenied("support grant company mismatch")
            support_ends_at = min(support_session.ends_at, grant.ends_at)
        elif support_session_id is not None:
            raise AuthorizationDenied("non-support principal cannot impersonate")

        return {
            "organization_id": organization.organization_id,
            "membership_id": membership.membership_id,
            "role": membership.role,
            "support_ends_at": support_ends_at,
        }

    def _active_session(
        self, raw_session_token: str, at: datetime
    ) -> tuple[Session, User]:
        if not isinstance(raw_session_token, str) or not raw_session_token:
            raise AuthorizationDenied("active authenticated session required")
        session = self.repository.get_session_by_digest(_digest(raw_session_token))
        if session is None or not session.active_at(at):
            raise AuthorizationDenied("active authenticated session required")
        user = self.repository.get_user(session.user_id)
        if user.status is not UserStatus.ACTIVE:
            raise AuthorizationDenied("deactivated user")
        return session, user

    def _signature(self, unsigned: dict[str, object]) -> str:
        digest = hmac.new(
            self._signing_key,
            _canonical(unsigned).encode(),
            "sha256",
        ).hexdigest()
        return "hmac-sha256:" + digest


def _canonical(value: dict[str, object]) -> str:
    def default(item: object) -> str:
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, Role):
            return item.value
        raise TypeError(f"unsupported principal value: {type(item).__name__}")

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=default)


def _digest(value: str) -> str:
    return "sha256:" + sha256(value.encode()).hexdigest()
