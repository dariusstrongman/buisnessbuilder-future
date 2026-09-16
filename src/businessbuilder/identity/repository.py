from __future__ import annotations

from abc import ABC, abstractmethod
import sqlite3
from threading import RLock

from businessbuilder._serialization import decode_record, encode_record

from .exceptions import IdentityConflict, IdentityNotFound
from .models import (
    AccountRecoveryRequest,
    FounderProfile,
    IdentityAuditEvent,
    Membership,
    MembershipStatus,
    Organization,
    Session,
    SupportAccessGrant,
    SupportImpersonationSession,
    Tenant,
    User,
)


class IdentityRepository(ABC):
    @abstractmethod
    def add_user(self, user: User) -> None: ...

    @abstractmethod
    def save_user(self, user: User) -> None: ...

    @abstractmethod
    def get_user(self, user_id: str) -> User: ...

    @abstractmethod
    def get_user_by_email(self, email: str) -> User | None: ...

    @abstractmethod
    def get_user_by_external_identity(self, provider: str, subject_digest: str) -> User | None: ...

    @abstractmethod
    def add_founder_profile(self, profile: FounderProfile) -> None: ...

    @abstractmethod
    def get_founder_profile_for_user(self, user_id: str) -> FounderProfile | None: ...

    @abstractmethod
    def add_tenant(self, tenant: Tenant) -> None: ...

    @abstractmethod
    def save_tenant(self, tenant: Tenant) -> None: ...

    @abstractmethod
    def get_tenant(self, tenant_id: str) -> Tenant: ...

    @abstractmethod
    def add_organization(self, organization: Organization) -> None: ...

    @abstractmethod
    def save_organization(self, organization: Organization) -> None: ...

    @abstractmethod
    def get_organization(self, organization_id: str) -> Organization: ...

    @abstractmethod
    def get_organization_by_tenant(self, tenant_id: str) -> Organization: ...

    @abstractmethod
    def add_membership(self, membership: Membership) -> None: ...

    @abstractmethod
    def save_membership(self, membership: Membership) -> None: ...

    @abstractmethod
    def get_membership(self, membership_id: str) -> Membership: ...

    @abstractmethod
    def get_active_membership(self, tenant_id: str, user_id: str) -> Membership | None: ...

    @abstractmethod
    def list_memberships(self, tenant_id: str) -> tuple[Membership, ...]: ...

    @abstractmethod
    def list_user_memberships(self, user_id: str) -> tuple[Membership, ...]: ...

    @abstractmethod
    def save_session(self, session: Session) -> None: ...

    @abstractmethod
    def get_session(self, session_id: str) -> Session: ...

    @abstractmethod
    def get_session_by_digest(self, token_digest: str) -> Session | None: ...

    @abstractmethod
    def sessions_for_user(self, user_id: str) -> tuple[Session, ...]: ...

    @abstractmethod
    def save_recovery(self, request: AccountRecoveryRequest) -> None: ...

    @abstractmethod
    def get_recovery_by_digest(self, token_digest: str) -> AccountRecoveryRequest | None: ...

    @abstractmethod
    def save_support_grant(self, grant: SupportAccessGrant) -> None: ...

    @abstractmethod
    def get_support_grant(self, grant_id: str) -> SupportAccessGrant: ...

    @abstractmethod
    def list_support_grants_for_user(self, support_user_id: str) -> tuple[SupportAccessGrant, ...]:
        """Grants issued *to* this support user only. Never another user's grants."""

    @abstractmethod
    def save_support_session(self, session: SupportImpersonationSession) -> None: ...

    @abstractmethod
    def get_support_session(self, session_id: str) -> SupportImpersonationSession: ...

    @abstractmethod
    def append_audit(self, event: IdentityAuditEvent) -> None: ...

    @abstractmethod
    def list_audit(self, tenant_id: str) -> tuple[IdentityAuditEvent, ...]: ...


