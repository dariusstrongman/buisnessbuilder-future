"""Stripe Checkout boundary restricted to non-production test-mode credentials.

No payment API call is made unless explicitly configured. The browser never
supplies a price, tax classification, customer mapping, or payment outcome.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from typing import Callable, Mapping
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from .models import BillingMode, Order, TaxDisposition


Transport = Callable[[str, str, bytes | None, dict[str, str]], dict]


def _default_transport(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> dict:
    with urlopen(Request(url, data=body, headers=headers, method=method), timeout=12) as response:
        if response.status >= 300:
            raise RuntimeError("payment provider request failed")
        return json.loads(response.read(64 * 1024))


class StripeTestPaymentProvider:
    """Live Stripe keys and URLs are rejected until a separate launch approval."""

    def __init__(self, *, api_key: str, webhook_secret: str,
                 transport: Transport = _default_transport,
                 tax_codes: Mapping[str, str] | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        if not api_key.startswith("sk_test_") or len(api_key) < 16:
            raise ValueError("Stripe test-mode credential required")
        if not webhook_secret.startswith("whsec_") or len(webhook_secret) < 16:
            raise ValueError("Stripe webhook signing secret required")
        self._api_key = api_key
        self._webhook_secret = webhook_secret
        self._transport = transport
        self._clock = clock
        self._tax_codes = dict(tax_codes or {})

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    @staticmethod
    def _redirect(value: str) -> str:
        if not isinstance(value, str) or len(value) > 1000 or not re.fullmatch(r"https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9_./?=&{}%-]*)?", value):
            raise ValueError("exact HTTPS checkout redirect required")
        return value

    def open_checkout(self, *, order: Order, idempotency_key: str,
                      success_url: str, cancel_url: str,
                      customer_email: str) -> tuple[str, str]:
        if order.tax_disposition is TaxDisposition.MANUAL_REVIEW:
            raise ValueError("tax disposition still requires review")
        if order.tax_disposition is TaxDisposition.TAXABLE:
            raise ValueError("taxable classification requires configured tax calculation")
        if not order.items or any(item.unit_amount is None for item in order.items):
            raise ValueError("canonical item price required")
        if not isinstance(customer_email, str) or not re.fullmatch(r"[^\s@]{1,64}@[^\s@]{1,190}", customer_email):
            raise ValueError("verified founder email required")
        mode = "subscription" if any(item.billing_mode is BillingMode.RECURRING for item in order.items) else "payment"
        fields: list[tuple[str, str]] = [
            ("mode", mode), ("client_reference_id", order.order_id),
            ("customer_email", customer_email.strip().lower()),
            ("success_url", self._redirect(success_url)),
            ("cancel_url", self._redirect(cancel_url)),
            ("metadata[bb_order_id]", order.order_id),
            ("payment_method_types[0]", "card"),
        ]
        if mode == "subscription":
            fields.append(("subscription_data[metadata][bb_order_id]", order.order_id))
        if order.tax_disposition is TaxDisposition.PROVIDER_CALCULATED:
            if any(not re.fullmatch(r"txcd_[A-Za-z0-9]+", self._tax_codes.get(item.product_code.value, "")) for item in order.items):
                raise ValueError("approved per-product tax codes are required")
            fields.append(("automatic_tax[enabled]", "true"))
        for index, item in enumerate(order.items):
            prefix = f"line_items[{index}]"
            amount = item.unit_amount
            assert amount is not None
            fields.extend([
                (f"{prefix}[price_data][currency]", amount.currency.lower()),
                (f"{prefix}[price_data][unit_amount]", str(amount.minor_units)),
                (f"{prefix}[price_data][product_data][name]", item.package_name_snapshot),
                (f"{prefix}[quantity]", str(item.quantity)),
            ])
            if order.tax_disposition is TaxDisposition.PROVIDER_CALCULATED:
                fields.append((f"{prefix}[price_data][product_data][tax_code]", self._tax_codes[item.product_code.value]))
            if item.billing_mode is BillingMode.RECURRING:
                fields.append((f"{prefix}[price_data][recurring][interval]", "month"))
        result = self._transport(
            "POST", "https://api.stripe.com/v1/checkout/sessions",
            urlencode(fields).encode("ascii"), self._headers(idempotency_key),
        )
        provider_ref, url = result.get("id"), result.get("url")
        if not isinstance(provider_ref, str) or not provider_ref.startswith("cs_test_") or not isinstance(url, str) or not url.startswith("https://checkout.stripe.com/"):
            raise RuntimeError("Stripe test checkout response is invalid")
        return provider_ref, url

    def verify_webhook(self, signature: str, raw_body: bytes) -> dict:
        if not isinstance(raw_body, bytes) or len(raw_body) > 1024 * 1024:
            raise ValueError("invalid webhook body")
        parts: dict[str, list[str]] = {}
        for part in signature.split(",") if isinstance(signature, str) else ():
            key, separator, value = part.partition("=")
            if separator:
                parts.setdefault(key.strip(), []).append(value.strip())
        try:
            timestamp = int(parts["t"][0])
        except (KeyError, ValueError, IndexError) as exc:
            raise ValueError("invalid webhook signature") from exc
        if abs(int(self._clock().timestamp()) - timestamp) > 300:
            raise ValueError("webhook timestamp outside tolerance")
        signed = str(timestamp).encode("ascii") + b"." + raw_body
        expected = hmac.new(self._webhook_secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, candidate) for candidate in parts.get("v1", [])):
            raise ValueError("webhook authenticity failed")
        try:
            event = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("malformed webhook") from exc
        if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not isinstance(event.get("type"), str) or not isinstance(event.get("data"), dict):
            raise ValueError("malformed provider event")
        return event

    def subscription_period(self, provider_subscription_ref: str) -> tuple[int, int]:
        if not isinstance(provider_subscription_ref, str) or not provider_subscription_ref.startswith("sub_"):
            raise ValueError("invalid subscription reference")
        value = self._transport(
            "GET", "https://api.stripe.com/v1/subscriptions/" + quote(provider_subscription_ref, safe=""),
            None, self._headers(),
        )
        starts, ends = value.get("current_period_start"), value.get("current_period_end")
        if starts is None or ends is None:
            items = value.get("items", {}).get("data", []) if isinstance(value.get("items"), dict) else []
            if len(items) == 1 and isinstance(items[0], dict):
                starts, ends = items[0].get("current_period_start"), items[0].get("current_period_end")
        if value.get("id") != provider_subscription_ref or not isinstance(starts, int) or not isinstance(ends, int) or ends <= starts:
            raise RuntimeError("subscription period is unavailable")
        return starts, ends
