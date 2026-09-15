# Supervised commercial admission — test-mode boundary

This branch does not enable live charges, production sending, or production deployment. Commercial remains the only order, subscription, and entitlement authority. Checkout redirects and browser clicks cannot activate entitlements; a verified raw-body Stripe **test-mode** event must reconcile to the exact persisted Checkout and order.

## Authority and sequence

For a new-business fixed offer: founder-approved Runtime scope → canonical Commercial draft → appointed operator admission → Stripe test Checkout → signed provider event → Commercial payment/subscription/entitlement transition → outbox → Runtime projection.

For the residential-cleaning **existing-business** offer: founder supplies a system inventory (an assertion, not verified truth) → appointed operator publishes bounded findings with source IDs from the persisted research packet → founder creates an **unpriced** order → appointed operator issues an exact audit/digest-bound quote above the marketing floor → founder approves the exact quote and current scope digest → appointed operator records a short-lived, immutable payment admission → eligible Checkout. A new-business package identifier is never reused for this path.

The operator's verified provider-backed session, active SUPPORT membership, tenant/organization/company state, separate expiring commercial appointment, action set, and principal signature are revalidated server-side. SUPPORT impersonation is deliberately excluded from the commercial operator BFF request. Neither SUPPORT membership nor provider group membership grants this authority by itself. A privileged provisioner verifier must be injected server-side; it defaults to absent and therefore fails closed. Denials are durably recorded after failed transactions roll back, without request payloads.

Admission binds the founder, tenant, company, offer, exact items/amount, existing-audit locator, recommendation digest, founder-approved quote version, and quote expiry. A quote proposal reserves the order quote slot transactionally; two processes cannot publish different current quotes or release two admissions for the same order version. Checkout rechecks admission expiry, order digest, operator appointment, eligibility time, quote status/expiry, and tax gate. `PAYMENT_DELAY_REQUIRED` remains blocked even if a founder approved the quote.

The legacy `record_payment_readiness` helper now works **only** when explicit deterministic test admission is enabled. Normal composition defaults it OFF. Test-mode tax uncertainty is named `TEST_MODE_UNDETERMINED`; it is allowed only by explicit test configuration and must never be represented as a Texas exemption. Normal pay-now release requires an injected tax-authority verifier; with no verifier it fails closed. This is a technical review port, not a tax determination.

## API contract (customer-safe fields only)

| Route | Actor | Meaning |
|---|---|---|
| `POST /api/v1/operator/companies/{company_id}/residential-cleaning/existing-scope` | appointed operator | Body: grant locator, bounded inventory-backed findings, known citation IDs. No tenant or role input. |
| `POST /api/v1/companies/{company_id}/residential-cleaning-pilot/existing-order` | founder OWNER | Empty body; deterministic unpriced order ID, retry-safe. |
| `POST /api/v1/operator/companies/{company_id}/orders/{order_id}/quote` | appointed operator | Body: grant locator, exact upfront minor units, short quote expiry. Audit/digest come from persisted order. |
| `GET /api/v1/orders/{order_id}/quote?company_id=...` | founder OWNER | Exact amount, recurring amount, expiry, digest, status. No internal audit/provider payload. |
| `POST /api/v1/orders/{order_id}/quote/approve?company_id=...` | founder OWNER | Body: quote ID + displayed digest; current Company Brain scope must still match. Browsing never approves. |
| `POST /api/v1/operator/companies/{company_id}/orders/{order_id}/release` | appointed operator | Body: grant locator, eligibility/time, review states and opaque references, expiry. Cannot self-issue. |
| `POST /api/v1/orders/{order_id}/checkout?company_id=...` | founder OWNER | Stable idempotency key; admission and quote revalidated before provider call. |
| `POST /api/v1/payment-webhooks/stripe` | Stripe test-mode event | Raw-body signature, persisted provider-reference and amount/currency reconciliation. Browser redirect has no authority. |

IDs in paths and request bodies are *locators*, not authority. Tenant, company, actor, role, membership and operator appointment are derived/rechecked on the server. Frontend never receives a secret ARN, webhook secret, provider payload or raw audit record.

## Stripe and failure safety

The Stripe adapter accepts `sk_test_` only and rejects live-mode events. Checkout prefills the authenticated founder email; only its SHA-256 digest is kept in the commercial record. A paid Checkout event must match that digest, the persisted order ID in signed session metadata, the session reference, and the canonical amount/currency before entitlement activation. Webhook signing uses the exact raw request bytes. Replays and duplicates use stable normalized event keys; out-of-order events remain retryable rather than being mistaken for success. `invoice.paid` is intentionally ignored for managed entitlement renewal because Stripe can emit it for out-of-band payments; a matched `invoice.payment_succeeded` for the canonical monthly amount and cycle is required. See [Stripe event types](https://docs.stripe.com/api/events/types), [webhook signatures](https://docs.stripe.com/webhooks/signature), and [invoice events](https://docs.stripe.com/invoicing/integration).

Payment failure, expired Checkout, refund, subscription failure/cancellation, and owned-state survival are covered by Commercial regression tests. No third-party government, domain, insurance, advertising, processor, attorney or CPA expense is included in Business Builder's canonical price. No revenue share or equity mechanism exists.

## Decisions before live payment

Legal counsel must determine whether any Business Builder offer falls within the FTC Business Opportunity Rule, which disclosures/earnings-claim materials are required, and whether any waiting period applies. The code expresses both immediate and delayed eligibility; it does **not** decide applicability. [FTC guidance](https://www.ftc.gov/business-guidance/resources/selling-work-home-or-other-business-opportunity-revised-rule-may-apply-you-1).

A Texas-qualified CPA/tax adviser must classify each separately identifiable component: site creation/hosting, strategy and consulting, the assembled business bundle, recurring managed operations/software, existing-business onboarding, and any tangible/digital deliverables. They must determine sourcing, taxable base, mixed-bundle invoicing, local rate, exemptions, out-of-state use, registrations, refunds, and tax treatment of renewal invoices. Stripe Tax setup/product codes cannot be selected from a marketing label alone. The [Texas Comptroller data-processing guidance](https://comptroller.texas.gov/taxes/publications/94-127.php), [taxable-services guidance](https://comptroller.texas.gov/taxes/publications/96-259.php), and [Stripe Checkout Tax documentation](https://docs.stripe.com/payments/checkout/taxes) are research inputs, not a ruling for this product.

Separate live blockers: verified terms/refund/cancellation disclosures, owner/operator appointment procedure, production tax-authority adapter, real Stripe test-account acceptance, live webhook endpoint configuration, supervised monitoring, and explicit live-charge approval. None is silently cleared by this branch.

## Test-only environment names

`BUSINESSBUILDER_TEST_POSTGRES_DSN`, `BUSINESSBUILDER_TEST_POSTGRES_SCHEMA`, `BUSINESS_BUILDER_API_URL`, `BUSINESS_BUILDER_STRIPE_TEST_SECRET_REF`, `BUSINESS_BUILDER_STRIPE_TEST_WEBHOOK_SECRET_REF`, `BUSINESS_BUILDER_CHECKOUT_SUCCESS_URL`, `BUSINESS_BUILDER_CHECKOUT_CANCEL_URL`, `BUSINESS_BUILDER_TEST_AUTH_MODE` (local harness only). Store values only in the approved secret/deployment store. Do not paste credentials into chat or command output. A pasted test key must be revoked before any acceptance proof.
