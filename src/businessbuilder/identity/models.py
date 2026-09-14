from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UserStatus(StrEnum):
    ACTIVE = "active"
    DEACTIVATED = "deactivated"


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class OrganizationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class MembershipStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    REMOVED = "removed"


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    SUPPORT = "support"


class Permission(StrEnum):
    VIEW_COMPANY_STATE = "company.view"
    APPROVE_FOUNDER_DECISIONS = "founder_decision.approve"
    AUTHORIZE_SPEND = "spend.authorize"
    VIEW_BILLING = "billing.view"
    CHANGE_SUBSCRIPTION = "subscription.change"
    MANAGE_MEMBERS = "members.manage"
    PERFORM_HANDOFF = "handoff.perform"
    ACCESS_ARTIFACTS = "artifacts.access"
    INTERACT_AI_WORKFORCE = "ai_workforce.interact"
    MANAGE_PROVIDER_CONNECTIONS = "provider_connections.manage"
    MANAGE_COMMUNICATIONS = "communications.manage"
    REQUEST_SUPPORT = "support.request"


@dataclass(frozen=True, slots=True)
class User:
    user_id: str
    email: str
    status: UserStatus = UserStatus.ACTIVE
    email_verified_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    deactivated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FounderProfile:
    founder_profile_id: str
    user_id: str
    display_name: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class Tenant:
    tenant_id: str
    status: TenantStatus = TenantStatus.ACTIVE
    created_at: datetime = field(default_factory=utc_now)
    suspended_at: datetime | None = None
    suspension_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Organization:
    organization_id: str
    tenant_id: str
    display_name: str
    owner_user_id: str
    company_ids: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    status: OrganizationStatus = OrganizationStatus.ACTIVE
    suspended_at: datetime | None = None
    suspension_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Membership:
    membership_id: str
    tenant_id: str
    organization_id: str
    user_id: str
    role: Role
    status: MembershipStatus
    invited_by_user_id: str
    invited_at: datetime
    accepted_at: datetime | None = None
    removed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Session:
    session_id: str
    user_id: str
    token_digest: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    mfa_satisfied: bool = False

    def active_at(self, at: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > at


@dataclass(frozen=True, slots=True)
class AccountRecoveryRequest:
    recovery_id: str
    user_id: str
    token_digest: str
    requested_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SupportAccessGrant:
    grant_id: str
    tenant_id: str
    company_id: str | None
    support_user_id: str
    approved_by_user_id: str
    permissions: frozenset[Permission]
    reason: str
    starts_at: datetime
    ends_at: datetime
    revoked_at: datetime | None = None

    def active_at(self, at: datetime) -> bool:
        return self.revoked_at is None and self.starts_at <= at < self.ends_at


@dataclass(frozen=True, slots=True)
class SupportImpersonationSession:
    impersonation_session_id: str
    grant_id: str
    tenant_id: str
    company_id: str | None
    support_user_id: str
    reason: str
    started_at: datetime
    ends_at: datetime
    ended_at: datetime | None = None

    def active_at(self, at: datetime) -> bool:
        return self.ended_at is None and self.started_at <= at < self.ends_at


@dataclass(frozen=True, slots=True)
class IdentityAuditEvent:
    audit_event_id: str
    tenant_id: str
    actor_user_id: str
    action: str
    target_type: str
    target_id: str
    occurred_at: datetime
    reason: str
    source: str
    company_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    actor_user_id: str
    tenant_id: str
    company_id: str | None = None
    support_impersonation_session_id: str | None = None
