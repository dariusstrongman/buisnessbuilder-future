"""Business Builder staging API backed by PostgreSQL."""

from __future__ import annotations

import json
import os
import uuid
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from businessbuilder.postgres.cloud_proof import get_cloud_proof, run_cloud_proof
from businessbuilder.postgres.connection import connect_postgres
from businessbuilder.postgres.migrations import migrate


ROOT = Path(__file__).resolve().parent
PROTOTYPE = ROOT / "prototype"
APP_VERSION = os.environ.get("APP_VERSION", "dev")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "staging")
INSTANCE_ID = uuid.uuid4().hex


def _database_health() -> None:
    connection = connect_postgres(application_name="businessbuilder-health")
    try:
        migrate(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 AS healthy")
            if cursor.fetchone()["healthy"] != 1:
                raise RuntimeError("PostgreSQL health query failed")
    finally:
        connection.close()


class StagingHandler(SimpleHTTPRequestHandler):
    server_version = "BusinessBuilderStaging/1"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(PROTOTYPE), **kwargs)

    def _json(self, status: HTTPStatus, body: dict[str, object]) -> None:
        payload = json.dumps(body, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path == "/healthz":
            try:
                _database_health()
                self._json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": "businessbuilder-staging",
                        "environment": ENVIRONMENT,
                        "version": APP_VERSION,
                        "instance_id": INSTANCE_ID,
                        "persistence": "postgresql",
                    },
                )
            except Exception as exc:
                self.log_error("database health failed: %s", exc)
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "status": "unhealthy",
                        "service": "businessbuilder-staging",
                        "persistence": "postgresql",
                    },
                )
            return
        if path in {"/proof", "/api/v1/proofs/billy-bob-commercial"}:
            try:
                self._json(HTTPStatus.OK, run_cloud_proof())
            except Exception as exc:
                self.log_error("PostgreSQL commercial proof failed: %s", exc)
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "status": "failed",
                        "proof": "billy-bob-commercial-postgres-v1",
                    },
                )
            return
        if path == "/api/v1/proofs/billy-bob-commercial/state":
            proof = get_cloud_proof()
            if proof is None:
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"status": "not_run", "backend": "postgresql"},
                )
            else:
                self._json(HTTPStatus.OK, proof)
            return
        if path == "/api/v1":
            self._json(
                HTTPStatus.OK,
                {
                    "service": "businessbuilder-staging",
                    "api_version": "v1",
                    "persistence": "postgresql",
                    "billing_provider": "simulated",
                    "readiness_authority": "verification",
                },
            )
            return
        super().do_GET()


if __name__ == "__main__":
    _database_health()
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), StagingHandler)
    print(f"Business Builder staging listening on :{port}", flush=True)
    server.serve_forever()
