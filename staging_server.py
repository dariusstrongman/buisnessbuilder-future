"""Business Builder staging API backed by PostgreSQL."""

from __future__ import annotations

import json
import os
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from businessbuilder.customer_api.bootstrap import create_postgres_customer_api
from businessbuilder.access_broker.adapters import S3ArtifactStore
from businessbuilder.commercial.stripe_test import StripeTestPaymentProvider
from businessbuilder.identity import cognito_authentication_from_environment
from businessbuilder.postgres.cloud_proof import get_cloud_proof, run_cloud_proof
from businessbuilder.postgres.connection import connect_postgres
from businessbuilder.postgres.migrations import migrate


ROOT = Path(__file__).resolve().parent
PROTOTYPE = ROOT / "prototype"
APP_VERSION = os.environ.get("APP_VERSION", "dev")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "staging")
MAX_REQUEST_BODY_BYTES = 1024 * 1024
MAX_REQUEST_TARGET_BYTES = 8192
PUBLIC_ASSETS = {
    "/": "/index.html",
    "/index.html": "/index.html",
    "/app.js": "/app.js",
    "/data.js": "/data.js",
    "/styles.css": "/styles.css",
}
PROOF_ENVIRONMENTS = frozenset({"development", "local", "test"})
STRIPE_TEST_ENVIRONMENTS = frozenset({"pilot", "development", "local", "test"})
PILOT_EVIDENCE_BUCKET = "businessbuilder-staging-artifacts-199949321335-us-east-1"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _proof_endpoints_enabled() -> bool:
    enabled = os.environ.get("ENABLE_PROOF_ENDPOINTS", "").strip().lower()
    environment = os.environ.get("ENVIRONMENT", ENVIRONMENT).strip().lower()
    return enabled in {"1", "true", "yes"} and environment in PROOF_ENVIRONMENTS


def _supervised_stripe_test_admission_enabled(*, configured: bool) -> bool:
    enabled = os.environ.get("PILOT_SUPERVISED_STRIPE_TEST", "").strip().lower() in {"1", "true", "yes"}
    if not enabled:
        return False
    environment = os.environ.get("ENVIRONMENT", ENVIRONMENT).strip().lower()
    if environment not in STRIPE_TEST_ENVIRONMENTS or not configured:
        raise RuntimeError("isolated Stripe test checkout requires pilot configuration")
    return True


def _pilot_evidence_store() -> S3ArtifactStore | None:
    evidence_bucket = os.environ.get("PILOT_EVIDENCE_BUCKET", "")
    if evidence_bucket and (ENVIRONMENT != "pilot" or evidence_bucket != PILOT_EVIDENCE_BUCKET):
        raise RuntimeError("pilot evidence bucket configuration invalid")
    return (
        S3ArtifactStore(bucket=evidence_bucket, allowed_prefix="tenant/")
        if evidence_bucket else None
    )


def _database_health() -> None:
    connection = connect_postgres(
        application_name="businessbuilder-health", ensure_schema=False
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 AS healthy, "
                "to_regclass('bb_schema_migrations') IS NOT NULL AS migrated"
            )
            row = cursor.fetchone()
            if row["healthy"] != 1 or not row["migrated"]:
                raise RuntimeError("PostgreSQL health query failed")
    finally:
        connection.close()


def _initialize_database() -> None:
    connection = connect_postgres(application_name="businessbuilder-startup")
    try:
        migrate(connection)
    finally:
        connection.close()


