"""Create the isolated Stripe sandbox webhook and store its signer in AWS.

Never prints provider credentials, signing secrets, payloads, or request bodies.
The endpoint is pilot-only and receives a small explicit event allowlist.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import boto3

from stripe_sandbox_pg_acceptance import _test_key


EVENTS = (
    "checkout.session.completed", "checkout.session.expired",
    "payment_intent.payment_failed", "refund.created",
    "customer.subscription.created", "customer.subscription.updated",
    "customer.subscription.deleted", "invoice.payment_succeeded",
    "invoice.payment_failed",
)
SECRET_NAME = "businessbuilder-pilot-stripe-webhook-v1"
ENDPOINT_URL = "https://d3qncwxo58gn5b.cloudfront.net/api/payment-webhooks/stripe"


def _stripe(method: str, path: str, key: str, fields: list[tuple[str, str]] | None = None) -> dict:
    body = urlencode(fields).encode() if fields else None
    request = Request(
        "https://api.stripe.com/v1" + path, data=body, method=method,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=15) as response:
        if response.status >= 300:
            raise RuntimeError("Stripe pilot endpoint request failed")
        return json.loads(response.read(64 * 1024))


def provision() -> tuple[str, str]:
    if os.environ.get("ENVIRONMENT") != "pilot":
        raise RuntimeError("Stripe endpoint provisioning requires isolated pilot mode")
    key = _test_key(os.environ["BUSINESS_BUILDER_STRIPE_TEST_SECRET_REF"])
    expected_account = os.environ["BUSINESS_BUILDER_STRIPE_TEST_ACCOUNT_ID"]
    if _stripe("GET", "/account", key).get("id") != expected_account:
        raise RuntimeError("Stripe credential points to a different sandbox")
    secrets = boto3.client("secretsmanager", region_name="us-east-1")
    existing = _stripe("GET", "/webhook_endpoints?limit=100", key).get("data", [])
    if any(endpoint.get("url") == ENDPOINT_URL and endpoint.get("status") == "enabled"
           for endpoint in existing):
        raise RuntimeError("pilot webhook already enabled; do not replace its signer implicitly")
    endpoint = _stripe("POST", "/webhook_endpoints", key, [
        ("url", ENDPOINT_URL),
        ("description", "Business Builder isolated Cognito-Stripe pilot acceptance"),
        *[("enabled_events[]", event) for event in EVENTS],
    ])
    signer = endpoint.get("secret")
    if (endpoint.get("livemode") is not False or endpoint.get("url") != ENDPOINT_URL
        or not isinstance(signer, str) or not signer.startswith("whsec_")):
        raise RuntimeError("sandbox webhook endpoint response invalid")
    try:
        secret = secrets.create_secret(
            Name=SECRET_NAME,
            Description="Isolated pilot Stripe test webhook signing material",
            SecretString=json.dumps({"STRIPE_TEST_WEBHOOK_SECRET": signer}),
            Tags=[
                {"Key": "Application", "Value": "businessbuilder"},
                {"Key": "Environment", "Value": "pilot"},
                {"Key": "Purpose", "Value": "unified-stripe-sandbox-acceptance-v1"},
            ],
        )
    except Exception:
        _stripe("POST", "/webhook_endpoints/" + endpoint["id"], key, [("disabled", "true")])
        raise RuntimeError("webhook signer storage failed; endpoint disabled") from None
    return endpoint["id"], secret["ARN"]


if __name__ == "__main__":
    try:
        endpoint_id, signer_ref = provision()
        print(json.dumps({"status": "provisioned", "stripe_test_endpoint_id": endpoint_id,
                          "signer_secret_ref": signer_ref}), flush=True)
    except Exception as error:
        print(json.dumps({"status": "failed", "failure_class": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
