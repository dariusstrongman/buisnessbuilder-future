from __future__ import annotations

from contextlib import redirect_stderr
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import StringIO
import json
import threading
import unittest
from unittest.mock import Mock, patch

import staging_server
from businessbuilder.postgres.migrations import DDL, MIGRATION_LOCK_KEY, migrate


class StagingServerSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.health = patch.object(staging_server, "_database_health")
        self.health.start()
        self.addCleanup(self.health.stop)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), staging_server.StagingHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_proof_routes_are_hidden_and_cannot_execute_by_default(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {"ENVIRONMENT": "staging", "ENABLE_PROOF_ENDPOINTS": "true"},
                clear=False,
            ),
            patch.object(staging_server, "run_cloud_proof") as run_proof,
            patch.object(staging_server, "get_cloud_proof") as get_proof,
        ):
            for path in (
                "/proof",
                "/api/v1/proofs/billy-bob-commercial",
                "/api/v1/proofs/billy-bob-commercial/state",
            ):
                status, _, body = self.request("GET", path)
                self.assertEqual(404, status)
                self.assertEqual({"status": "not_found"}, json.loads(body))
        run_proof.assert_not_called()
        get_proof.assert_not_called()

    def test_supervised_stripe_test_checkout_is_pilot_only_and_fail_closed(self) -> None:
        for environment in ("production", "staging"):
            with patch.dict("os.environ", {"ENVIRONMENT": environment, "PILOT_SUPERVISED_STRIPE_TEST": "1"}):
                with self.assertRaises(RuntimeError):
                    staging_server._stripe_test_checkout_enabled(configured=True)
        with patch.dict("os.environ", {"ENVIRONMENT": "pilot", "PILOT_SUPERVISED_STRIPE_TEST": "1"}):
            with self.assertRaises(RuntimeError):
                staging_server._stripe_test_checkout_enabled(configured=False)
            self.assertTrue(staging_server._stripe_test_checkout_enabled(configured=True))

    def test_offline_proof_remains_available_only_with_explicit_opt_in(self) -> None:
        proof = {"status": "passed", "proof": "fixture"}
        with (
            patch.dict(
                "os.environ",
                {"ENVIRONMENT": "development", "ENABLE_PROOF_ENDPOINTS": "true"},
                clear=False,
            ),
            patch.object(staging_server, "run_cloud_proof", return_value=proof),
            patch.object(staging_server, "get_cloud_proof", return_value=proof),
        ):
            execution = self.request("GET", "/proof")
            state = self.request(
                "GET", "/api/v1/proofs/billy-bob-commercial/state"
            )
        self.assertEqual((200, proof), (execution[0], json.loads(execution[2])))
        self.assertEqual((200, proof), (state[0], json.loads(state[2])))

    def test_health_is_minimal_and_every_response_is_hardened(self) -> None:
        status, headers, body = self.request(
            "GET", "/healthz", headers={"Origin": "https://attacker.invalid"}
        )
        self.assertEqual(200, status)
        self.assertEqual({"status": "ok"}, json.loads(body))
        self.assertEqual("BusinessBuilder", headers["Server"])
        self.assertNotIn("Python", headers["Server"])
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])
        self.assertEqual("DENY", headers["X-Frame-Options"])
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual("same-origin", headers["Cross-Origin-Resource-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        serialized = body.decode().lower()
        for forbidden in ("environment", "version", "instance", "postgres"):
            self.assertNotIn(forbidden, serialized)

    def test_failures_and_query_strings_do_not_disclose_sensitive_values(self) -> None:
        marker = "credential-value-must-not-leak"
        captured = StringIO()
        with (
            patch.dict(
                "os.environ",
                {"ENVIRONMENT": "development", "ENABLE_PROOF_ENDPOINTS": "true"},
                clear=False,
            ),
            patch.object(
                staging_server,
                "run_cloud_proof",
                side_effect=RuntimeError(marker),
            ),
            redirect_stderr(captured),
        ):
            status, _, body = self.request("GET", f"/proof?token={marker}")
        self.assertEqual(500, status)
        self.assertNotIn(marker, body.decode())
        self.assertNotIn(marker, captured.getvalue())
        self.assertIn("path=/proof", captured.getvalue())

    def test_static_files_are_allowlisted_and_directories_are_not_listed(self) -> None:
        index = self.request("GET", "/")
        denied = self.request("GET", "/missing/")
        encoded = self.request("GET", "/%2e%2e/docs/MASTER_ARCHITECTURE.md")
        self.assertEqual(200, index[0])
        self.assertEqual(404, denied[0])
        self.assertEqual(404, encoded[0])
        self.assertNotIn(b"Directory listing", denied[2])

    def test_unsupported_methods_and_oversized_requests_fail_closed(self) -> None:
        unsupported = self.request(
            "POST",
            "/api/v1",
            body=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        oversized = self.request(
            "POST",
            "/api/v1",
            headers={
                "Content-Length": str(staging_server.MAX_REQUEST_BODY_BYTES + 1)
            },
        )
        self.assertEqual(405, unsupported[0])
        self.assertEqual(413, oversized[0])
        self.assertEqual("GET, HEAD, OPTIONS", unsupported[1]["Allow"])
        self.assertIn("Content-Security-Policy", unsupported[1])


class DatabaseHealthSecurityTests(unittest.TestCase):
    def test_health_connection_cannot_create_or_migrate_schema(self) -> None:
        cursor = Mock()
        cursor.fetchone.return_value = {"healthy": 1, "migrated": True}
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        connection = Mock()
        connection.cursor.return_value = cursor
        with (
            patch.object(
                staging_server, "connect_postgres", return_value=connection
            ) as connect,
            patch.object(staging_server, "migrate") as migrate,
        ):
            staging_server._database_health()
        connect.assert_called_once_with(
            application_name="businessbuilder-health", ensure_schema=False
        )
        migrate.assert_not_called()
        self.assertNotIn("CREATE", cursor.execute.call_args.args[0].upper())
        connection.close.assert_called_once_with()

    def test_migration_is_locked_and_existing_version_is_a_no_op(self) -> None:
        cursor = Mock()
        cursor.fetchone.return_value = {"applied": 1}
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        transaction = Mock()
        transaction.__enter__ = Mock(return_value=transaction)
        transaction.__exit__ = Mock(return_value=False)
        connection = Mock()
        connection.cursor.return_value = cursor
        connection.transaction.return_value = transaction

        migrate(connection)

        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("pg_advisory_xact_lock", statements[0])
        self.assertEqual((MIGRATION_LOCK_KEY,), cursor.execute.call_args_list[0].args[1])
        self.assertNotIn(DDL, statements)

    def test_new_migration_version_applies_once_inside_the_lock(self) -> None:
        cursor = Mock()
        cursor.fetchone.return_value = None
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        transaction = Mock()
        transaction.__enter__ = Mock(return_value=transaction)
        transaction.__exit__ = Mock(return_value=False)
        connection = Mock()
        connection.cursor.return_value = cursor
        connection.transaction.return_value = transaction

        migrate(connection)

        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn(DDL, statements)
        self.assertTrue(
            any("INSERT INTO bb_schema_migrations" in item for item in statements)
        )


if __name__ == "__main__":
    unittest.main()