class StagingHandler(SimpleHTTPRequestHandler):
    server_version = "BusinessBuilder"
    sys_version = ""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(PROTOTYPE), **kwargs)

    def version_string(self) -> str:
        return self.server_version

    def _json(
        self,
        status: HTTPStatus,
        body: dict[str, object],
        *,
        send_body: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        payload = json.dumps(body, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            self.wfile.write(payload)

    def _request_envelope_allowed(self) -> bool:
        if len(self.path.encode("utf-8", errors="replace")) > MAX_REQUEST_TARGET_BYTES:
            self._json(
                HTTPStatus.REQUEST_URI_TOO_LONG,
                {"status": "error", "error": "request target too long"},
                send_body=self.command != "HEAD",
            )
            return False
        if self.headers.get("Transfer-Encoding") is not None:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "error": "unsupported request framing"},
                send_body=self.command != "HEAD",
            )
            return False
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) > 1:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "error": "invalid content length"},
                send_body=self.command != "HEAD",
            )
            return False
        if lengths:
            try:
                length = int(lengths[0], 10)
            except ValueError:
                length = -1
            if length < 0:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"status": "error", "error": "invalid content length"},
                    send_body=self.command != "HEAD",
                )
                return False
            if length > MAX_REQUEST_BODY_BYTES:
                self._json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"status": "error", "error": "request body too large"},
                    send_body=self.command != "HEAD",
                )
                return False
        return True

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "object-src 'none'; form-action 'self'",
        )
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        super().end_headers()

    def _serve_get(self, *, send_body: bool) -> None:
        if not self._request_envelope_allowed():
            return
        path = urlsplit(self.path).path
        if path == "/healthz":
            try:
                _database_health()
                self._json(
                    HTTPStatus.OK,
                    {"status": "ok"},
                    send_body=send_body,
                )
            except Exception:
                self.log_error("database health failed")
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"status": "unhealthy"},
                    send_body=send_body,
                )
            return
        if path in {"/proof", "/api/v1/proofs/billy-bob-commercial"}:
            if not _proof_endpoints_enabled():
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"status": "not_found"},
                    send_body=send_body,
                )
                return
            try:
                self._json(
                    HTTPStatus.OK, run_cloud_proof(), send_body=send_body
                )
            except Exception:
                self.log_error("commercial proof failed")
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "status": "failed",
                        "proof": "billy-bob-commercial-postgres-v1",
                    },
                    send_body=send_body,
                )
            return
        if path == "/api/v1/proofs/billy-bob-commercial/state":
            if not _proof_endpoints_enabled():
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"status": "not_found"},
                    send_body=send_body,
                )
                return
            proof = get_cloud_proof()
            if proof is None:
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"status": "not_run", "backend": "postgresql"},
                    send_body=send_body,
                )
            else:
                self._json(HTTPStatus.OK, proof, send_body=send_body)
            return
        if path == "/api/v1":
            self._json(
                HTTPStatus.OK,
                {
                    "service": "businessbuilder",
                    "api_version": "v1",
                },
                send_body=send_body,
            )
            return
        if path.startswith("/api/v1/"):
            self._serve_customer_api("GET", send_body=send_body)
            return
        asset_path = PUBLIC_ASSETS.get(path)
        if asset_path is None:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"status": "not_found"},
                send_body=send_body,
            )
            return
        self.path = asset_path
        if send_body:
            super().do_GET()
        else:
            super().do_HEAD()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._serve_get(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self._serve_get(send_body=False)

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._request_envelope_allowed():
            return
        path = urlsplit(self.path).path
        application = getattr(self.server, "customer_api", None)
        api_methods = application.allowed_methods(path) if application and path.startswith("/api/v1/") else ()
        if path.startswith("/api/v1/") and application and not api_methods:
            self._json(HTTPStatus.NOT_FOUND, {"status": "not_found"}, send_body=False)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        allow = self._allow_header(api_methods) if api_methods else "GET, HEAD, OPTIONS"
        self.send_header("Allow", allow)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _method_not_allowed(self) -> None:
        if not self._request_envelope_allowed():
            return
        path = urlsplit(self.path).path
        application = getattr(self.server, "customer_api", None)
        api_methods = application.allowed_methods(path) if application and path.startswith("/api/v1/") else ()
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        allow = self._allow_header(api_methods) if api_methods else "GET, HEAD, OPTIONS"
        self.send_header("Allow", allow)
        self.send_header("Content-Type", "application/json")
        payload = b'{"error":"method not allowed","status":"error"}'
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._request_envelope_allowed():
            return
        path = urlsplit(self.path).path
        if not path.startswith("/api/v1/"):
            self._method_not_allowed()
            return
        self._serve_customer_api("POST", send_body=True)

    def _serve_customer_api(self, method: str, *, send_body: bool) -> None:
        application = getattr(self.server, "customer_api", None)
        request_id = self._trusted_request_id(self.headers.get("X-Request-ID"))
        correlation_id = self._trusted_request_id(
            self.headers.get("X-Correlation-ID"), fallback=request_id
        )
        response_headers = {
            "X-Request-ID": request_id,
            "X-Correlation-ID": correlation_id,
        }
        if application is None:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"status": "error", "error": "service_unavailable"},
                send_body=send_body,
                headers=response_headers,
            )
            return
        try:
            body = None
            if method == "POST":
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    self._json(
                        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                        {"status": "error", "error": "application_json_required"},
                        headers=response_headers,
                    )
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                if urlsplit(self.path).path == "/api/v1/payment-webhooks/stripe":
                    body = raw  # Signature validation requires the unmodified bytes.
                else:
                    try:
                        body = json.loads(raw) if raw else {}
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"status": "error", "error": "invalid_json"},
                            headers=response_headers,
                        )
                        return
            parsed = urlsplit(self.path)
            response = application.handle(
                method=method,
                path=parsed.path,
                headers={key: value for key, value in self.headers.items()},
                query=parse_qs(parsed.query, keep_blank_values=True),
                body=body,
                request_id=request_id,
                correlation_id=correlation_id,
            )
            if response.status is HTTPStatus.METHOD_NOT_ALLOWED:
                response_headers["Allow"] = self._allow_header(response.allow)
            self._json(
                response.status,
                response.body,
                send_body=send_body,
                headers=response_headers,
            )
        except Exception:
            self.log_error("customer API request failed")
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"status": "error", "error": "request_failed"},
                send_body=send_body,
                headers=response_headers,
            )

    @staticmethod
    def _trusted_request_id(value: str | None, *, fallback: str | None = None) -> str:
        if value and REQUEST_ID_PATTERN.fullmatch(value):
            return value
        return fallback or f"req_{uuid4().hex}"

    @staticmethod
    def _allow_header(methods: tuple[str, ...]) -> str:
        expanded = list(methods)
        if "GET" in expanded:
            expanded.insert(expanded.index("GET") + 1, "HEAD")
        expanded.append("OPTIONS")
        return ", ".join(expanded)

    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_TRACE = _method_not_allowed
    do_CONNECT = _method_not_allowed

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del message, explain
        try:
            status = HTTPStatus(code)
        except ValueError:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._json(
            status,
            {"status": "error", "error": "request failed"},
            send_body=self.command != "HEAD",
        )

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        path = urlsplit(self.path).path
        sys.stderr.write(
            f"businessbuilder_http method={self.command} path={path} "
            f"status={code} size={size}\n"
        )

    def log_error(self, format: str, *args: object) -> None:
        del format, args
        path = urlsplit(self.path).path
        sys.stderr.write(
            f"businessbuilder_http_error method={self.command} path={path}\n"
        )