class InMemoryIdentityRepository(IdentityRepository):
    """Thread-safe offline adapter; keys and lookups always preserve tenant scope."""

    def __init__(self) -> None:
        self._lock = RLock()
        self.users: dict[str, User] = {}
        self.founder_profiles: dict[str, FounderProfile] = {}
        self.tenants: dict[str, Tenant] = {}
        self.organizations: dict[str, Organization] = {}
        self.memberships: dict[str, Membership] = {}
        self.sessions: dict[str, Session] = {}
        self.recoveries: dict[str, AccountRecoveryRequest] = {}
        self.support_grants: dict[str, SupportAccessGrant] = {}
        self.support_sessions: dict[str, SupportImpersonationSession] = {}
        self.audit_events: list[IdentityAuditEvent] = []

    def add_user(self, user: User) -> None:
        with self._lock:
            if user.user_id in self.users or self.get_user_by_email(user.email):
                raise IdentityConflict("user id or email already exists")
            if (
                user.authentication_provider
                and user.provider_subject_digest
                and self.get_user_by_external_identity(
                    user.authentication_provider, user.provider_subject_digest
                ) is not None
            ):
                raise IdentityConflict("external identity already exists")
            self.users[user.user_id] = user

    def save_user(self, user: User) -> None:
        with self._lock:
            if user.user_id not in self.users:
                raise IdentityNotFound("user not found")
            conflict = self.get_user_by_email(user.email)
            if conflict is not None and conflict.user_id != user.user_id:
                raise IdentityConflict("email already exists")
            if user.authentication_provider and user.provider_subject_digest:
                external = self.get_user_by_external_identity(
                    user.authentication_provider, user.provider_subject_digest
                )
                if external is not None and external.user_id != user.user_id:
                    raise IdentityConflict("external identity already exists")
            self.users[user.user_id] = user

    def get_user(self, user_id: str) -> User:
        try:
            return self.users[user_id]
        except KeyError as exc:
            raise IdentityNotFound("user not found") from exc

    def get_user_by_email(self, email: str) -> User | None:
        normalized = email.strip().lower()
        return next((user for user in self.users.values() if user.email == normalized), None)

    def get_user_by_external_identity(self, provider: str, subject_digest: str) -> User | None:
        return next(
            (
                user for user in self.users.values()
                if user.authentication_provider == provider
                and user.provider_subject_digest == subject_digest
            ),
            None,
        )

    def add_founder_profile(self, profile: FounderProfile) -> None:
        with self._lock:
            if profile.founder_profile_id in self.founder_profiles:
                raise IdentityConflict("founder profile already exists")
            if any(item.user_id == profile.user_id for item in self.founder_profiles.values()):
                raise IdentityConflict("user already has founder profile")
            self.get_user(profile.user_id)
            self.founder_profiles[profile.founder_profile_id] = profile

    def get_founder_profile_for_user(self, user_id: str) -> FounderProfile | None:
        return next((item for item in self.founder_profiles.values() if item.user_id == user_id), None)

    def add_tenant(self, tenant: Tenant) -> None:
        with self._lock:
            if tenant.tenant_id in self.tenants:
                raise IdentityConflict("tenant already exists")
            self.tenants[tenant.tenant_id] = tenant

    def save_tenant(self, tenant: Tenant) -> None:
        with self._lock:
            if tenant.tenant_id not in self.tenants:
                raise IdentityNotFound("tenant not found")
            self.tenants[tenant.tenant_id] = tenant

    def get_tenant(self, tenant_id: str) -> Tenant:
        try:
            return self.tenants[tenant_id]
        except KeyError as exc:
            raise IdentityNotFound("tenant not found") from exc

    def add_organization(self, organization: Organization) -> None:
        with self._lock:
            if organization.organization_id in self.organizations:
                raise IdentityConflict("organization already exists")
            if any(item.tenant_id == organization.tenant_id for item in self.organizations.values()):
                raise IdentityConflict("tenant already has an organization")
            self.get_tenant(organization.tenant_id)
            self.get_user(organization.owner_user_id)
            self.organizations[organization.organization_id] = organization

    def save_organization(self, organization: Organization) -> None:
        with self._lock:
            existing = self.get_organization(organization.organization_id)
            if existing.tenant_id != organization.tenant_id:
                raise IdentityConflict("organization tenant is immutable")
            self.organizations[organization.organization_id] = organization

    def get_organization(self, organization_id: str) -> Organization:
        try:
            return self.organizations[organization_id]
        except KeyError as exc:
            raise IdentityNotFound("organization not found") from exc

    def get_organization_by_tenant(self, tenant_id: str) -> Organization:
        item = next((item for item in self.organizations.values() if item.tenant_id == tenant_id), None)
        if item is None:
            raise IdentityNotFound("organization not found")
        return item

    def add_membership(self, membership: Membership) -> None:
        with self._lock:
            if membership.membership_id in self.memberships:
                raise IdentityConflict("membership already exists")
            if any(
                item.tenant_id == membership.tenant_id
                and item.user_id == membership.user_id
                and item.status is not MembershipStatus.REMOVED
                for item in self.memberships.values()
            ):
                raise IdentityConflict("user already has a pending or active membership")
            organization = self.get_organization(membership.organization_id)
            if organization.tenant_id != membership.tenant_id:
                raise IdentityConflict("membership tenant mismatch")
            self.get_user(membership.user_id)
            self.memberships[membership.membership_id] = membership

    def save_membership(self, membership: Membership) -> None:
        with self._lock:
            existing = self.get_membership(membership.membership_id)
            if (existing.tenant_id, existing.organization_id, existing.user_id) != (
                membership.tenant_id,
                membership.organization_id,
                membership.user_id,
            ):
                raise IdentityConflict("membership identity and scope are immutable")
            self.memberships[membership.membership_id] = membership

    def get_membership(self, membership_id: str) -> Membership:
        try:
            return self.memberships[membership_id]
        except KeyError as exc:
            raise IdentityNotFound("membership not found") from exc

    def get_active_membership(self, tenant_id: str, user_id: str) -> Membership | None:
        return next(
            (
                item
                for item in self.memberships.values()
                if item.tenant_id == tenant_id
                and item.user_id == user_id
                and item.status is MembershipStatus.ACTIVE
            ),
            None,
        )

    def list_memberships(self, tenant_id: str) -> tuple[Membership, ...]:
        return tuple(item for item in self.memberships.values() if item.tenant_id == tenant_id)

    def list_user_memberships(self, user_id: str) -> tuple[Membership, ...]:
        return tuple(item for item in self.memberships.values() if item.user_id == user_id)

    def save_session(self, session: Session) -> None:
        with self._lock:
            self.get_user(session.user_id)
            self.sessions[session.session_id] = session

    def get_session(self, session_id: str) -> Session:
        try:
            return self.sessions[session_id]
        except KeyError as exc:
            raise IdentityNotFound("session not found") from exc

    def get_session_by_digest(self, token_digest: str) -> Session | None:
        return next((item for item in self.sessions.values() if item.token_digest == token_digest), None)

    def sessions_for_user(self, user_id: str) -> tuple[Session, ...]:
        return tuple(item for item in self.sessions.values() if item.user_id == user_id)

    def save_recovery(self, request: AccountRecoveryRequest) -> None:
        with self._lock:
            self.get_user(request.user_id)
            self.recoveries[request.recovery_id] = request

    def get_recovery_by_digest(self, token_digest: str) -> AccountRecoveryRequest | None:
        return next((item for item in self.recoveries.values() if item.token_digest == token_digest), None)

    def save_support_grant(self, grant: SupportAccessGrant) -> None:
        with self._lock:
            self.get_tenant(grant.tenant_id)
            self.get_user(grant.support_user_id)
            self.get_user(grant.approved_by_user_id)
            self.support_grants[grant.grant_id] = grant

    def get_support_grant(self, grant_id: str) -> SupportAccessGrant:
        try:
            return self.support_grants[grant_id]
        except KeyError as exc:
            raise IdentityNotFound("support grant not found") from exc

    def list_support_grants_for_user(self, support_user_id: str) -> tuple[SupportAccessGrant, ...]:
        return tuple(
            sorted(
                (item for item in self.support_grants.values()
                 if item.support_user_id == support_user_id),
                key=lambda item: (item.ends_at, item.grant_id),
            )
        )

    def save_support_session(self, session: SupportImpersonationSession) -> None:
        with self._lock:
            grant = self.get_support_grant(session.grant_id)
            if (grant.tenant_id, grant.support_user_id) != (session.tenant_id, session.support_user_id):
                raise IdentityConflict("support session does not match grant")
            self.support_sessions[session.impersonation_session_id] = session

    def get_support_session(self, session_id: str) -> SupportImpersonationSession:
        try:
            return self.support_sessions[session_id]
        except KeyError as exc:
            raise IdentityNotFound("support session not found") from exc

    def append_audit(self, event: IdentityAuditEvent) -> None:
        with self._lock:
            if any(item.audit_event_id == event.audit_event_id for item in self.audit_events):
                raise IdentityConflict("audit event id already exists")
            self.audit_events.append(event)

    def list_audit(self, tenant_id: str) -> tuple[IdentityAuditEvent, ...]:
        return tuple(item for item in self.audit_events if item.tenant_id == tenant_id)


