from __future__ import annotations

from datetime import datetime, timezone
import os
import unittest
from uuid import uuid4

from businessbuilder.identity import ProductionFounderSessionService, VerifiedExternalIdentity
from businessbuilder.postgres import PostgresIdentityRepository
from businessbuilder.runtime.ids import DeterministicIds

NOW = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc)


class Provider:
    name = "cognito"

    def verify_access_token(self, token: str) -> VerifiedExternalIdentity:
        del token
        return VerifiedExternalIdentity(
            "cognito", "postgres-founder-subject", "postgres-auth@example.test", True,
            "Postgres Founder", NOW, "provider-session-postgres",
        )


@unittest.skipUnless(os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"), "requires isolated PostgreSQL")
class PostgresProductionAuthTests(unittest.TestCase):
    def test_identity_session_rotation_and_revocation_survive_restart(self) -> None:
        dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        schema = f"{prefix}_auth_{uuid4().hex[:10]}"
        ids = DeterministicIds()
        first_repo = PostgresIdentityRepository(dsn, schema=schema)
        first = ProductionFounderSessionService(first_repo, Provider(), id_factory=ids, clock=lambda: NOW)
        user, session, raw = first.establish("provider-access-token")
        first_repo.close()

        reopened_repo = PostgresIdentityRepository(dsn, schema=schema)
        reopened = ProductionFounderSessionService(reopened_repo, Provider(), id_factory=ids, clock=lambda: NOW)
        self.assertEqual(user.user_id, reopened.sessions.validate_token(raw).user_id)
        same, replacement, rotated = reopened.rotate(raw, "provider-access-token")
        self.assertEqual(user.user_id, same.user_id)
        self.assertEqual(session.session_id, replacement.rotated_from_session_id)
        with self.assertRaises(Exception):
            reopened.sessions.validate_token(raw)
        reopened_repo.close()

        final_repo = PostgresIdentityRepository(dsn, schema=schema)
        final = ProductionFounderSessionService(final_repo, Provider(), id_factory=ids, clock=lambda: NOW)
        self.assertEqual(user.user_id, final.sessions.validate_token(rotated).user_id)
        same_again, _, _ = final.establish("provider-access-token")
        self.assertEqual(user.user_id, same_again.user_id)
        self.assertEqual(1, len([item for item in final_repo.users.values() if item.email == user.email]))
        final.revoke(rotated)
        final_repo.close()

        last_repo = PostgresIdentityRepository(dsn, schema=schema)
        last = ProductionFounderSessionService(last_repo, Provider(), id_factory=ids, clock=lambda: NOW)
        with self.assertRaises(Exception):
            last.sessions.validate_token(rotated)
        last_repo.close()


if __name__ == "__main__":
    unittest.main()
