from __future__ import annotations

from typing import Protocol


class AuthenticationProvider(Protocol):
    """Provider-neutral authentication boundary. Credentials never enter domain storage."""

    def authenticate(self, email: str, proof: str) -> str | None:
        """Return the authenticated user_id, or None when proof is invalid."""

    def begin_email_verification(self, user_id: str, email: str) -> str:
        """Return an opaque provider challenge reference."""

    def complete_email_verification(self, challenge_ref: str, proof: str) -> str | None:
        """Return the verified user_id, or None when the challenge is invalid."""

    def update_recovery_credential(self, user_id: str, new_proof: str) -> None: ...


class FutureMfaProvider(Protocol):
    def begin_enrollment(self, user_id: str) -> str: ...

    def verify_challenge(self, user_id: str, challenge_ref: str, proof: str) -> bool: ...


class FakeDevAuthenticationProvider:
    """Offline passwordless assertion adapter; stores only assertion digests."""

    def __init__(self) -> None:
        from hashlib import sha256

        self._sha256 = sha256
        self._assertions: dict[str, tuple[str, str]] = {}
        self._email_challenges: dict[str, str] = {}

    def register(self, user_id: str, email: str, assertion: str) -> None:
        self._assertions[email.strip().lower()] = (user_id, self._digest(assertion))

    def authenticate(self, email: str, proof: str) -> str | None:
        record = self._assertions.get(email.strip().lower())
        return record[0] if record and record[1] == self._digest(proof) else None

    def begin_email_verification(self, user_id: str, email: str) -> str:
        reference = f"dev_email_challenge_{len(self._email_challenges) + 1}"
        self._email_challenges[reference] = user_id
        return reference

    def complete_email_verification(self, challenge_ref: str, proof: str) -> str | None:
        return self._email_challenges.get(challenge_ref) if proof else None

    def update_recovery_credential(self, user_id: str, new_proof: str) -> None:
        for email, (current_user_id, _) in tuple(self._assertions.items()):
            if current_user_id == user_id:
                self._assertions[email] = (user_id, self._digest(new_proof))
                return
        raise LookupError("authentication identity not found")

    def _digest(self, value: str) -> str:
        return self._sha256(value.encode()).hexdigest()
