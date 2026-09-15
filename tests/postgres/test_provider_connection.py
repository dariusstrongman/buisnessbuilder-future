from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import uuid4

from businessbuilder.postgres import PostgresRuntimeRepository
from businessbuilder.provider_connection.models import OAuthTransaction, digest_text
from businessbuilder.provider_connection.models import ConnectionCommand, ConnectionHealthState, ProviderHealth
from businessbuilder.access_broker.models import ConnectionStatus, ProviderConnection
from businessbuilder.provider_connection.connections import safe_connection


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

    def test_connection_health_and_opaque_refs_survive_restart_and_isolate_tenants(self):
        first = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        connection = ProviderConnection(
            "connection_dashboard_pg", "account_dashboard_pg", "tenant_pg", "company_pg",
            "sandbox-calendar", ConnectionStatus.ACTIVE, NOW, NOW,
            account_type="calendar", provider_account_id="calendar_business_001",
            scopes_requested=frozenset({"calendar.read"}),
            scopes_granted=frozenset({"calendar.read"}),
            connected_at=NOW, capability="CALENDAR",
            secret_ref_ids=("secretref_dashboard_pg",),
        )
        health = ProviderHealth(
            connection.connection_id, connection.tenant_id, connection.company_id, True,
            1, 0, 0, 0, None, None, NOW, NOW,
            ConnectionHealthState.WARNING, NOW, None, None, 2, "requests", NOW,
        )
        command = ConnectionCommand("tenant_pg", "company_pg", "pg-connection-command-001",
                                    connection.provider, connection.provider_account_id,
                                    connection.connection_id, NOW)
        first.save_broker_record("provider_connection", connection.connection_id,
                                 connection.tenant_id, connection.company_id, connection)
        first.save_broker_record("provider_health", connection.connection_id,
                                 connection.tenant_id, connection.company_id, health)
        first.save_broker_record("connection_command", "connection_command_dashboard_pg",
                                 connection.tenant_id, connection.company_id, command)
        first.close()
        reopened = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        durable_connection = reopened.get_broker_record("provider_connection", "tenant_pg", "company_pg", connection.connection_id)
        durable_health = reopened.get_broker_record("provider_health", "tenant_pg", "company_pg", connection.connection_id)
        self.assertEqual(connection, durable_connection)
        self.assertEqual(health, durable_health)
        self.assertEqual(command, reopened.get_broker_record("connection_command", "tenant_pg",
                                                              "company_pg", "connection_command_dashboard_pg"))
        view = safe_connection(durable_connection, durable_health, at=NOW)
        self.assertEqual("WARNING", view["health"])
        self.assertNotIn("secretref_dashboard_pg", str(view))
        self.assertIsNone(reopened.get_broker_record("provider_connection", "tenant_other", "company_pg", connection.connection_id))
        self.assertIsNone(reopened.get_broker_record("provider_health", "tenant_pg", "company_other", connection.connection_id))
        self.assertIsNone(reopened.get_broker_record("connection_command", "tenant_other", "company_pg",
                                                    "connection_command_dashboard_pg"))
        reopened.close()

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
