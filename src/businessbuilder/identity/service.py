from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
import secrets
from typing import Callable

from .authorization import AuthorizationPolicy, SUPPORT_NEVER_ALLOWED
from .exceptions import AuthorizationDenied, IdentityConflict, InvalidIdentityTransition
from .models import (
    AccountRecoveryRequest,
    AuthorizationContext,
    FounderProfile,
    IdentityAuditEvent,
    Membership,
    MembershipStatus,
    Organization,
    OrganizationStatus,
    Permission,
    Role,
    Session,
    SupportAccessGrant,
    SupportImpersonationSession,
    Tenant,
    TenantStatus,
    User,
    UserStatus,
)
from .ports import AuthenticationProvider
from .repository import IdentityRepository


class IdentityService:
    def __init__(
        self,
        repository: IdentityRepository,
        *,
        id_factory: Callable[[str], str],
        clock: Callable[[], datetime],
    ) -> None:
        self.repository = repository
        self.id_factory = id_factory
        self.clock = clock
        self.authorization = AuthorizationPolicy(repository)

    def register_user(self, email: str) -> User:
        normalized = email.strip().lower()
        if "@" not in normalized:
            raise ValueError("valid email required")
        user = User(self.id_factory("user"), normalized, created_at=self.clock())
        self.repository.add_user(user)
        return user

    def create_founder_profile(
        self, user_id: str, display_name: str, *, founder_profile_id: str | None = None
    ) -> FounderProfile:
        if not display_name.strip():
            raise ValueError("founder display name required")
        self.repository.get_user(user_id)
        profile = FounderProfile(
            founder_profile_id or self.id_factory("founder"),
            user_id,
            display_name.strip(),
            self.clock(),
        )
        self.repository.add_founder_profile(profile)
        return profile

    def register_founder(self, email: str, display_name: str) -> tuple[User, FounderProfile]:
        user = self.register_user(email)
        return user, self.create_founder_profile(user.user_id, display_name)

    def create_account(
        self, owner_user_id: str, display_name: str, *, tenant_id: str | None = None,
        organization_id: str | None = None, membership_id: str | None = None,
    ) -> tuple[Tenant, Organization, Membership]:
        now = self.clock()
        owner = self.repository.get_user(owner_user_id)
        if owner.status is not UserStatus.ACTIVE:
            raise InvalidIdentityTransition("deactivated user cannot own an account")
        if self.repository.get_founder_profile_for_user(owner_user_id) is None:
            raise InvalidIdentityTransition("account owner requires a distinct founder profile")
        if not display_name.strip():
            raise ValueError("organization display name required")
        tenant = Tenant(tenant_id or self.id_factory("tenant"), created_at=now)
        organization = Organization(
            organization_id or self.id_factory("org"), tenant.tenant_id, display_name.strip(), owner_user_id, created_at=now
        )
        membership = Membership(
            membership_id or self.id_factory("membership"),
            tenant.tenant_id,
            organization.organization_id,
            owner_user_id,
            Role.OWNER,
            MembershipStatus.ACTIVE,
            owner_user_id,
            now,
            accepted_at=now,
        )
        self.repository.add_tenant(tenant)
        self.repository.add_organization(organization)
        self.repository.add_membership(membership)
        self._audit(tenant.tenant_id, owner_user_id, "organization.created", "organization", organization.organization_id, "Founder created customer account")
        return tenant, organization, membership

    def attach_company(self, context: AuthorizationContext, company_id: str) -> Organization:
        self.authorization.require(context, Permission.APPROVE_FOUNDER_DECISIONS, at=self.clock())
        organization = self.repository.get_organization_by_tenant(context.tenant_id)
        if company_id in organization.company_ids:
            return organization
        changed = replace(organization, company_ids=organization.company_ids + (company_id,))
        self.repository.save_organization(changed)
        self._audit(context.tenant_id, context.actor_user_id, "organization.company_attached", "company", company_id, "Owner attached company to tenant", company_id)
        return changed

    def invite_member(
        self,
        context: AuthorizationContext,
        user_id: str,
        role: Role,
        *,
        reason: str = "Team invitation",
    ) -> Membership:
        self.authorization.require(context, Permission.MANAGE_MEMBERS, at=self.clock())
        if role is Role.OWNER:
            raise InvalidIdentityTransition("ownership uses the transfer workflow")
        if role is Role.SUPPORT and context.support_impersonation_session_id:
            raise AuthorizationDenied("support cannot invite support")
        organization = self.repository.get_organization_by_tenant(context.tenant_id)
        now = self.clock()
        membership = Membership(
            self.id_factory("membership"), context.tenant_id, organization.organization_id,
            user_id, role, MembershipStatus.INVITED, context.actor_user_id, now,
        )
        self.repository.add_membership(membership)
        self._audit(context.tenant_id, context.actor_user_id, "membership.invited", "membership", membership.membership_id, reason, context.company_id, {"role": role.value, "user_id": user_id})
        return membership

    def accept_membership(self, membership_id: str, user_id: str) -> Membership:
        membership = self.repository.get_membership(membership_id)
        if membership.user_id != user_id or membership.status is not MembershipStatus.INVITED:
            raise InvalidIdentityTransition("invitation cannot be accepted")
        user = self.repository.get_user(user_id)
        tenant = self.repository.get_tenant(membership.tenant_id)
        if user.status is not UserStatus.ACTIVE or tenant.status is not TenantStatus.ACTIVE:
            raise InvalidIdentityTransition("inactive user or tenant")
        changed = replace(membership, status=MembershipStatus.ACTIVE, accepted_at=self.clock())
        self.repository.save_membership(changed)
        self._audit(membership.tenant_id, user_id, "membership.accepted", "membership", membership_id, "Invitee accepted membership")
        return changed

    def remove_member(self, context: AuthorizationContext, membership_id: str, reason: str) -> Membership:
        self.authorization.require(context, Permission.MANAGE_MEMBERS, at=self.clock())
        membership = self.repository.get_membership(membership_id)
        if membership.tenant_id != context.tenant_id:
            raise AuthorizationDenied("membership is outside tenant")
        if membership.role is Role.OWNER:
            raise InvalidIdentityTransition("owner must transfer ownership first")
        if membership.status is MembershipStatus.REMOVED:
            return membership
        changed = replace(membership, status=MembershipStatus.REMOVED, removed_at=self.clock())
        self.repository.save_membership(changed)
        self._audit(context.tenant_id, context.actor_user_id, "membership.removed", "membership", membership_id, reason, context.company_id)
        return changed

    def change_member_role(
        self, context: AuthorizationContext, membership_id: str, new_role: Role, reason: str
    ) -> Membership:
        self.authorization.require(context, Permission.MANAGE_MEMBERS, at=self.clock())
        membership = self.repository.get_membership(membership_id)
        if membership.tenant_id != context.tenant_id:
            raise AuthorizationDenied("membership is outside tenant")
        if membership.status is not MembershipStatus.ACTIVE:
            raise InvalidIdentityTransition("only active membership role can change")
        if membership.role is Role.OWNER or new_role is Role.OWNER:
            raise InvalidIdentityTransition("ownership uses the transfer workflow")
        actor_membership = self.repository.get_active_membership(context.tenant_id, context.actor_user_id)
        if actor_membership is None:
            raise AuthorizationDenied("active actor membership required")
        if actor_membership.role is Role.ADMIN and new_role in {Role.ADMIN, Role.SUPPORT}:
            raise AuthorizationDenied("only owner can grant admin or support role")
        if membership.role is new_role:
            return membership
        changed = replace(membership, role=new_role)
        self.repository.save_membership(changed)
        self._audit(context.tenant_id, context.actor_user_id, "membership.role_changed", "membership", membership_id, reason, context.company_id, {"prior_role": membership.role.value, "new_role": new_role.value})
        return changed

    def transfer_ownership(
        self, context: AuthorizationContext, new_owner_user_id: str, reason: str
    ) -> Organization:
        self.authorization.require(context, Permission.APPROVE_FOUNDER_DECISIONS, at=self.clock())
        organization = self.repository.get_organization_by_tenant(context.tenant_id)
        if organization.owner_user_id != context.actor_user_id:
            raise AuthorizationDenied("only current owner may transfer ownership")
        incoming = self.repository.get_active_membership(context.tenant_id, new_owner_user_id)
        outgoing = self.repository.get_active_membership(context.tenant_id, context.actor_user_id)
        if incoming is None or outgoing is None:
            raise InvalidIdentityTransition("both owners need active membership")
        if self.repository.get_founder_profile_for_user(new_owner_user_id) is None:
            raise InvalidIdentityTransition("new owner requires a distinct founder profile")
        self.repository.save_membership(replace(incoming, role=Role.OWNER))
        self.repository.save_membership(replace(outgoing, role=Role.ADMIN))
        changed = replace(organization, owner_user_id=new_owner_user_id)
        self.repository.save_organization(changed)
        self._audit(context.tenant_id, context.actor_user_id, "ownership.transferred", "organization", organization.organization_id, reason, context.company_id, {"new_owner_user_id": new_owner_user_id})
        return changed

    def deactivate_user(self, context: AuthorizationContext, user_id: str, reason: str) -> User:
        if context.actor_user_id != user_id:
            self.authorization.require(context, Permission.MANAGE_MEMBERS, at=self.clock())
        user = self.repository.get_user(user_id)
        for membership in self.repository.list_memberships(context.tenant_id):
            if membership.user_id == user_id and membership.role is Role.OWNER and membership.status is MembershipStatus.ACTIVE:
                raise InvalidIdentityTransition("transfer ownership before deactivating owner")
        changed = replace(user, status=UserStatus.DEACTIVATED, deactivated_at=self.clock())
        self.repository.save_user(changed)
        self._audit(context.tenant_id, context.actor_user_id, "user.deactivated", "user", user_id, reason, context.company_id)
        return changed

    def suspend_tenant(self, context: AuthorizationContext, reason: str) -> Tenant:
        self.authorization.require(context, Permission.APPROVE_FOUNDER_DECISIONS, at=self.clock())
        tenant = self.repository.get_tenant(context.tenant_id)
        changed = replace(tenant, status=TenantStatus.SUSPENDED, suspended_at=self.clock(), suspension_reason=reason)
        self.repository.save_tenant(changed)
        self._audit(context.tenant_id, context.actor_user_id, "tenant.suspended", "tenant", context.tenant_id, reason, context.company_id)
        return changed

    def suspend_organization(self, context: AuthorizationContext, reason: str) -> Organization:
        self.authorization.require(context, Permission.APPROVE_FOUNDER_DECISIONS, at=self.clock())
        organization = self.repository.get_organization_by_tenant(context.tenant_id)
        changed = replace(
            organization, status=OrganizationStatus.SUSPENDED,
            suspended_at=self.clock(), suspension_reason=reason,
        )
        self.repository.save_organization(changed)
        self._audit(context.tenant_id, context.actor_user_id, "organization.suspended", "organization", organization.organization_id, reason, context.company_id)
        return changed

    def grant_support_access(
        self,
        context: AuthorizationContext,
        support_user_id: str,
        permissions: frozenset[Permission],
        duration: timedelta,
        reason: str,
        *,
        company_id: str | None = None,
    ) -> SupportAccessGrant:
        self.authorization.require(context, Permission.REQUEST_SUPPORT, at=self.clock())
        owner_membership = self.repository.get_active_membership(context.tenant_id, context.actor_user_id)
        if owner_membership is None or owner_membership.role is not Role.OWNER:
            raise AuthorizationDenied("only owner can grant support access")
        support_membership = self.repository.get_active_membership(context.tenant_id, support_user_id)
        if support_membership is None or support_membership.role is not Role.SUPPORT:
            raise InvalidIdentityTransition("support user needs an active support membership")
        if not reason.strip() or duration <= timedelta(0) or duration > timedelta(days=7):
            raise ValueError("reason and duration from 1 second through 7 days are required")
        if permissions & SUPPORT_NEVER_ALLOWED:
            raise AuthorizationDenied("support grant includes protected customer authority")
        if company_id is not None:
            organization = self.repository.get_organization_by_tenant(context.tenant_id)
            if company_id not in organization.company_ids:
                raise AuthorizationDenied("support grant company is outside tenant")
        now = self.clock()
        grant = SupportAccessGrant(
            self.id_factory("support_grant"), context.tenant_id, company_id, support_user_id,
            context.actor_user_id, permissions, reason.strip(), now, now + duration,
        )
        self.repository.save_support_grant(grant)
        self._audit(context.tenant_id, context.actor_user_id, "support_access.granted", "support_access_grant", grant.grant_id, reason, company_id, {"permissions": sorted(item.value for item in permissions), "ends_at": grant.ends_at.isoformat()})
        return grant

    def start_support_session(self, support_user_id: str, grant_id: str, reason: str) -> SupportImpersonationSession:
        now = self.clock()
        grant = self.repository.get_support_grant(grant_id)
        if grant.support_user_id != support_user_id or not grant.active_at(now):
            raise AuthorizationDenied("active matching support grant required")
        membership = self.repository.get_active_membership(grant.tenant_id, support_user_id)
        if membership is None or membership.role is not Role.SUPPORT:
            raise AuthorizationDenied("active support membership required")
        if not reason.strip():
            raise ValueError("support session reason required")
        session = SupportImpersonationSession(
            self.id_factory("support_session"), grant_id, grant.tenant_id, grant.company_id,
            support_user_id, reason.strip(), now, grant.ends_at,
        )
        self.repository.save_support_session(session)
        self._audit(grant.tenant_id, support_user_id, "support_impersonation.started", "support_impersonation_session", session.impersonation_session_id, reason, grant.company_id, {"grant_id": grant_id})
        return session

    def record_support_action(
        self, context: AuthorizationContext, permission: Permission, action: str, target_type: str,
        target_id: str, reason: str,
    ) -> None:
        self.authorization.require(context, permission, at=self.clock())
        self._audit(context.tenant_id, context.actor_user_id, action, target_type, target_id, reason, context.company_id, {"support_impersonation_session_id": context.support_impersonation_session_id})

    def end_support_session(self, session_id: str, support_user_id: str, reason: str) -> SupportImpersonationSession:
        session = self.repository.get_support_session(session_id)
        if session.support_user_id != support_user_id:
            raise AuthorizationDenied("only acting support user may end session")
        if session.ended_at is not None:
            return session
        changed = replace(session, ended_at=self.clock())
        self.repository.save_support_session(changed)
        self._audit(session.tenant_id, support_user_id, "support_impersonation.ended", "support_impersonation_session", session_id, reason, session.company_id)
        return changed

    def _audit(
        self, tenant_id: str, actor_user_id: str, action: str, target_type: str,
        target_id: str, reason: str, company_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.repository.append_audit(
            IdentityAuditEvent(
                self.id_factory("identity_audit"), tenant_id, actor_user_id, action,
                target_type, target_id, self.clock(), reason, "businessbuilder.identity",
                company_id, metadata or {},
            )
        )


class SessionService:
    """Session/recovery boundary that persists only digests and opaque references."""

    def __init__(
        self,
        repository: IdentityRepository,
        provider: AuthenticationProvider,
        *,
        id_factory: Callable[[str], str],
        clock: Callable[[], datetime],
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.id_factory = id_factory
        self.clock = clock

    def sign_in(self, email: str, proof: str, *, lifetime: timedelta = timedelta(hours=12)) -> tuple[Session, str]:
        user_id = self.provider.authenticate(email.strip().lower(), proof)
        if user_id is None:
            raise AuthorizationDenied("authentication failed")
        user = self.repository.get_user(user_id)
        if user.status is not UserStatus.ACTIVE:
            raise AuthorizationDenied("deactivated user")
        raw_token = secrets.token_urlsafe(32)
        now = self.clock()
        session = Session(self.id_factory("session"), user_id, _digest(raw_token), now, now + lifetime)
        self.repository.save_session(session)
        return session, raw_token

    def issue_for_user(
        self,
        user_id: str,
        *,
        provider_name: str,
        provider_session_id: str,
        lifetime: timedelta = timedelta(minutes=15),
        rotated_from_session_id: str | None = None,
    ) -> tuple[Session, str]:
        """Issue an opaque internal session after a trusted provider assertion."""
        user = self.repository.get_user(user_id)
        if user.status is not UserStatus.ACTIVE or user.email_verified_at is None:
            raise AuthorizationDenied("active verified user required")
        if not provider_name or not provider_session_id:
            raise AuthorizationDenied("trusted provider session required")
        raw_token = secrets.token_urlsafe(32)
        now = self.clock()
        session = Session(
            self.id_factory("session"),
            user_id,
            _digest(raw_token),
            now,
            now + lifetime,
            provider_name=provider_name,
            provider_session_digest=_digest(provider_session_id),
            rotated_from_session_id=rotated_from_session_id,
        )
        self.repository.save_session(session)
        self._audit(user, "session.established", session, "Verified external identity established session")
        return session, raw_token

    def validate_token(self, raw_token: str) -> User:
        session = self.repository.get_session_by_digest(_digest(raw_token))
        if session is None or not session.active_at(self.clock()):
            raise AuthorizationDenied("invalid or expired session")
        user = self.repository.get_user(session.user_id)
        if user.status is not UserStatus.ACTIVE:
            raise AuthorizationDenied("deactivated user")
        return user

    def sign_out(self, raw_token: str, *, reason: str = "User signed out") -> None:
        session = self.repository.get_session_by_digest(_digest(raw_token))
        if session is not None and session.revoked_at is None:
            changed = replace(session, revoked_at=self.clock(), revocation_reason=reason)
            self.repository.save_session(changed)
            self._audit(
                self.repository.get_user(session.user_id),
                "session.revoked",
                changed,
                reason,
            )

    def rotate(
        self,
        raw_token: str,
        *,
        provider_name: str,
        provider_session_id: str,
        lifetime: timedelta = timedelta(minutes=15),
    ) -> tuple[Session, str]:
        current = self.repository.get_session_by_digest(_digest(raw_token))
        user = self.validate_token(raw_token)
        if current is None:
            raise AuthorizationDenied("invalid or expired session")
        # Revoke first so a storage failure can only force reauthentication;
        # it can never leave both security-sensitive sessions active.
        self.sign_out(raw_token, reason="Session rotated")
        replacement, replacement_token = self.issue_for_user(
            user.user_id,
            provider_name=provider_name,
            provider_session_id=provider_session_id,
            lifetime=lifetime,
            rotated_from_session_id=current.session_id,
        )
        return replacement, replacement_token

    def revoke_user_sessions(self, user_id: str, *, reason: str) -> int:
        revoked = 0
        for session in tuple(self.repository.sessions_for_user(user_id)):
            if session.revoked_at is None:
                self.repository.save_session(
                    replace(session, revoked_at=self.clock(), revocation_reason=reason)
                )
                revoked += 1
        if revoked:
            user = self.repository.get_user(user_id)
            self._audit(user, "session.all_revoked", None, reason, {"count": revoked})
        return revoked

    def request_recovery(self, email: str, *, lifetime: timedelta = timedelta(minutes=30)) -> str | None:
        user = self.repository.get_user_by_email(email.strip().lower())
        if user is None or user.status is not UserStatus.ACTIVE:
            return None
        raw_token = secrets.token_urlsafe(32)
        now = self.clock()
        self.repository.save_recovery(
            AccountRecoveryRequest(self.id_factory("recovery"), user.user_id, _digest(raw_token), now, now + lifetime)
        )
        return raw_token

    def complete_recovery(self, raw_token: str, new_proof: str) -> None:
        request = self.repository.get_recovery_by_digest(_digest(raw_token))
        if request is None or request.consumed_at is not None or request.expires_at <= self.clock():
            raise AuthorizationDenied("invalid or expired recovery token")
        self.provider.update_recovery_credential(request.user_id, new_proof)
        self.repository.save_recovery(replace(request, consumed_at=self.clock()))

    def begin_email_verification(self, user_id: str) -> str:
        user = self.repository.get_user(user_id)
        return self.provider.begin_email_verification(user.user_id, user.email)

    def complete_email_verification(self, user_id: str, challenge_ref: str, proof: str) -> User:
        verified_user_id = self.provider.complete_email_verification(challenge_ref, proof)
        if verified_user_id != user_id:
            raise AuthorizationDenied("email verification failed")
        user = self.repository.get_user(user_id)
        changed = replace(user, email_verified_at=self.clock())
        self.repository.save_user(changed)
        return changed

    def _audit(
        self,
        user: User,
        action: str,
        session: Session | None,
        reason: str,
        metadata: dict | None = None,
    ) -> None:
        memberships = tuple(
            item for item in self.repository.list_user_memberships(user.user_id)
            if item.status is MembershipStatus.ACTIVE
        )
        tenant_id = memberships[0].tenant_id if len(memberships) == 1 else "unresolved"
        self.repository.append_audit(
            IdentityAuditEvent(
                self.id_factory("identity_audit"),
                tenant_id,
                user.user_id,
                action,
                "session",
                session.session_id if session else user.user_id,
                self.clock(),
                reason,
                "businessbuilder.identity.production_auth",
                metadata=metadata or {},
            )
        )


def _digest(value: str) -> str:
    return "sha256:" + sha256(value.encode()).hexdigest()
