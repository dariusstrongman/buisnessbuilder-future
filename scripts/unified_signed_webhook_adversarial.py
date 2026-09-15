"""Bounded Stripe TEST webhook replay/reconciliation proof against the pilot BFF.

Loads both sandbox secrets into process memory only. Never prints provider
payloads, signers, customer email, tokens, or payment references.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from hmac import new as hmac_new
from http.client import HTTPSConnection
import json
import os
from time import time
from urllib.parse import urlencode

import boto3

from stripe_sandbox_pg_acceptance import _test_key


ACCOUNT = "acct_1UFpupPqDkzMFXg6"
PILOT_HOST = "d3qncwxo58gn5b.cloudfront.net"
ORDERS = {
    "business": "order_cleaning_build_f0b58241ef091e037f00",
    "business_run": "order_cleaning_build_f2b040b336361468f78b",
    "existing_run": "order_cleaning_existing_fd9005cbb91cbffe34d1",
}


def _get_json(host: str, path: str, token: str) -> dict:
    connection = HTTPSConnection(host, timeout=15)
    try:
        connection.request("GET", path, headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("Stripe sandbox event lookup failed")
        return json.loads(response.read(1_000_001))
    finally:
        connection.close()


def _send(event: dict, signer: str) -> tuple[int, dict]:
    raw = json.dumps(event, separators=(",", ":"), sort_keys=True).encode()
    timestamp = str(int(time()))
    signature = hmac_new(signer.encode(), timestamp.encode() + b"." + raw, sha256).hexdigest()
    connection = HTTPSConnection(PILOT_HOST, timeout=15)
    try:
        connection.request(
            "POST", "/api/payment-webhooks/stripe", body=raw,
            headers={"Content-Type": "application/json", "Stripe-Signature": f"t={timestamp},v1={signature}"},
        )
        response = connection.getresponse()
        body = json.loads(response.read(1_000_001))
        return response.status, body
    finally:
        connection.close()


def run() -> dict:
    if os.environ.get("ENVIRONMENT") != "pilot":
        raise RuntimeError("isolated pilot environment required")
    key = _test_key("stripetest")
    account = _get_json("api.stripe.com", "/v1/account", key)
    if account.get("id") != ACCOUNT:
        raise RuntimeError("wrong Stripe test account")
    secret = boto3.client("secretsmanager", region_name="us-east-1").get_secret_value(
        SecretId="businessbuilder-pilot-stripe-webhook-v1"
    )
    signer = json.loads(secret["SecretString"])["STRIPE_TEST_WEBHOOK_SECRET"]
    if not isinstance(signer, str) or not signer.startswith("whsec_"):
        raise RuntimeError("sandbox webhook signer unavailable")
    query = urlencode({"limit": "100", "type": "checkout.session.completed"})
    events = _get_json("api.stripe.com", "/v1/events?" + query, key).get("data", [])
    selected = {}
    for event in events:
        if event.get("livemode") is not False or event.get("type") != "checkout.session.completed":
            continue
        owner = event.get("data", {}).get("object", {}).get("client_reference_id")
        if owner in ORDERS.values():
            selected[owner] = event
    if len(selected) != len(ORDERS):
        raise RuntimeError("the three actual Stripe sandbox Checkout completion events are not available")
    result = {"source": "actual_stripe_test_events", "orders": len(selected)}
    for name, order_id in ORDERS.items():
        status, body = _send(selected[order_id], signer)
        if status != 200 or body.get("applied") != 0:
            raise RuntimeError(f"{name} signed duplicate webhook mutated Commercial")
    result["signed_duplicate_replay"] = "idempotent_no_new_effect"
    base = selected[ORDERS["business"]]
    for case in ("wrong_amount", "wrong_currency", "wrong_order", "wrong_customer", "live_mode"):
        event = deepcopy(base)
        event["id"] = "evt_unified_adversarial_" + case + "_" + sha256(str(time()).encode()).hexdigest()[:8]
        value = event["data"]["object"]
        if case == "wrong_amount":
            value["amount_subtotal"] -= 1
        elif case == "wrong_currency":
            value["currency"] = "eur"
        elif case == "wrong_order":
            value["client_reference_id"] = ORDERS["business_run"]
        elif case == "wrong_customer":
            value["customer_details"]["email"] = "forged-founder@example.invalid"
        elif case == "live_mode":
            event["livemode"] = True
        status, body = _send(event, signer)
        if status < 400 or body.get("applied", 0) != 0:
            raise RuntimeError(f"{case} signed adversarial event was accepted")
        result[case] = "rejected"
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(run(), sort_keys=True), flush=True)
    except Exception as error:
        print(json.dumps({"status": "failed", "failure_class": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
