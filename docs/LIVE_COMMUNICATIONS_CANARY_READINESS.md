# Live Communications Canary Readiness v1

Status: repository implementation complete; sandbox simulation only. `live_send_enabled` is a server-owned constant and remains `False`. No live provider adapter is registered.

This layer answers whether an already-authorized company/provider/sender is operationally ready for a future narrow canary. It does not authenticate users, grant entitlements, orchestrate jobs, decide ordinary communication policy, resolve secrets, or send mail. Identity, Commercial, Runtime, AI Workforce, Provider Connection, the broker, outbound safety, and Communications Compliance remain authoritative.

## Provider boundary

`EmailDeliveryAdapter` declares OAuth scopes, send operation, provider request identifiers, idempotency strategy, quotas, callback event types and verification, token refresh, error classification, and sender requirements. The only implementation in this branch is a deterministic emulator whose contract declares `network_delivery_enabled=False`; its `send` method always raises.

The future Gmail contract must use least-privilege OAuth scopes, broker-managed refresh tokens, the existing ProviderReceipt idempotency key, a stable outbound message fingerprint, and sent-mail/history reconciliation. Gmail mailbox changes use Cloud Pub/Sub notifications and a history cursor; watches expire and must be renewed. Google documents the Gmail API, OAuth scopes, quotas, and push/watch behavior at:

- https://developers.google.com/workspace/gmail/api/reference/rest
- https://developers.google.com/identity/protocols/oauth2/scopes
- https://developers.google.com/workspace/gmail/api/reference/quota
- https://developers.google.com/workspace/gmail/api/guides/push

## Provider comparison

| Criterion | Gmail / Google Workspace | Microsoft Graph / Outlook | Transactional provider (Amazon SES) |
|---|---|---|---|
| Customer mailbox model | Strong delegated mailbox and thread/reply semantics | Strong delegated mailbox, draft, reply, and send semantics | Sending identity, not a customer mailbox |
| OAuth | User consent; Gmail scopes require careful minimum selection and provider review | Entra delegated permissions and tenant-consent complexity | AWS IAM/API credentials rather than customer mailbox OAuth |
| Send result | Message resource; no provider-level Business Builder idempotency guarantee, so receipt + reconciliation required | `sendMail` returns accepted, not final delivery; receipt + reconciliation required | API message ID plus strong event-publishing support |
| Mailbox events | Gmail watch via Pub/Sub; history reconciliation required and watch renewal at least every seven days | Change-notification subscriptions with renewal; client-state/JWT validation options | Configuration-set events through SNS/CloudWatch/Firehose |
| Bounce/complaint visibility | Mailbox DSNs and provider-specific reconciliation; weaker structured complaint telemetry | Mailbox/NDR reconciliation; weaker structured complaint telemetry | Strong native bounce, complaint, reject, delay, and delivery events |
| Limits | Per-project and per-user quota units; `messages.send` consumes quota | Per-mailbox and app/tenant throttles; four concurrent Outlook requests documented | Account/region sending quotas and sandbox restrictions |
| Sender/domain reputation | Existing Workspace mailbox/domain reputation is relevant | Existing M365 mailbox/domain reputation is relevant | Requires verified identity and deliberate domain reputation management |
| Operational fit | Best fit for the first `reply_to_inbound` mailbox pilot | Good second adapter for Microsoft-heavy customers | Best later option for transactional notices, not the first delegated-inbox pilot |

Microsoft documents Outlook mail creation/sending, throttles, and authenticated change notifications at https://learn.microsoft.com/en-us/graph/outlook-create-send-messages, https://learn.microsoft.com/en-us/graph/throttling-limits, and https://learn.microsoft.com/en-us/graph/change-notifications-with-resource-data. Amazon documents SES authentication and event publishing at https://docs.aws.amazon.com/ses/latest/dg/send-email-authentication-spf.html and https://docs.aws.amazon.com/ses/latest/dg/monitor-sending-using-event-publishing-setup.html.

Recommendation: begin future approval work with one dedicated Google Workspace test mailbox and the Gmail API. It best matches the existing lowest-risk `reply_to_inbound` use case. This recommendation is conditional: OAuth verification, a Pub/Sub authenticity design, watch renewal, DSN/bounce reconciliation, and an explicit legal/security review must be completed before registration or live use. SES should be evaluated separately for transactional-notice delivery where its native bounce/complaint telemetry is a stronger fit.