_IDENTITY_TYPES = {
    item.__name__: item
    for item in (
        User, FounderProfile, Tenant, Organization, Membership, Session,
        AccountRecoveryRequest, SupportAccessGrant, SupportImpersonationSession,
        IdentityAuditEvent,
    )
}

# Enum types are needed by the tagged JSON decoder.
from .models import MembershipStatus, OrganizationStatus, Permission, Role, TenantStatus, UserStatus
_IDENTITY_TYPES.update({item.__name__: item for item in (MembershipStatus, OrganizationStatus, Permission, Role, TenantStatus, UserStatus)})


class SQLiteIdentityRepository(InMemoryIdentityRepository):
    """Durable offline adapter using JSON records and append-only audit rows."""

    def __init__(self, path: str = ":memory:") -> None:
        super().__init__()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS identity_records (
                kind TEXT NOT NULL, record_key TEXT NOT NULL, tenant_id TEXT,
                body TEXT NOT NULL, PRIMARY KEY(kind, record_key)
            );
            CREATE INDEX IF NOT EXISTS identity_records_tenant ON identity_records(tenant_id, kind);
            CREATE TABLE IF NOT EXISTS identity_audit_events (
                audit_event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, body TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS identity_audit_no_update BEFORE UPDATE ON identity_audit_events
            BEGIN SELECT RAISE(ABORT, 'identity audit is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS identity_audit_no_delete BEFORE DELETE ON identity_audit_events
            BEGIN SELECT RAISE(ABORT, 'identity audit is append-only'); END;
            """
        )
        self._load()

    def close(self) -> None:
        self.connection.close()

    def _load(self) -> None:
        targets = {
            "user": self.users, "founder_profile": self.founder_profiles,
            "tenant": self.tenants, "organization": self.organizations,
            "membership": self.memberships, "session": self.sessions,
            "recovery": self.recoveries, "support_grant": self.support_grants,
            "support_session": self.support_sessions,
        }
        for kind, key, body in self.connection.execute("SELECT kind, record_key, body FROM identity_records"):
            targets[kind][key] = decode_record(body, _IDENTITY_TYPES)
        for (body,) in self.connection.execute("SELECT body FROM identity_audit_events ORDER BY rowid"):
            self.audit_events.append(decode_record(body, _IDENTITY_TYPES))

    def _put(self, kind: str, key: str, tenant_id: str | None, value: object) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO identity_records VALUES (?, ?, ?, ?) ON CONFLICT(kind, record_key) DO UPDATE SET tenant_id=excluded.tenant_id, body=excluded.body",
                (kind, key, tenant_id, encode_record(value)),
            )

    def add_user(self, user: User) -> None:
        super().add_user(user); self._put("user", user.user_id, None, user)

    def save_user(self, user: User) -> None:
        super().save_user(user); self._put("user", user.user_id, None, user)

    def add_founder_profile(self, profile: FounderProfile) -> None:
        super().add_founder_profile(profile); self._put("founder_profile", profile.founder_profile_id, None, profile)

    def add_tenant(self, tenant: Tenant) -> None:
        super().add_tenant(tenant); self._put("tenant", tenant.tenant_id, tenant.tenant_id, tenant)

    def save_tenant(self, tenant: Tenant) -> None:
        super().save_tenant(tenant); self._put("tenant", tenant.tenant_id, tenant.tenant_id, tenant)

    def add_organization(self, organization: Organization) -> None:
        super().add_organization(organization); self._put("organization", organization.organization_id, organization.tenant_id, organization)

    def save_organization(self, organization: Organization) -> None:
        super().save_organization(organization); self._put("organization", organization.organization_id, organization.tenant_id, organization)

    def add_membership(self, membership: Membership) -> None:
        super().add_membership(membership); self._put("membership", membership.membership_id, membership.tenant_id, membership)

    def save_membership(self, membership: Membership) -> None:
        super().save_membership(membership); self._put("membership", membership.membership_id, membership.tenant_id, membership)

    def save_session(self, session: Session) -> None:
        super().save_session(session); self._put("session", session.session_id, None, session)

    def save_recovery(self, request: AccountRecoveryRequest) -> None:
        super().save_recovery(request); self._put("recovery", request.recovery_id, None, request)

    def save_support_grant(self, grant: SupportAccessGrant) -> None:
        super().save_support_grant(grant); self._put("support_grant", grant.grant_id, grant.tenant_id, grant)

    def save_support_session(self, session: SupportImpersonationSession) -> None:
        super().save_support_session(session); self._put("support_session", session.impersonation_session_id, session.tenant_id, session)

    def append_audit(self, event: IdentityAuditEvent) -> None:
        super().append_audit(event)
        with self.connection:
            self.connection.execute(
                "INSERT INTO identity_audit_events VALUES (?, ?, ?)",
                (event.audit_event_id, event.tenant_id, encode_record(event)),
            )
