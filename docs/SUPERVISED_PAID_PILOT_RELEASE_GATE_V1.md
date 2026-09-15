# Supervised paid-pilot release gate v1

This is a **go/no-go boundary**, not a production launch approval. Stripe TEST
Checkout remains the only configured provider. No live Stripe adapter, live
credentials, real charges, legal conclusion, or Texas tax classification are
enabled by this branch. Ready/Fully Set remain Verification decisions only.

## Release records and authority

The existing append-versioned Commercial ledger stores `release_gate` history
and one immutable `release_packet` for an exact tenant/company/order. Each
gate record has status (`PENDING`, `APPROVED`, `HOLD`), owner, opaque evidence
reference, approving actor and time, review/expiry date, required blocking
classification, reason, timestamp, and version. Superseding a decision appends
a new version; it does not rewrite history. All nine required gates are
blocking. PostgreSQL reads approvals live rather than relying on the API's
startup snapshot. Missing, held, expired, or stale approvals fail closed.

No customer API may record an approval. A server-only `PaidPilotReleaseGate`
requires an independently configured authority verifier for the exact actor,
gate, tenant, company, and order. A separate server-side packet evidence
verifier must attest that the founder actually accepted the exact terms and
that the packet references are real; neither verifier is installed by default.
All mutations therefore deny by default. The operator release for sandbox eligibility does **not**
substitute for attorney, CPA, security, or production acceptance.

| Blocking approval | Required evidence/decision | Accountable owner today |
| --- | --- | --- |
| FTC/business-opportunity legal | Written applicability/disclosure/waiting-period decision and reviewed customer path | Darius to appoint qualified counsel; unassigned reviewer |
| Texas tax/package classification | Written treatment per package and third-party cost handling | Darius to appoint Texas CPA/tax counsel; unassigned reviewer |
| Production Stripe | Live account/endpoint configuration, signed webhook drill, exact reconciliation, idempotency/replay review | Darius to appoint payment operator; unassigned |
| Production Cognito | Production pool/client, verified-email/domain path, operator MFA, support provisioning, disable/revoke reconciliation and abuse monitoring | Darius to appoint identity operator; unassigned |
| Operator provisioning | Distinct founder/operator accounts, MFA, expiring company-scoped grants, no founder self-review | Darius to appoint platform operator; unassigned |
| Evidence/privacy/retention | Real malware scanner, protected evidence access, approved retention/privacy/deletion policy and operator review | Darius to appoint privacy/security owner; unassigned |
| Monitoring/alerts | Named on-call monitor, payment/auth/webhook alerts, response drill | Darius to appoint monitoring owner; unassigned |
| Rollback/migration | Compatible image/schema promotion and restoration drill with named rollback owner | Darius to appoint deployment owner; unassigned |
| Terms/refund/cancellation | Exact version accepted by founder and counsel-reviewed refund/cancellation language | Darius to appoint counsel/product owner; unassigned |

Until those owners and evidence are explicitly recorded, the current release
status is `NOT_READY`. A reviewer can append `HOLD` at any time. After all
blocking approvals are current, status is `READY_FOR_SUPERVISED_PILOT`—still
**not** permission to charge. Only a separately authenticated server-side
ceremony can create the exact first-customer packet and move the order to
`APPROVED_FOR_LIVE_CHARGE`. The packet expires. Any changed order/quote,
payment admission, expired gate, or hold revokes effective live eligibility.
Test-mode commercial admission can never be promoted into this packet.

## First-customer packet and ceremony

The immutable packet contains tenant/company/order and a digest of exact
offer, USD first-charge amount, currency, quote and admission; tax state;
legal evidence reference/state; customer terms version and verified founder
acceptance reference; refund and cancellation
references; assigned operator, monitoring and rollback owners; support
contact; payment eligibility; release decision; actor/time; and expiry.
No raw legal documents, secrets, customer email, or card data are copied into
the packet.

Operator checklist, in order:

1. Confirm this is the intended **one** founder, company, offer, quote if
   applicable, first-charge amount, currency, admission, and tax state.
2. Confirm each of the nine current gate records has a named owner, written
   evidence reference, authorized approver, approval timestamp, and future
   review date. Ask counsel/CPA for their actual decisions; do not infer them.
3. Confirm verified founder terms acceptance, refund/cancellation wording,
   support contact, and any legally required payment delay are reflected in
   Commercial eligibility. `PAYMENT_DELAY_REQUIRED` remains a hard block.
4. Confirm live Stripe and Cognito configurations by **name/reference** in the
   approved secret/deployment store, never by printing values. Drill webhook
   signature, wrong amount/currency/customer, duplicate/replay and failure
   alerts in an isolated rehearsal.
