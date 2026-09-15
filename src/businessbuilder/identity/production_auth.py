from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Callable, Protocol

from .exceptions import AuthorizationDenied, IdentityConflict
from .models import Session, User, UserStatus
from .ports import ExternalIdentityProvider, VerifiedExternalIdentity
from .repository import IdentityRepository
from .service import IdentityService, SessionService


_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class CognitoGetUserClient(Protocol):
    def get_user(self, *, AccessToken: str) -> dict[str, Any]: ...


class CognitoAuthenticationAdapter:
    """Validates Cognito access tokens without importing Cognito group authority.

    Cognito GetUser performs provider-side token validation and revocation checks.
    Claims are then narrowed to the configured pool/client and only identity
    attributes are returned. Roles, tenant and company claims are ignored.
    """

    name = "cognito"

    def __init__(
        self,
        client: CognitoGetUserClient,
        *,
        region: str,
        user_pool_id: str,
        app_client_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not all((region.strip(), user_pool_id.strip(), app_client_id.strip())):
            raise ValueError("complete Cognito configuration required")
        self.client = client
        self.region = region.strip()
        self.user_pool_id = user_pool_id.strip()
        self.app_client_id = app_client_id.strip()
        self.issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"
        self.clock = clock

    def verify_access_token(self, access_token: str) -> VerifiedExternalIdentity:
        if not isinstance(access_token, str) or not 40 <= len(access_token) <= 16_384:
            raise AuthorizationDenied("invalid provider assertion")
        try:
            response = self.client.get_user(AccessToken=access_token)
            claims = _jwt_payload(access_token)
        except Exception as exc:
            raise AuthorizationDenied("invalid provider assertion") from exc
        now = int(self.clock().timestamp())
        if (
            claims.get("iss") != self.issuer
            or claims.get("client_id") != self.app_client_id
            or claims.get("token_use") != "access"
            or not isinstance(claims.get("exp"), int)
            or claims["exp"] <= now
        ):
            raise AuthorizationDenied("provider token scope is invalid")
        attributes = {
            item.get("Name"): item.get("Value")
            for item in response.get("UserAttributes", [])
            if isinstance(item, dict)
        }
        subject = attributes.get("sub") or claims.get("sub")
        email = str(attributes.get("email") or "").strip().lower()
        verified = str(attributes.get("email_verified") or "").lower() == "true"
        if not isinstance(subject, str) or not subject or not _EMAIL.fullmatch(email):
            raise AuthorizationDenied("provider identity is incomplete")
        if claims.get("sub") not in {None, subject}:
            raise AuthorizationDenied("provider subject mismatch")
        auth_time = claims.get("auth_time")
        authenticated_at = (
            datetime.fromtimestamp(auth_time, timezone.utc)
            if isinstance(auth_time, int) and 0 < auth_time <= now
            else self.clock()
        )
        display_name = str(
            attributes.get("name") or attributes.get("given_name") or email.split("@", 1)[0]
        ).strip()[:160]
        provider_session_id = str(claims.get("origin_jti") or claims.get("jti") or "")
        if not provider_session_id:
            raise AuthorizationDenied("provider session identity missing")
        return VerifiedExternalIdentity(
            self.name,
            subject,
            email,
            verified,
            display_name,
            authenticated_at,
            provider_session_id,
        )


class ProductionFounderSessionService:
    """Binds a verified external identity to one internal Business Builder user."""

    def __init__(
        self,
        repository: IdentityRepository,
        provider: ExternalIdentityProvider,
        *,
        id_factory: Callable[[str], str],
        clock: Callable[[], datetime],
        session_lifetime: timedelta = timedelta(minutes=15),
    ) -> None:
        if not timedelta(minutes=5) <= session_lifetime <= timedelta(hours=1):
            raise ValueError("production session lifetime must be between 5 and 60 minutes")
        self.repository = repository
        self.provider = provider
        self.clock = clock
        self.session_lifetime = session_lifetime
        self.identity = IdentityService(repository, id_factory=id_factory, clock=clock)
        self.sessions = SessionService(repository, provider=_NoCredentialProvider(), id_factory=id_factory, clock=clock)

    def establish(self, access_token: str) -> tuple[User, Session, str]:
        asserted = self.provider.verify_access_token(access_token)
        user = self._resolve_founder(asserted)
        session, raw_token = self.sessions.issue_for_user(
            user.user_id,
            provider_name=asserted.provider,
            provider_session_id=asserted.provider_session_id,
            lifetime=self.session_lifetime,
        )
        return user, session, raw_token

    def rotate(self, raw_session: str, access_token: str) -> tuple[User, Session, str]:
        current_user = self.sessions.validate_token(raw_session)
        asserted = self.provider.verify_access_token(access_token)
        subject_digest = _subject_digest(asserted.provider, asserted.subject)
        mapped = self.repository.get_user_by_external_identity(asserted.provider, subject_digest)
        if mapped is None or mapped.user_id != current_user.user_id or not asserted.email_verified:
            raise AuthorizationDenied("provider identity does not match current session")
        session, replacement = self.sessions.rotate(
            raw_session,
            provider_name=asserted.provider,
            provider_session_id=asserted.provider_session_id,
            lifetime=self.session_lifetime,
        )
        return current_user, session, replacement

    def revoke(self, raw_session: str) -> None:
        self.sessions.sign_out(raw_session)

    def start_support_session(self, raw_session: str, grant_id: str, reason: str):
        user = self.sessions.validate_token(raw_session)
        return self.identity.start_support_session(user.user_id, grant_id, reason)

    def _resolve_founder(self, asserted: VerifiedExternalIdentity) -> User:
        if asserted.provider != self.provider.name or not asserted.email_verified:
            raise AuthorizationDenied("verified email required")
        subject_digest = _subject_digest(asserted.provider, asserted.subject)
        user = self.repository.get_user_by_external_identity(asserted.provider, subject_digest)
        if user is not None:
            if user.email != asserted.email or user.status is not UserStatus.ACTIVE:
                raise AuthorizationDenied("external identity requires operator reconciliation")
            return user

        user = self.repository.get_user_by_email(asserted.email)
        if user is not None:
            if user.status is not UserStatus.ACTIVE:
                raise AuthorizationDenied("deactivated user")
            if user.authentication_provider not in {None, asserted.provider}:
                raise AuthorizationDenied("email is already bound to another provider")
            if user.provider_subject_digest not in {None, subject_digest}:
                raise AuthorizationDenied("email is already bound to another identity")
            user = replace(
                user,
                email_verified_at=user.email_verified_at or self.clock(),
                authentication_provider=asserted.provider,
                provider_subject_digest=subject_digest,
            )
            self.repository.save_user(user)
        else:
            stable = sha256(f"{asserted.provider}:{asserted.subject}".encode()).hexdigest()[:24]
            user = User(
                f"user_auth_{stable}",
                asserted.email,
                email_verified_at=self.clock(),
                created_at=self.clock(),
                authentication_provider=asserted.provider,
                provider_subject_digest=subject_digest,
            )
            try:
                self.repository.add_user(user)
            except IdentityConflict:
                existing = self.repository.get_user_by_external_identity(asserted.provider, subject_digest)
                if existing is None or existing.email != asserted.email:
                    raise AuthorizationDenied("identity bootstrap conflict") from None
                user = existing

        if self.repository.get_founder_profile_for_user(user.user_id) is None:
            stable = sha256(user.user_id.encode()).hexdigest()[:24]
            try:
                self.identity.create_founder_profile(
                    user.user_id,
                    asserted.display_name,
                    founder_profile_id=f"founder_auth_{stable}",
                )
            except IdentityConflict:
                if self.repository.get_founder_profile_for_user(user.user_id) is None:
                    raise
        return user


class _NoCredentialProvider:
    """Production sessions are issued only through verified external identities."""

    def authenticate(self, email: str, proof: str) -> str | None:
        del email, proof
        return None

    def begin_email_verification(self, user_id: str, email: str) -> str:
        del user_id, email
        raise AuthorizationDenied("email verification is provider managed")

    def complete_email_verification(self, challenge_ref: str, proof: str) -> str | None:
        del challenge_ref, proof
        return None

    def update_recovery_credential(self, user_id: str, new_proof: str) -> None:
        del user_id, new_proof
        raise AuthorizationDenied("account recovery is provider managed")


def _subject_digest(provider: str, subject: str) -> str:
    return "sha256:" + sha256(f"{provider}:{subject}".encode()).hexdigest()


def _jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or len(parts[1]) > 12_000:
        raise ValueError("malformed token")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    value = json.loads(base64.urlsafe_b64decode(padded.encode()))
    if not isinstance(value, dict):
        raise ValueError("malformed token")
    return value


def cognito_authentication_from_environment(
    *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
) -> CognitoAuthenticationAdapter | None:
    """Create the production adapter only for an explicit complete configuration."""
    selected = os.environ.get("BUSINESS_BUILDER_AUTH_PROVIDER", "").strip().lower()
    if not selected:
        return None
    if selected != "cognito":
        raise RuntimeError("unsupported BUSINESS_BUILDER_AUTH_PROVIDER")
    required = {
        "region": os.environ.get("AWS_REGION", "").strip(),
        "user_pool_id": os.environ.get("COGNITO_USER_POOL_ID", "").strip(),
        "app_client_id": os.environ.get("COGNITO_APP_CLIENT_ID", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("Cognito authentication configuration is incomplete")
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "cognito-idp",
        region_name=required["region"],
        config=Config(retries={"mode": "standard", "max_attempts": 2}),
    )
    return CognitoAuthenticationAdapter(client, clock=clock, **required)
