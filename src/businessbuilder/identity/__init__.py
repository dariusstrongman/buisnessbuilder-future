"""Customer identity, tenant ownership, authorization, and session boundaries."""

from .authorization import AuthorizationPolicy, ROLE_PERMISSIONS, SUPPORT_NEVER_ALLOWED
from .exceptions import AuthorizationDenied, IdentityConflict, IdentityError, IdentityNotFound, InvalidIdentityTransition
from .models import (
    AccountRecoveryRequest, AuthorizationContext, FounderProfile, IdentityAuditEvent,
    Membership, MembershipStatus, Organization, OrganizationStatus, Permission, Role, Session,
    SupportAccessGrant, SupportImpersonationSession, Tenant, TenantStatus, User, UserStatus,
)
from .ports import AuthenticationProvider, FakeDevAuthenticationProvider, FutureMfaProvider
from .principal import AuthenticatedPrincipal, PrincipalContextAuthority
from .repository import IdentityRepository, InMemoryIdentityRepository, SQLiteIdentityRepository
from .service import IdentityService, SessionService

__all__ = [
    "AccountRecoveryRequest", "AuthenticationProvider", "AuthorizationContext",
    "AuthenticatedPrincipal", "AuthorizationDenied", "AuthorizationPolicy",
    "FakeDevAuthenticationProvider",
    "FounderProfile", "FutureMfaProvider", "IdentityAuditEvent", "IdentityConflict",
    "IdentityError", "IdentityNotFound", "IdentityRepository", "IdentityService",
    "InMemoryIdentityRepository", "SQLiteIdentityRepository", "InvalidIdentityTransition", "Membership",
    "MembershipStatus", "Organization", "OrganizationStatus", "Permission",
    "PrincipalContextAuthority", "ROLE_PERMISSIONS", "Role",
    "SUPPORT_NEVER_ALLOWED", "Session", "SessionService", "SupportAccessGrant",
    "SupportImpersonationSession", "Tenant", "TenantStatus", "User", "UserStatus",
]
