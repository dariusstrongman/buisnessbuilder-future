# Communications compliance and operations v1

## Status and authority

This subsystem is a sandbox-only policy and operational-control layer. It is not a
legal-completeness claim, a provider, or an orchestrator. Runtime still authorizes and
orchestrates jobs; AI Workforce controls roles and capabilities; Provider Connection
owns external-account lifecycle; the Broker owns scoped credential access; Outbound
Safety owns recipient/content/rate enforcement; Company Brain and Verification retain
their existing authority.

The delivery adapter allowlist contains only deterministic sandbox providers. The
server-side live-send invariant is false, client/model input has no live-enable field,
and rollout transitions above `SANDBOX` are rejected in code.

## Initial jurisdiction policy

`communications-compliance.us-federal.v1` is a deliberately narrow staging policy
model, not legal advice. It provides dimensions for tenant, company and recipient
jurisdiction; channel; purpose; consent basis; retention; opt-out and disclosure;
marketing restrictions; quiet hours; recordkeeping; and reconsent requirements.

Only the explicit `US` staging baseline is built in. State/territory or other-country
overlays must be supplied as versioned policy objects. Missing, unknown, or unsupported
jurisdictions deny admission. The implementation does not claim to encode CAN-SPAM,
TCPA, state privacy law, sector-specific rules, international law, or provider terms
completely. Counsel must approve policies, retention periods, disclosures, and consent
semantics before any live launch.

## Consent and unsubscribe

Consent evidence is append-preserved and attributable to a canonical business event or
a trusted OWNER/ADMIN principal. Evidence records purpose, channel, source, timestamp,
actor/event, opaque evidence reference, policy and terms versions, expiry, withdrawal,
and supersession. Agents cannot call the trusted capture path or fabricate a canonical
source event. A current send evaluates current policy against historical versioned
evidence and can express a reconsent requirement.

Unsubscribe tokens contain only random material and a server HMAC. PostgreSQL stores a
SHA-256 digest and server-owned scope, never an address or tenant/company identifier in
the URL token. The public endpoint returns the same short response for valid and invalid
tokens. Consumption, withdrawal, suppression, and replay state are durable and
idempotent. Re-enable requires OWNER/ADMIN, a supported jurisdiction, no legal/complaint
block, and new explicit consent provenance.

## PII retention and erasure

The minimal classification vocabulary distinguishes destination identifiers, recipient
metadata, message content, delivery metadata, consent evidence, audit metadata, and
provider identifiers. Operational recipients, delivery state, ProviderReceipts,
compliance decisions, consent evidence, and audit evidence have separate versioned
retention records.

Erasure and retention expiry pseudonymize operational destinations with a one-way digest
tombstone while retaining suppression, preventing accidental re-contact. Compliance
evidence and held records survive. Cross-scope deletion, hold bypass, and implicit
restoration are denied. All operations are idempotent and audited without raw content,
destinations, secrets, or provider payloads.

## Callback, abuse, and alerts

Provider-neutral callback ports support authenticated adapters; the deterministic
sandbox adapter uses HMAC. Unsigned or forged delivery, bounce, and complaint callbacks
cannot mutate trusted state. Callback IDs are durable one-time claims. Hard bounce and
complaint immediately suppress; complaint also creates an abuse signal. Deferred/soft
states remain retryable reconciliation states, rejected responses retain safe
classification, and unknown states make no unsafe inference. Provider Connection's
existing authenticated callback path remains responsible for revocation/disconnect.

Explainable abuse signals aggregate over configurable windows. Explicit warning,
throttle, suspend, and emergency thresholds persist alerts; suspend/emergency engages a
company kill switch. Notifications use a no-op port in this branch. Safe customer views
show class, severity, reason code, and time only; the server-side operator summary shows
counts, never destinations, bodies, secrets, hidden scores, or raw provider data.

## Kill switches and rollout

Kill switches can be global, tenant, company, provider-connection, or channel scoped.
They are checked at request admission and again immediately before the Broker invokes a
provider, so an already-queued job cannot bypass a new pause. OWNER/ADMIN may change only
company state. Platform scope requires an injected trusted operator verifier. SUPPORT
and agents have no mutation authority. Every change and killed-send attempt is durable.

The rollout model represents `DISABLED`, `SANDBOX`, `INTERNAL_CANARY`, `LIMITED_LIVE`,
and `GENERAL_LIVE`, plus tenant/company/provider/purpose/domain allowlists and send,
recipient, founder-approval, and monitoring constraints. In this branch only
`DISABLED` and `SANDBOX` transitions are accepted, and sandbox allowlists are enforced.

## Customer-safe API

| Method | Route | Behavior |
|---|---|---|
| POST | `/api/v1/communications/unsubscribe` | login-free, opaque, oracle-safe unsubscribe |
| GET | `/api/v1/companies/{company_id}/communications/compliance-status` | safe connection/sandbox/pause/action summary |
| GET | `/api/v1/companies/{company_id}/communications/alerts` | safe alert summaries |
| GET, POST | `/api/v1/companies/{company_id}/communications/send-state` | view; OWNER/ADMIN company pause control |
| GET | `/api/v1/companies/{company_id}/recipients/{recipient_id}/consent` | scoped evidence summary |
| POST | `/api/v1/companies/{company_id}/recipients/{recipient_id}/erasure` | authorized idempotent erasure workflow |

Existing suppression, recipient, delivery, communication-history, and policy endpoints
remain available. Strict methods, server-derived principal scope, request limits,
sanitized errors, denial audit, and response minimization remain in force.

## Live-launch blockers

Before live sending, Darius and qualified legal/privacy reviewers must approve supported
jurisdiction overlays, consent and reconsent semantics, disclosures, quiet hours,
recordkeeping/retention, erasure exceptions and holds, provider-specific webhook
authenticity, abuse-rate denominators, operator escalation/on-call procedures, canary
allowlists, deliverability/domain controls, and an operational live-gate ceremony. This
branch intentionally supplies no procedure capable of enabling live delivery.
