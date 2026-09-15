from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from businessbuilder.identity import (
    AuthorizationDenied,
    CognitoAuthenticationAdapter,
    InMemoryIdentityRepository,
    ProductionFounderSessionService,
    UserStatus,
    VerifiedExternalIdentity,
)
from businessbuilder.runtime.ids import DeterministicIds


NOW = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc)


class FakeExternalProvider:
    name = "cognito"

    def __init__(self) -> None:
        self.identities = {
            "verified": VerifiedExternalIdentity(
                "cognito", "subject-founder", "founder@example.test", True,
                "Founder", NOW, "provider-session-1",
            ),
            "unverified": VerifiedExternalIdentity(
                "cognito", "subject-unverified", "waiting@example.test", False,
                "Waiting", NOW, "provider-session-2",
            ),
            "other": VerifiedExternalIdentity(
                "cognito", "subject-other", "other@example.test", True,
                "Other", NOW, "provider-session-3",
            ),
        }

    def verify_access_token(self, access_token: str) -> VerifiedExternalIdentity:
        try:
            return self.identities[access_token]
        except KeyError as exc:
            raise AuthorizationDenied("invalid provider assertion") from exc


def service(repo: InMemoryIdentityRepository | None = None):
    repository = repo or InMemoryIdentityRepository()
    value = ProductionFounderSessionService(
        repository,
        FakeExternalProvider(),
        id_factory=DeterministicIds(),
        clock=lambda: NOW,
    )
    return repository, value


def test_verified_provider_identity_bootstraps_one_founder_and_rotates_session() -> None:
    repo, auth = service()
    user, first, raw_first = auth.establish("verified")
    assert user.email == "founder@example.test"
    assert user.email_verified_at == NOW
    assert user.authentication_provider == "cognito"
    assert user.provider_subject_digest.startswith("sha256:")
    assert repo.get_founder_profile_for_user(user.user_id) is not None
    assert repo.list_user_memberships(user.user_id) == ()

    same, concurrent, raw_concurrent = auth.establish("verified")
    assert same.user_id == user.user_id
    assert concurrent.session_id != first.session_id

    _, rotated, raw_rotated = auth.rotate(raw_first, "verified")
    assert rotated.rotated_from_session_id == first.session_id
    with pytest.raises(AuthorizationDenied):
        auth.sessions.validate_token(raw_first)
    assert auth.sessions.validate_token(raw_rotated).user_id == user.user_id
    assert auth.sessions.validate_token(raw_concurrent).user_id == user.user_id
    actions = {event.action for event in repo.list_audit("unresolved")}
    assert {"session.established", "session.revoked"} <= actions


def test_unverified_disabled_and_cross_identity_rotation_fail_closed() -> None:
    repo, auth = service()
    with pytest.raises(AuthorizationDenied, match="verified email"):
        auth.establish("unverified")
    user, _, raw = auth.establish("verified")
    with pytest.raises(AuthorizationDenied, match="does not match"):
        auth.rotate(raw, "other")
    repo.save_user(replace(user, status=UserStatus.DEACTIVATED, deactivated_at=NOW))
    with pytest.raises(AuthorizationDenied):
        auth.establish("verified")
    with pytest.raises(AuthorizationDenied):
        auth.sessions.validate_token(raw)


def test_duplicate_founder_bootstrap_race_is_idempotent() -> None:
    repo, auth = service()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: auth.establish("verified")[0].user_id, range(2)))
    assert results[0] == results[1]
    assert len(repo.users) == 1
    assert len(repo.founder_profiles) == 1


def test_rotation_storage_failure_leaves_old_session_revoked() -> None:
    class FailingRepository(InMemoryIdentityRepository):
        fail_replacement = False

        def save_session(self, session):
            if self.fail_replacement and session.rotated_from_session_id and session.revoked_at is None:
                raise RuntimeError("simulated storage failure")
            return super().save_session(session)

    repo = FailingRepository()
    _, auth = service(repo)
    _, _, raw = auth.establish("verified")
    repo.fail_replacement = True
    with pytest.raises(RuntimeError, match="storage failure"):
        auth.rotate(raw, "verified")
    with pytest.raises(AuthorizationDenied):
        auth.sessions.validate_token(raw)


class FakeCognitoClient:
    def get_user(self, *, AccessToken: str):
        if AccessToken == "rejected":
            raise RuntimeError("not authorized")
        return {
            "UserAttributes": [
                {"Name": "sub", "Value": "subject-founder"},
                {"Name": "email", "Value": "Founder@Example.Test"},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name", "Value": "Founder"},
            ]
        }


def jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_cognito_adapter_checks_pool_client_use_expiry_and_verified_attributes() -> None:
    adapter = CognitoAuthenticationAdapter(
        FakeCognitoClient(), region="us-east-1", user_pool_id="pool", app_client_id="client",
        clock=lambda: NOW,
    )
    valid = {
        "iss": "https://cognito-idp.us-east-1.amazonaws.com/pool",
        "client_id": "client", "token_use": "access", "sub": "subject-founder",
        "exp": int((NOW + timedelta(minutes=5)).timestamp()),
        "auth_time": int(NOW.timestamp()), "origin_jti": "origin-session",
        "cognito:groups": ["SUPPORT"],
    }
    identity = adapter.verify_access_token(jwt(valid))
    assert identity.email == "founder@example.test"
    assert not hasattr(identity, "role")
    for change in (
        {"client_id": "forged"}, {"iss": "https://other.invalid"},
        {"token_use": "id"}, {"exp": int((NOW - timedelta(seconds=1)).timestamp())},
    ):
        with pytest.raises(AuthorizationDenied):
            adapter.verify_access_token(jwt({**valid, **change}))
