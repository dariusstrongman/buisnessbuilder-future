# Independent approval evidence + production configuration rehearsal v1

Date: 2026-09-15. Source: `codex/supervised-paid-pilot-release-gate-v1`
backend `9b7404f28b0add09e2643ff8136eb7c43c76c373`. This is a
**read-only cloud and disposable-local-PostgreSQL rehearsal**. It does not
authorize a real founder, create a production gate record, enable live
Stripe, merge, deploy, or charge. The machine-readable
`PAID_PILOT_REHEARSAL_GATE_MATRIX.json` is evidence inventory only; the
append-versioned Commercial ledger and independently verified gate/packet
authorities remain authoritative.

## GO / HOLD matrix

No person has yet been designated as an approving owner for these nine gates.
The accountable *roles* are recorded below, but a role label is not an
appointment, an on-call commitment, or a legal/tax approval. All nine are
blocking and remain `HOLD`/`NOT_READY`; the overall decision is **HOLD**.

| Gate | Owner role; appointed person | Evidence actually present | Still needed to turn green |
| --- | --- | --- | --- |
| FTC/business-opportunity legal | Qualified counsel; **unappointed** | None | Written counsel decision on applicability, disclosures, waiting period, terms and evidence reference. No inference from product code. |
| Texas tax/package treatment | Texas CPA/tax counsel; **unappointed** | None | Written review of each fixed/quoted package, $299 recurring service, first-cycle bundle, third-party costs and provider/manual tax disposition. |
| Production Stripe | Payment operations; **unappointed** | Real Stripe TEST Checkout, signed test webhook, idempotency and exact reconciliation proof; only test secret/webhook resource names seen | Reviewed live-account/endpoint references, signed live-mode *no-charge* rehearsal, live webhook alert owner, rollback and keys in approved secret store. No live adapter is implemented. |
| Production Cognito | Identity operations; **unappointed** | One isolated pilot pool and public code/PKCE app client; verified email, token revocation/rotation, existence suppression | Separate production pool/client/domain; approved operator MFA; production sender; account disable/revoke reconciliation; abuse monitoring. |
| Operator provisioning | Platform operator; **unappointed** | Pilot founder/operator separation, scoped SUPPORT grant/expiry and self-review denial | Named production operator identity, mandatory MFA attestation, privileged provisioning/revocation drill and company-scoped appointment runbook. |
| Evidence/privacy/retention | Privacy/security operations; **unappointed** | Staging S3 public access block (all four flags), AES256 default encryption, versioning; immutable/quarantined evidence model | Actual malware scanner, approved privacy/retention/deletion terms and real evidence-access/operator-review drill. `PendingMalwareScanner` is fail-closed, not protection. |
| Monitoring/alerts | Monitoring on-call; **unappointed** | Existing code audit and durable denial/event records | Named responder, Business Builder-specific auth/payment/webhook/scanner alarms, notification route and alert/rollback drill. Read-only CloudWatch inventory showed no Business Builder alarm; SNS topic list was empty. |
| Migration/rollback | Deployment/rollback owner; **unappointed** | Fresh disposable PostgreSQL migration/restart suite; current staging pilot ECS service running task revision 12 | Compatible *post-gate-record* backend image, schema clone/restore and webhook reconciliation drill, named owner and RPO/RTO. Pilot revision 7 is forbidden; revision 12 also cannot be assumed to decode the new gate records. |
| Customer terms/refund/cancellation | Product counsel; **unappointed** | Immutable first-customer packet model and synthetic acceptance-verifier tests | Counsel-reviewed exact terms/refund/cancellation versions, authenticated founder acceptance record, independent packet evidence verifier and actual support contact. No founder terms-acceptance route exists yet. |

## Production Stripe structure rehearsal — HOLD

The AWS name-only inventory found `stripetest` and the isolated pilot webhook
secret name, not a Stripe live-mode secret name. This does not prove that no
live account exists elsewhere; it proves only that the approved *current*
deployment inventory has not established a live configuration. The code's
only provider adapter, `StripeTestPaymentProvider`, rejects a live key. It
was not given a live key and made no live-mode network call.

The proposed **environment-variable names only** are
`STRIPE_LIVE_SECRET_REF`, `STRIPE_LIVE_WEBHOOK_SECRET_REF`,
`STRIPE_LIVE_ACCOUNT_ID`, `STRIPE_LIVE_WEBHOOK_ENDPOINT`,
`STRIPE_LIVE_MONITORING_OWNER`, and `STRIPE_LIVE_ROLLBACK_REF`.
All six are absent from the rehearsal process. The script checks only the
presence of names and never reads or outputs their values. Even if every
name exists, the release remains HOLD until a separately verified review
records webhook endpoint authenticity, exact order/customer/amount/currency,
replay/idempotency, alerts, and rollback evidence. Sandbox test events are
not a substitute for that review. No live charge or live Checkout was opened.

