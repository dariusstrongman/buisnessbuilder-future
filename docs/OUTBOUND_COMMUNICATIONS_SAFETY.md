# Outbound communications safety v1

## Authority boundary

Outbound Communications Safety is a policy gate, not an orchestrator. Runtime still
admits, budgets, approves, queues and revalidates the bounded job. AI Workforce still
defines the role/capability/action. Provider Connection and the Secret/Artifact Broker
still own external-account and credential access. Company Brain remains canonical
business truth and Verification remains the only Ready/Fully Set authority.

The worker envelope contains only an opaque `communication_ref`. The recipient
destination and message body are never placed in SQS. At execution time the broker
loads the server-owned request and revalidates its Runtime job, tenant, company, role,
capability, provider connection, recipient, purpose, consent, suppression, approval,
content result, rate reservation and expiry before resolving credentials or calling a
provider.

## Recipient, consent and purpose

Recipient records contain the minimum address, relationship and safety state required
to make a decision: normalized email, canonical source, relationship, consent basis and
provenance, suppression/opt-out state, contact timestamps and bounded risk flags.
Email syntax rejects display-name and header-injection forms. SMS, voice and social are
modeled but fail closed because only email policy is implemented.

Every request uses a closed purpose enum. Unknown purposes fail validation. Purpose
selects the allowed provider operation, relationship/consent bases, approval rule and
durable limits. The lowest-risk paths are:

- `reply_to_inbound`: requires a persisted `integration.message.received` event and a
  customer-initiated/transactional/explicit consent context.
- `quote_response`: requires a persisted inbound quote/message event. Price, discount
  and availability claims require current Company Brain fact references.
- `review_request`: requires a persisted `service.completed` event, a service/customer
  relationship and a one-per-recipient daily limit. Incentives require current Company
  Brain authority and approval evidence.

Marketing and re-engagement are approval-required. Sensitive/high-impact content is
also approval-required. The approval reference must appear in the current Runtime
envelope and is revalidated by Runtime. Bulk count greater than one is unconditionally
denied in v1.

## Content and personalization

Content is inspected in process and only its SHA-256 plus non-sensitive decision flags
are persisted. The bounded checker denies secret patterns, unsupported/fabricated
claims, guarantees, false urgency, unapproved prices/discounts/availability/incentives,
privacy or cross-scope data flags, abusive language, unresolved templates, and missing
marketing opt-out disclosure. This checker is a safety floor, not a semantic truth
engine; future live sending needs a reviewed policy/ruleset and evaluation corpus.

Personalization references must resolve to current active records through the public
Company Brain query interface in the same tenant/company. Safety never writes Company
Brain state.

## Suppression, rate limits and durability

Opt-out and provider suppression events are idempotently claimed and persisted. An
opt-out immediately changes consent to withdrawn and suppresses future execution.
Re-enable is never implicit: only OWNER/ADMIN with a trusted principal can record new
explicit consent provenance. Hard bounce and complaint callbacks create suppression.

Rate-limit reservations are atomic database claims keyed by tenant, company,
recipient, purpose and communication idempotency. PostgreSQL uses an advisory
transaction lock for competing sends in the same scope; SQLite uses its repository
write lock for offline tests. Per-minute, per-hour, per-day and ten-second burst limits
are enforced for both company and recipient. Duplicate idempotency returns the prior
reservation without consuming another slot.

ProviderReceipt adds only safe communication metadata: recipient reference, purpose,
policy decision, approval reference, suppression/content results, rate reservation and
delivery/reconciliation state. Raw message bodies and destinations are not receipts.
Stable provider request IDs suppress duplicate sends and permit reconciliation after a
crash between provider acceptance and local acknowledgement.

Authenticated, provider-verified delivery callbacks support queued, accepted,
delivered, deferred, bounced, rejected, complained and unknown. Callback event IDs are
one-time durable claims. Replays return current state without a second mutation.

## Customer API

| Method | Route | Authority |
|---|---|---|
| GET | `/api/v1/companies/{company_id}/recipients/{recipient_id}` | scoped company view |
| GET, POST | `.../recipients/{recipient_id}/suppression` | view; OWNER/ADMIN create typed admin/legal/abuse suppression |
| POST | `.../recipients/{recipient_id}/opt-out` | OWNER/ADMIN |
| POST | `.../recipients/{recipient_id}/re-enable` | OWNER/ADMIN + explicit consent provenance |
| GET | `/api/v1/companies/{company_id}/communication-policy` | scoped company view |
| GET | `/api/v1/companies/{company_id}/communications` | scoped company view |
| GET | `/api/v1/companies/{company_id}/deliveries/{delivery_id}` | scoped company view |

The API masks the destination and excludes risk flags, provider request IDs, external
object refs, raw policy internals, broker locators, secret refs and provider payloads.
SUPPORT may view only within an active scoped grant/session; SUPPORT cannot opt out on
behalf of a customer or re-enable a recipient.

## Deployment state and blockers

All provider actions remain deterministic sandbox operations and `.example.test`
recipients. No live email integration or unrestricted sending is enabled. A future live
communications subsystem requires Darius approval for legal/jurisdictional consent
rules, retention/PII handling, customer-facing consent and unsubscribe UX, reviewed
content policy/evaluations, provider-specific bounce/complaint authenticity, abuse
monitoring, operational alerting and an explicit live-send kill switch.
