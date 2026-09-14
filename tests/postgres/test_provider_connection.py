from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import uuid4

from businessbuilder.postgres import PostgresRuntimeRepository
from businessbuilder.provider_connection.models import OAuthTransaction, digest_text


NOW = datetime(2026, 9, 13, 22, 0, tzinfo=timezone.utc)


@unittest.skipUnless(os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"),
                     "requires an isolated PostgreSQL test database")
class PostgresProviderConnectionTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        self.schema = f"{prefix}_oauth_{uuid4().hex[:10]}"

    def test_oauth_state_is_durable_one_time_and_session_bound(self):
        first = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        transaction = OAuthTransaction(
            "oauth_flow_pg", digest_text("state-pg"), digest_text("verifier-pg"),
            "session_pg", "tenant_pg", "company_pg", "connection_pg",
            "sandbox-email", digest_text("https://app.example.test/callback"),
            digest_text("nonce-pg"), frozenset({"mail.read"}), NOW, NOW + timedelta(minutes=10),
        )
        first.save_oauth_transaction(transaction)
        first.close()
        second = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        self.assertEqual(transaction, second.get_oauth_transaction(transaction.state_digest))
        args = (transaction.state_digest, transaction.session_id,
                transaction.redirect_uri_digest, transaction.pkce_verifier_digest)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: second.consume_oauth_transaction(*args, at=NOW), range(2)))
        self.assertEqual(1, sum(value is not None for value in results))
        self.assertIsNone(second.consume_oauth_transaction(
            transaction.state_digest, "wrong_session", transaction.redirect_uri_digest,
            transaction.pkce_verifier_digest, at=NOW))
        second.close()

    def test_refresh_lease_and_callback_dedupe_are_concurrency_safe(self):
        repositories = [PostgresRuntimeRepository(self.dsn, schema=self.schema) for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            refresh = list(pool.map(lambda repo: repo.claim_provider_refresh(
                "tenant_pg", "company_pg", "connection_pg", str(id(repo)),
                at=NOW, lease=timedelta(seconds=30)), repositories))
        self.assertEqual(1, sum(refresh))
        with ThreadPoolExecutor(max_workers=2) as pool:
            callbacks = list(pool.map(lambda repo: repo.claim_provider_callback(
                "sandbox-email", "callback_pg_001"), repositories))
        self.assertEqual(1, sum(callbacks))
        for repository in repositories: repository.close()

