from __future__ import annotations

from datetime import datetime

from .exceptions import AuthorizationDenied
from .models import (
    AuthorizationContext,
    MembershipStatus,
    OrganizationStatus,
    Permission,
    Role,
    TenantStatus,
    UserStatus,
)
from .repository import IdentityRepository


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(Permission),
    Role.ADMIN: frozenset(
        {
            Permission.VIEW_COMPANY_STATE,
            Permission.VIEW_BILLING,
            Permission.MANAGE_MEMBERS,
            Permission.ACCESS_ARTIFACTS,
            Permission.INTERACT_AI_WORKFORCE,
            Permission.MANAGE_PROVIDER_CONNECTIONS,
            Permission.REQUEST_SUPPORT,
        }
    ),
    Role.MEMBER: frozenset(
        {
            Permission.VIEW_COMPANY_STATE,
            Permission.ACCESS_ARTIFACTS,
            Permission.INTERACT_AI_WORKFORCE,
            Permission.REQUEST_SUPPORT,
        }
    ),
    Role.SUPPORT: frozenset(
        {
            Permission.VIEW_COMPANY_STATE,
            Permission.ACCESS_ARTIFACTS,
        }
    ),
}

# These authorities can never be delegated through a support grant.
SUPPORT_NEVER_ALLOWED = frozenset(
    {
        Permission.APPROVE_FOUNDER_DECISIONS,
        Permission.AUTHORIZE_SPEND,
        Permission.VIEW_BILLING,
        Permission.CHANGE_SUBSCRIPTION,
        Permission.MANAGE_MEMBERS,
        Permission.PERFORM_HANDOFF,
        Permission.INTERACT_AI_WORKFORCE,
        Permission.MANAGE_PROVIDER_CONNECTIONS,
    }
)


class AuthorizationPolicy:
    """Server-side, default-deny authorization over persisted identity state."""

    def __init__(self, repository: IdentityRepository) -> None:
        self.repository = repository

    def require(self, context: AuthorizationContext, permission: Permission, *, at: datetime) -> None:
        user = self.repository.get_user(context.actor_user_id)
        if user.status is not UserStatus.ACTIVE:
            raise AuthorizationDenied("deactivated user")
        tenant = self.repository.get_tenant(context.tenant_id)
        if tenant.status is not TenantStatus.ACTIVE:
            raise AuthorizationDenied("suspended tenant")

        if context.company_id is not None:
            organization = self.repository.get_organization_by_tenant(context.tenant_id)
            if organization.status is not OrganizationStatus.ACTIVE:
                raise AuthorizationDenied("suspended organization")
            if context.company_id not in organization.company_ids:
                raise AuthorizationDenied("company is outside tenant")
        else:
            organization = self.repository.get_organization_by_tenant(context.tenant_id)
            if organization.status is not OrganizationStatus.ACTIVE:
                raise AuthorizationDenied("suspended organization")

        membership = self.repository.get_active_membership(context.tenant_id, context.actor_user_id)
        if membership is None or membership.status is not MembershipStatus.ACTIVE:
            raise AuthorizationDenied("active membership required")
        if permission not in ROLE_PERMISSIONS.get(membership.role, frozenset()):
            raise AuthorizationDenied(f"{membership.role.value} lacks {permission.value}")

        if membership.role is Role.SUPPORT:
            self._require_support_scope(context, permission, at)

    def allows(self, context: AuthorizationContext, permission: Permission, *, at: datetime) -> bool:
        try:
            self.require(context, permission, at=at)
            return True
        except (AuthorizationDenied, LookupError):
            return False

    def _require_support_scope(
        self, context: AuthorizationContext, permission: Permission, at: datetime
    ) -> None:
        if permission in SUPPORT_NEVER_ALLOWED:
            raise AuthorizationDenied("support cannot receive protected customer authority")
        if not context.support_impersonation_session_id:
            raise AuthorizationDenied("audited support session required")
        session = self.repository.get_support_session(context.support_impersonation_session_id)
        grant = self.repository.get_support_grant(session.grant_id)
        if not session.active_at(at) or not grant.active_at(at):
            raise AuthorizationDenied("support access expired or ended")
        if session.support_user_id != context.actor_user_id or session.tenant_id != context.tenant_id:
            raise AuthorizationDenied("support session scope mismatch")
        if grant.support_user_id != context.actor_user_id or grant.tenant_id != context.tenant_id:
            raise AuthorizationDenied("support grant scope mismatch")
        if grant.company_id is not None and grant.company_id != context.company_id:
            raise AuthorizationDenied("support company scope mismatch")
        if session.company_id is not None and session.company_id != context.company_id:
            raise AuthorizationDenied("support session company scope mismatch")
        if permission not in grant.permissions:
            raise AuthorizationDenied("permission absent from support grant")