## Readiness and sender models

`SenderIdentityReadiness` records the synthetic/test sender, connection, domain ownership, SPF, DKIM, DMARC, alignment, provider verification, and reputation. `DeliverabilityHealth` records synthetic success, defer, hard-bounce, complaint, rejection, throttle, auth-failure, reputation-warning, callback, and reconciliation signals. Unknown, missing, or failed domain authentication blocks eligibility.

`CanaryEligibility` returns every gate and every failed reason. The gates are active tenant, organization and company; active `ai_workforce.execute` managed entitlement; active/healthy connection; required OAuth scopes; bound sender; valid domain auth; good reputation; clean deliverability health; configured jurisdiction/consent; operational unsubscribe/suppression, callback, kill, alert, and abuse controls; available runbook; recorded privileged approval; and both live gates still off.

## Ceremony and permit

Only an injected server-side `operator_verifier` can begin the ceremony. A separate trusted founder-approval verifier must confirm the approval reference. Customer endpoints, tenant settings, SUPPORT, models, agents, and callbacks have no mutation path.

The ceremony records target tenant/company/provider/sender, exact purposes and recipient digests/domains, total/hour caps, start/expiry, monitoring owner, rollback reference, kill-switch test, and approval. It emits a server-HMAC-signed, simulation-only permit. Wildcards, bulk, marketing, windows over 24 hours, and caps above 10 are rejected. Permits do not renew; expiry or revocation requires a new ceremony.

At admission and immediately before provider action, the permit signature, scope, recipient digest, purpose, current eligibility, current entitlement, provider health, and kill switches are revalidated. At execution, an atomic PostgreSQL/SQLite permit reservation prevents concurrent total/hour cap bypass. The normal communication rate limiter and ProviderReceipt idempotency remain independently authoritative.

## Callback and reconciliation requirements

The Gmail pilot must authenticate its Pub/Sub transport (OIDC/JWT audience, issuer, signature and service-account binding), bind notifications to the stored tenant/company connection, use event/history identifiers for replay suppression, and reconcile from the last durable Gmail history ID. A notification is only a hint to reconcile; it cannot enable sending or directly assert delivery. Delivery/NDR evidence must be matched to the stable provider request/message reference. Uncertain actions run as bounded Runtime-scheduled reconciliation jobs; there is no busy loop or standing worker added here.

Microsoft Graph would require subscription `clientState` validation and, for rich notifications, JWT issuer/signature/audience/caller checks. SES would require AWS-authenticated SNS event delivery, source/topic policy checks, event ID replay suppression, and configuration-set reconciliation.

## Monitoring and alerts

Repository-backed metrics cover admission/execution allows, policy denials, provider outcome, bounce, complaint, suppression, callback verification failure, rate pressure, kill-switch activation, permit use, duplicate suppression, and reconciliation backlog/resolution. Canary alerts are explainable and omit raw destinations and content.

- Critical: complaint in tiny canary, unexpected recipient, callback-auth spike, kill bypass attempt, credential compromise, or send outside permit.
- High: hard bounce, repeated provider failures/throttling, or unresolved provider result.
- Warning/info: reconnect approaching, SPF/DKIM/DMARC degradation, or nearing a cap.

## Safe status views

Customers receive only `Provider connected/needs reconnect`, `Sandbox testing`, `Needs domain setup`, `Canary review pending`, or `Sending paused`, plus `live_send_enabled: false`. The authenticated route is `GET /api/v1/companies/{company_id}/communications/canary-readiness`; there is no enable endpoint.

Trusted operators can retrieve a scoped summary containing connection, sender/domain, compliance, consent/suppression, callback, abuse/alert, kill-switch and failed-gate status. It is status only and cannot change rollout.

## Remaining approval gates before any real email

No real email may be sent until Darius separately approves all of: provider selection and app registration; legal review of the narrow jurisdiction/purpose policy; security review of exact OAuth scopes and callback validation; real-domain SPF/DKIM/DMARC and alignment evidence; privacy/DPA and data-retention terms; an internal mailbox and exact internal recipient allowlist; production secrets/IAM design; monitoring owner and paging channel; provider-specific reconciliation; an executed kill-switch drill; production change review; and a time-limited live permit ceremony implemented in a later branch. This branch deliberately cannot create such a live permit.
