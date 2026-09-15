"""One-shot, local-only Stripe CLI signed-webhook acceptance listener.

The Docker CLI signs actual Stripe sandbox events. Secrets remain in AWS or
process memory; this does not create a persistent webhook endpoint or deploy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import urlparse
from uuid import uuid4

from businessbuilder.commercial import CommercialService, RecordingCommercialEventSink
from businessbuilder.commercial.operator_authority import CommercialOperatorAuthority
from businessbuilder.commercial.stripe_test import StripeTestPaymentProvider, _default_transport
from businessbuilder.commercial.stripe_webhooks import StripeWebhookIngress
from businessbuilder.identity import IdentityService
from businessbuilder.postgres import PostgresCommercialRepository, PostgresIdentityRepository

from stripe_sandbox_pg_acceptance import _test_key


def main() -> None:
    dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
    if urlparse(dsn).hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("only disposable loopback PostgreSQL is permitted")
    schema = os.environ["BUSINESS_BUILDER_STRIPE_SANDBOX_SCHEMA"]
    if not re.fullmatch(r"bb_stripe_sandbox_[0-9a-f]{12}", schema):
        raise RuntimeError("acceptance schema locator invalid")
    port = int(os.environ.get("BUSINESS_BUILDER_STRIPE_SANDBOX_WEBHOOK_PORT", "8765"))
    requested_events = os.environ.get(
        "BUSINESS_BUILDER_STRIPE_SANDBOX_EVENTS",
        "checkout.session.completed,checkout.session.expired,checkout.session.async_payment_succeeded",
    )
    allowed_events = {
        "checkout.session.completed", "checkout.session.expired",
        "checkout.session.async_payment_succeeded", "refund.created",
        "customer.subscription.deleted", "invoice.payment_succeeded", "invoice.payment_failed",
    }
    events = requested_events.split(",")
    if not events or any(item not in allowed_events for item in events):
        raise RuntimeError("sandbox event filter invalid")
    expected_type = os.environ.get("BUSINESS_BUILDER_STRIPE_SANDBOX_EXPECT_TYPE", "checkout.session.completed")
    expected_count = int(os.environ.get("BUSINESS_BUILDER_STRIPE_SANDBOX_EXPECT_COUNT", "3"))
    if expected_type not in events or not 1 <= expected_count <= 10:
        raise RuntimeError("sandbox listener completion policy invalid")
    key = _test_key(os.environ["BUSINESS_BUILDER_STRIPE_TEST_SECRET_REF"])
    expected_account = os.environ["BUSINESS_BUILDER_STRIPE_TEST_ACCOUNT_ID"]
    account = _default_transport(
        "GET", "https://api.stripe.com/v1/account", None,
        {"Authorization": f"Bearer {key}"},
    )
    if account.get("id") != expected_account:
        raise RuntimeError("Stripe credential points to the wrong sandbox account")
    identity_repo = PostgresIdentityRepository(dsn, schema=schema)
    commercial_repo = PostgresCommercialRepository(dsn, schema=schema)
    now = lambda: datetime.now(timezone.utc)
    identity = IdentityService(identity_repo, id_factory=lambda kind: f"{kind}_{uuid4().hex}", clock=now)
    authority = CommercialOperatorAuthority(
        identity_repo, commercial_repo,
        signing_key=b"isolated_sandbox_operator_test_signing_key_v1",
        clock=now,
    )
    commercial = CommercialService(
        commercial_repo, identity.authorization, RecordingCommercialEventSink(),
        id_factory=lambda kind: f"{kind}_{uuid4().hex}", clock=now,
        operator_authority=authority, allow_test_admission=True,
    )
    state: dict = {"ingress": None, "accepted": [], "denied": 0, "replay_zero": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def do_POST(self) -> None:
            if self.path != "/api/v1/payment-webhooks/stripe":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1024 * 1024 or state["ingress"] is None:
                self.send_error(413 if length > 1024 * 1024 else 503)
                return
            raw = self.rfile.read(length)
            signature = self.headers.get("Stripe-Signature", "")
            try:
                applied = state["ingress"].handle(signature, raw)
                event = json.loads(raw)
                if state["ingress"].handle(signature, raw) == 0:
                    state["replay_zero"] += 1
                else:
                    raise RuntimeError("duplicate signed Stripe event was not idempotent")
                state["accepted"].append({
                    "event_ref": event.get("id"), "type": event.get("type"),
                    "applied": applied,
                })
                self.send_response(200)
            except Exception:
                state["denied"] += 1
                self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"received":true}')

    server = HTTPServer(("0.0.0.0", port), Handler)
    server.timeout = 2
    env = dict(os.environ, STRIPE_API_KEY=key)
    command = [
        "docker", "run", "--rm", "--name", "bb-stripe-cli-acceptance-v1",
        "-e", "STRIPE_API_KEY", "stripe/stripe-cli:latest", "listen", "--skip-update",
        "--events", ",".join(events),
        "--forward-to", f"http://host.docker.internal:{port}/api/v1/payment-webhooks/stripe",
    ]
    cli = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, bufsize=1)
    issued_secret: list[str] = []

    def read_cli() -> None:
        assert cli.stdout is not None
        for line in cli.stdout:
            found = re.search(r"whsec_[A-Za-z0-9]+", line)
            if found and not issued_secret:
                issued_secret.append(found.group(0))
            # Never print CLI lines: they may include signing material.

    reader = threading.Thread(target=read_cli, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 20
        while not issued_secret and cli.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        if not issued_secret:
            raise RuntimeError("Stripe CLI did not issue a sandbox signing secret")
        provider = StripeTestPaymentProvider(api_key=key, webhook_secret=issued_secret[0])
        state["ingress"] = StripeWebhookIngress(commercial_repo, commercial, provider)
        print(json.dumps({"status": "signed_webhook_listener_ready", "port": port,
                          "schema": schema, "signing_secret_redacted": True}), flush=True)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline and cli.poll() is None:
            server.handle_request()
            if sum(item["type"] == expected_type for item in state["accepted"]) >= expected_count:
                break
        print(json.dumps({"status": "listener_finished", "accepted": state["accepted"],
                          "denied_count": state["denied"],
                          "duplicate_signed_replays_suppressed": state["replay_zero"]}), flush=True)
    finally:
        server.server_close()
        if cli.poll() is None:
            cli.terminate()
            try:
                cli.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cli.kill()
        # Docker run may outlive its host-side CLI process on Windows. Stop
        # only the uniquely named acceptance container created above.
        subprocess.run(
            ["docker", "stop", "bb-stripe-cli-acceptance-v1"],
            capture_output=True, timeout=8, check=False,
        )
        commercial_repo.close()
        identity_repo.close()
        key = ""
        issued_secret.clear()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "failed", "failure_class": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