## Production Cognito structure rehearsal — HOLD

Read-only Cognito inventory found only `businessbuilder-pilot-auth-v1` and
its public pilot web app client, not a named production pool. The pilot has
auto-verified email, verified-email recovery, 12-character mixed password
policy, authorization code/PKCE, exact HTTPS callback/logout URLs,
15-minute access/ID tokens, one-day rotating refresh tokens, revocation,
and user-existence suppression. Its pool MFA setting is **OPTIONAL**, which
does not satisfy an approved production operator MFA requirement. The pilot
CloudFront URLs are not treated as production callbacks.

Proposed **names only**:
`COGNITO_PRODUCTION_USER_POOL_ID`,
`COGNITO_PRODUCTION_APP_CLIENT_ID`, `COGNITO_PRODUCTION_DOMAIN`,
`COGNITO_PRODUCTION_CALLBACK_URL`, `COGNITO_PRODUCTION_LOGOUT_URL`,
`COGNITO_OPERATOR_MFA_POLICY_REF`,
`COGNITO_EMAIL_DOMAIN_READINESS_REF`,
`COGNITO_DISABLE_RECONCILIATION_REF`,
`COGNITO_ABUSE_MONITORING_OWNER`,
`BUSINESS_BUILDER_AUTH_COOKIE_SIGNING_KEY`, and
`BUSINESS_BUILDER_API_URL`.
All eleven are absent from the rehearsal process. A later production review
must validate secret-store references, exact callback/logout URLs, operator
MFA enrollment/enforcement, support provisioning, account disable/revoke
reconciliation, and abuse/rate controls without printing credentials.

## Evidence, monitoring, and operator drills — HOLD

The staging artifact bucket is private, AES256-encrypted, and versioned.
The evidence subsystem enforces file type/size, SHA-256 identity, quarantine,
tenant scope, immutable submissions, scan state, and operator review. Its
default pending scanner **cannot** release uploaded evidence as clean. A
deterministic scanner exists only for tests. No approved real malware scanner
or retention/legal-hold decision has been attached to the paid-pilot gate.

The pilot Cognito operator acceptance established a distinct identity and
expiring company-scoped SUPPORT grant. It did **not** provision a production
operator or enforce a production MFA policy. No founder self-review or
SUPPORT billing/approval authority was added. Read-only CloudWatch/SNS
inventory found no Business Builder alert route or accountable responder;
durable internal audit is not paging.

## Promotion and rollback rehearsal — HOLD

The local PostgreSQL suite created fresh schemas, applied migrations,
persisted all nine HOLD records, restarted the repository, and preserved
tenant isolation. This is not a production backup/restore drill. The AWS
pilot service is still on `businessbuilder-pilot-auth-v1:12` (one desired and
running task); no task was changed.

The rollback inspection compared unified source
`7d78d56c704b2ee43b33135164a135068ee3179b` with the new release-gate
codec and reproduced a decoder failure when a gate record is read without
`GateRecord`. Therefore:

1. `businessbuilder-pilot-auth-v1:7` remains excluded because it cannot read
   newer commercial `ProductCode` values.
2. `businessbuilder-pilot-auth-v1:12` is a **pre-gate-record** task revision;
   after real gate records are written, it is excluded pending an actual
   compatibility proof. Do not designate it a safe rollback image merely
   because it ran the unified sandbox proof.
3. Before any promotion, build and test a new immutable backend image that
   reads existing Commercial state **and** gate records; keep a tested
   compatible image available as rollback target. Promote backend/schema
   before the read-only frontend, never the reverse. Clone/restore private
   PostgreSQL, run migrations twice, verify signed webhook replay, and name
   RPO/RTO and owner. No production deployment occurs in this rehearsal.

## Founder terms and packet rehearsal — HOLD

Tests demonstrate that even nine synthetically green gates cannot issue a
packet without the separate authenticated ceremony and packet-evidence
verifier; a missing/expired gate, payment delay, tax review, stale order,
expired packet, or test-mode admission blocks live eligibility. A synthetic
test packet is not real founder acceptance. The flagship currently has no
versioned founder terms/refund/cancellation acceptance capture, and counsel
has not approved the text. No real first-customer packet was created.

## Reproduction and limits

Run `python scripts/paid_pilot_readiness_rehearsal.py --cloud` for name-only,
read-only AWS inventory plus the exact nine-gate HOLD matrix. Without
`--cloud`, it is entirely local. It never calls `get-secret-value`, opens
Checkout, mutates AWS, or inspects an environment variable's content.
PostgreSQL tests use a disposable local container/schema, not staging RDS.
The matrix is not a signer for Commercial approvals and must not be copied
into production as if these HOLD records were green.