5. Confirm monitoring owner is watching, operator MFA and scoped appointment
   are current, evidence scanner/privacy controls are live, and rollback owner
   has tested the compatible image/database restoration plan.
6. Engage and test emergency Checkout disable/rollback before the first
   customer window; record the drill reference. Set a short packet expiry and
   explicit operator go/no-go decision. A browser or SUPPORT click cannot
   create this packet.
7. Immediately before opening a **future** live Checkout, reread all approvals,
   packet, Commercial order/admission, and payment eligibility. On any mismatch:
   **HOLD**, do not open Checkout, investigate, and append a superseding review.

Stripe's signed, reconciled payment webhook remains the only entitlement
activation authority even after a live release. A redirect cannot activate it.

## Production configuration inventory (names/references only)

These are proposed *names* for the later production configuration review, not
deployed or populated values:

- Stripe: `STRIPE_LIVE_SECRET_REF`, `STRIPE_LIVE_WEBHOOK_SECRET_REF`,
  `STRIPE_LIVE_ACCOUNT_ID`, `STRIPE_LIVE_WEBHOOK_ENDPOINT`,
  `STRIPE_LIVE_MONITORING_OWNER`, `STRIPE_LIVE_ROLLBACK_REF`.
- Cognito/BFF: `COGNITO_PRODUCTION_USER_POOL_ID`,
  `COGNITO_PRODUCTION_APP_CLIENT_ID`, `COGNITO_PRODUCTION_DOMAIN`,
  `COGNITO_PRODUCTION_CALLBACK_URL`, `COGNITO_PRODUCTION_LOGOUT_URL`,
  `COGNITO_OPERATOR_MFA_POLICY_REF`, `COGNITO_EMAIL_DOMAIN_READINESS_REF`,
  `COGNITO_DISABLE_RECONCILIATION_REF`, `COGNITO_ABUSE_MONITORING_OWNER`,
  `BUSINESS_BUILDER_AUTH_COOKIE_SIGNING_KEY`, `BUSINESS_BUILDER_API_URL`.

The Stripe gate needs a real live webhook endpoint with signature verification,
named alerts, rollback, and the existing order/customer/amount/currency and
replay rules intact. The Cognito gate needs exact HTTPS callback/logout URLs,
verified email, production sender/domain, mandatory operator MFA policy,
separate support provisioning, account disable/revoke reconciliation, and
rate/abuse monitoring. A variable name merely existing is not proof that the
configuration works. Secret values must stay in the approved secret store.

## Promotion and rollback order

Source ancestry for this focused branch is the proven unified backend
`7d78d56c704b2ee43b33135164a135068ee3179b` and frontend
`62c1030ae481540bfff412519e642a5470ab58df`. A future release owner must
record the exact final SHAs and promote deliberately; this branch does not
merge or deploy.

1. Freeze known compatible backend/frontend image SHAs and take an encrypted,
   restore-tested database backup. Verify migration DDL on a disposable clone.
2. Promote compatible **backend schema/code first**, with test-only Checkout
   still configured and live credentials absent. Existing `bb_commercial_records`
   storage accepts the new `release_gate`/`release_packet` kinds without DDL;
   existing idempotency and billing-event tables are unchanged. Re-run startup
   migrations twice and confirm old states still read.
3. Promote frontend BFF and founder status view next. It is read-only and must
   display unknown/unavailable as blocked, not green. Verify auth, tenant scope,
   webhook and test Checkout against the compatible backend.
4. Only after legal/tax/config/operations signoffs and a manual ceremony may a
   separate, reviewed live-provider promotion be considered. This branch
   includes **no** live-provider implementation or automatic enable switch.

**Do not roll back to pilot ECS revision 7**: it cannot parse newer persisted
`ProductCode` values. The last known compatible pilot revision 12 was used in
the unified proof but must still be tested against a cloned *post-promotion*
schema before becoming a rollback target. For failure, immediately block new
Checkout, stop ingress/queue dispatch where safe, preserve signed webhook
receipts, and roll back to a verified compatible image. If no image can read
new commercial codes, restore the encrypted pre-promotion snapshot to an
isolated database and reconcile events since backup before any customer
traffic resumes; never point revision 7 at newer state. Record RPO/RTO and
owners in the rollback gate evidence. Customer-owned Company Brain and
Verification records are not erased to reverse billing.

## Open decisions before one paid customer

Counsel must decide FTC/business-opportunity applicability, disclosures,
timing, contract/refund/cancellation wording, and whether any payment delay is
required. A Texas CPA/tax reviewer must classify each package, recurring
managed services, bundled first charge, and third-party pass-throughs. Darius
must appoint actual production auth/payment, privacy, monitoring and rollback
owners. A real malware-scanning adapter, production email/domain, provider
disable reconciliation and compatible rollback drill remain open. None is
marked approved by the sandbox acceptance proof.