if __name__ == "__main__":
    _initialize_database()
    signing_key = os.environ.get("CUSTOMER_API_PRINCIPAL_KEY", "").encode()
    if len(signing_key) < 32:
        raise RuntimeError("CUSTOMER_API_PRINCIPAL_KEY must contain at least 32 bytes")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), StagingHandler)
    stripe_key = os.environ.get("STRIPE_TEST_SECRET_KEY", "")
    stripe_webhook = os.environ.get("STRIPE_TEST_WEBHOOK_SECRET", "")
    payment_provider = (
        StripeTestPaymentProvider(api_key=stripe_key, webhook_secret=stripe_webhook)
        if stripe_key and stripe_webhook else None
    )
    test_admission = _supervised_stripe_test_admission_enabled(configured=payment_provider is not None)
    evidence_store = _pilot_evidence_store()
    server.customer_api = create_postgres_customer_api(
        signing_key=signing_key,
        founder_authentication_provider=cognito_authentication_from_environment(),
        payment_provider=payment_provider,
        allow_supervised_stripe_test_admission=test_admission,
        checkout_success_url=os.environ.get("BUSINESS_BUILDER_CHECKOUT_SUCCESS_URL"),
        checkout_cancel_url=os.environ.get("BUSINESS_BUILDER_CHECKOUT_CANCEL_URL"),
        residential_cleaning_evidence_store=evidence_store,
        # No production malware scanner is configured; uploads stay pending/quarantined.
    )
    print(f"Business Builder staging listening on :{port}", flush=True)
    server.serve_forever()
