# Supervised Checkout + Payment Authority v1

This branch is **test-mode only**. It does not enable live Stripe, decide tax or Business Opportunity Rule applicability, or make a customer immediately eligible to pay. The Commercial repository remains the authority for orders, payments, subscriptions and entitlements. A browser redirect or customer API request cannot activate an entitlement.

## Offer and order contract

`commercial/pricing.py` is the canonical founding-price source, exposed read-only at `GET /api/v1/pricing`. Amounts are USD minor units:

| Offer | Upfront | Monthly | Chargeability |
| --- | ---: | ---: | --- |
| Build My Website | 79500 | — | Fixed |
| Build My Business | 149500 | — | Fixed |
| New Business + Run | 199500 | 29900 | Fixed; the first subscription invoice includes both amounts |
| Existing Business + Run | from 149500 | 29900 | Quote required; the floor is **not** a charge |

The separate existing-business product/version IDs prevent a new-business price from being applied to existing-business onboarding. Government fees, domains, insurance, paid SaaS, advertising, payment-processing fees, attorney/CPA fees and all other third-party charges are not included in these Business Builder fees. There is no revenue share, equity or percentage-of-sales billing.

An existing-business customer records an existing-systems inventory in Company Brain. This is explicitly a founder assertion pending operator scope, not a completed Existing Business Audit. The Commercial quote service requires an audit reference, recommendation digest, scoped amount, expiration and founder approval. **A trusted operator-authored audit/scoped recommendation and its authenticated quote-publishing path remain blocked for the first paid existing-business customer.** No customer API currently turns the `from` floor into a fixed charge. A quote must not be advertised as ready until that operator workflow exists.

`PAYMENT_DELAY_REQUIRED` is the default order eligibility; `PAY_NOW_ELIGIBLE` can only be recorded by a separate internal supervised decision with a review reference. The reason/length of any disclosure or waiting period is a legal review decision, not hard-coded here. Tax disposition is one of `taxable`, `non_taxable`, `provider_calculated`, or `manual_review`. Manual review blocks checkout. Provider-calculated tax requires an approved Stripe tax code for each Business Builder fee item.

## Customer and provider flow

1. Digest-bound founder approval commits the residential-cleaning recommendation scope. Commercial creates a draft, canonical fixed-price Build My Business order. The founder may select the canonical New Business + Run offer while the order is uncharged.
2. `GET /api/v1/orders/{order_id}/checkout?company_id=...` reads only the persisted state. `POST` takes a retry/idempotency key and is unavailable while eligibility is delayed, tax is under manual review, no payment provider is configured, or the order is unpriced.
3. The server sends its immutable order items and its own exact HTTPS success/cancel URLs to a Stripe **test-mode** Checkout Session. A recurring bundle uses Stripe subscription mode with a one-time setup price and a recurring $299 price. The BFF returns only an open test Checkout redirect URL.
4. `POST /api/v1/payment-webhooks/stripe` takes the raw body and Stripe signature. It verifies HMAC/timestamp, requires `livemode=false`, looks up the **persisted** Checkout Session reference, and checks order reference, USD currency and canonical subtotal. It does not trust client/Stripe metadata for tenant authority. A completed but unpaid session does not activate anything. A verified paid session creates payment, subscription and entitlements through Commercial's normalized-event transaction.
5. Duplicate/replayed events are idempotent; an unresolvable out-of-order event fails for retry. Checkout abandonment, failure, refund and subscription cancellation retain history. Full refund suspends managed entitlements; customer-owned state and Company Brain are retained. Period-end cancellation stops future recurring charges according to terms and preserves handoff/export rights; it does not transfer Business Builder platform IP.

The HTTP adapter refuses non-test Stripe secret keys. No network call occurs without explicit test-mode configuration. No test credentials are committed.

## Environment variable names only

`STRIPE_TEST_SECRET_KEY`, `STRIPE_TEST_WEBHOOK_SECRET`, `BUSINESS_BUILDER_CHECKOUT_SUCCESS_URL`, `BUSINESS_BUILDER_CHECKOUT_CANCEL_URL`, `BUSINESSBUILDER_DATABASE_URL`, and the existing Cognito/BFF session configuration names. Secret **values** belong only in the approved deployment secret store, never source, logs or chat. The success/cancel URLs must be exact HTTPS URLs registered/approved for the pilot. Stripe test Checkout must stay isolated from production account credentials.

## Decisions before live payment

- Counsel: whether this specific Business Builder offer is covered by the FTC Business Opportunity Rule or another disclosure/waiting regime; required disclosures, waiting period, refund/cancellation terms and customer-facing claims. Do not infer applicability from the product name.
- Texas CPA/tax counsel: taxable classification of each service/package component, treatment of website/software/consulting and recurring managed operations, taxable bundled transactions, nexus and out-of-state sales, tax codes, exemptions and invoice presentation. A test-mode provider calculation is not a tax opinion.
- Operator: authenticated commercial decision authority separate from SUPPORT, approved tax disposition, pay-now/release ceremony, dispute/refund handling and quote publication for existing businesses.
- Provider operations: Stripe test-account acceptance with real test webhooks (including subscriptions, refunds and failure cases), correct event subscriptions/API version, signing-secret rotation, reconciliation of uncertain webhook/Checkout outcomes, subscription first-invoice reference mapping and processing-fee disclosure.

No production deployment, live payment, or entitlement activation from frontend state is authorized by this branch.

Stripe source contracts: https://docs.stripe.com/payments/checkout/how-checkout-works ; https://docs.stripe.com/webhooks ; https://docs.stripe.com/payments/checkout/taxes . Legal/tax review background: https://www.ftc.gov/business-guidance/resources/selling-work-home-or-other-business-opportunity-revised-rule-may-apply-you-1 ; https://comptroller.texas.gov/taxes/tax-policy-news/2024-april.php .
