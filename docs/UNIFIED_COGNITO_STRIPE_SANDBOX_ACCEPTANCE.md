# Unified Cognito–Flagship–Stripe sandbox acceptance v1

Scope: isolated non-production Cognito pilot, existing flagship site, existing
private pilot PostgreSQL, and the **website builder Stripe test account only**.
This is not a live-payment release or legal/tax determination.

## Authority and acceptance path

The same provider-verified Cognito founder from the Cognito pilot acceptance
logged in through Managed Login. The BFF established an HttpOnly customer
session; the backend derived user, OWNER membership, organization, tenant, and
company scope. The founder used the flagship intake to persist three separate
residential-cleaning companies, cited research packets, digest-bound scope
approvals, and pending commercial orders. A distinct real Cognito operator had
an active SUPPORT membership, founder-approved company support grants, and
AWS-deployer-appointed, exact-company commercial release grants. Cognito groups
did not supply Business Builder authority.

| Test offer | Canonical first Checkout | Result |
| --- | ---: | --- |
| Build My Business | $1,495 | Stripe TEST paid; signed webhook reconciled exact order and activated build entitlements |
| Build My Business + Run | $1,995 + first $299 = $2,294 | Stripe TEST paid; recurring subscription active; managed entitlements active |
| Existing Business + Run | exact test quote $1,695 + first $299 = $1,994 | audit → cited operator scope → unpriced order → quote → founder digest approval → operator release → Stripe TEST paid |

Stripe's actual test Checkout line items confirmed the distinct upfront and
first $299 cycle for both subscription offers. The existing-business marketing
floor was never used as an exact charge. Frontend redirects, package selection,
and scope browsing did not activate entitlements. Ready and Fully Set remained
false under Verification despite payment.

Actual Stripe test events were re-signed with the exact isolated webhook signer
inside process memory to prove duplicate replay had zero new effect. Wrong
amount, currency, customer, order mapping, and live-mode event variants were
rejected. A separate real Stripe TEST session was expired; its signed event
canceled the order and granted no entitlements. A declined test card left its
Checkout pending and granted no entitlements (the customer may retry that
Checkout; a declined attempt is not automatically a canceled order).

An additional real-Cognito-founder unpaid order proved anonymous access,
forged company/role inputs, success-redirect-only activation, tax-review-required
PAY_NOW, PAYMENT_DELAY_REQUIRED Checkout, stale quote digest, and revoked cookie
replay failed closed. These checks used no synthetic founder or payment event.

The running pilot API initially could not see an operator appointment committed
by a separate bounded Fargate task because its PostgreSQL commercial snapshot
was loaded at startup. A scoped PostgreSQL grant reread now checks fresh
appointment and revocation versions; isolated PostgreSQL regression tests cover
this. No new orchestrator, database, worker service, IAM user, or access key was
created.

## Deployment and cost boundary

Only the existing `businessbuilder-pilot-auth` non-production ECS service was
updated with immutable image digests. The existing RDS database remained
private. The only new standing AWS resource is the isolated Stripe test webhook
signing secret in the approved Secrets Manager store (approximately $0.40/month
list price); bounded one-shot appointment tasks add only small usage-based
runtime. The existing pilot estimate remained under the $100/month ceiling.
No production service, DNS, Stripe live mode, real customer data, or real charge
was touched.

Environment-variable **names only**: `PILOT_SUPERVISED_STRIPE_TEST`,
`STRIPE_TEST_SECRET_KEY`, `STRIPE_TEST_WEBHOOK_SECRET`,
`BUSINESS_BUILDER_CHECKOUT_SUCCESS_URL`,
`BUSINESS_BUILDER_CHECKOUT_CANCEL_URL`, `COGNITO_PILOT_SITE`,
`BUSINESS_BUILDER_API_URL`, `CUSTOMER_API_PRINCIPAL_KEY`.

## Important limitations before a paid pilot

- The unified run used the same **previously signed-up and email-verified**
  Cognito founder from the completed Cognito pilot proof; it did not repeat a
  fresh email-verification ceremony in this run. There is no synthetic identity
  seam, but signup-to-payment was not one uninterrupted browser recording.
- Stripe test-mode amount/tax states do not establish Texas tax treatment.
  CPA review and provider tax configuration are required before live mode.
- Commercial eligibility supports both immediate and delayed admission; FTC
  applicability and any required disclosure/waiting ceremony still require
  counsel/operator decision, not code inference.
- The old pilot ECS revision 7 cannot parse newer persisted ProductCode values;
  it is not a safe rollback target. Current compatible revisions were used for
  updates. A production migration/rollback compatibility plan remains required.
- Two earlier disposable pilot companies were contaminated by the old
  deterministic test-payment admission flag and were **excluded** from this
  authority proof. They contain no real customer data and must not be cited as
  signed-webhook evidence.
- The wider residential-cleaning business remains unverified: founder legal,
  banking, insurance, provider authorizations, evidence review, and real
  customer-journey execution are separate gates. No live outbound provider,
  external filing, merchant activation, or domain purchase was exercised.

## Reproduce safely

Frontend scripts: `unified-founder-bootstrap.mjs`,
`unified-commercial-acceptance.mjs`, `unified-negative-acceptance.mjs`,
`unified-stripe-failure-acceptance.mjs`, `unified-render-review.mjs`.
Backend scripts: `unified_signed_webhook_adversarial.py`,
`run_pilot_appointment_task.py`, `deploy_unified_pilot_task.py`.
All scripts require explicit pilot locators or pilot-mode guards, load approved
secrets in process memory, and print only safe status/IDs. Do not use staging
RDS for disposable automated PostgreSQL tests.
