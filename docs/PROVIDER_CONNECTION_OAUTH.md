# Provider Connection and OAuth v1

## Authority boundary

Provider Connection owns external-account lifecycle only. Identity authenticates the
customer; Commercial determines entitlement; Runtime authorizes and orchestrates a
bounded job; AI Workforce decides the role/capability/action; the existing broker
resolves an opaque credential reference; Company Brain and Verification remain the
only authorities for business truth and readiness.

There is no provider-side orchestrator and no standing per-customer process.

## OAuth flow

1. An OWNER or ADMIN uses an authenticated session and a company-scoped API route.
2. The server revalidates user, session, tenant, organization, membership, role and
   company, then stores a short-lived OAuth transaction containing digests only.
3. A random state, nonce and S256 PKCE challenge bind the request to the session,
   exact redirect URI and server-derived tenant/company.
4. The callback accepts no tenant/company authority. The state locates the durable
   server record, the current session and company authority are revalidated, and the
   transaction is atomically consumed once before code exchange.
5. Ephemeral access/refresh tokens are written through `SecretStorePort`. PostgreSQL
   stores only the opaque secret reference, expiry, granted scopes, safe account
   metadata and lifecycle status.
6. Capability grants map only existing AI Workforce actions. `mail.read` permits the
   Inbox Assistant read/classify/attachment metadata operations; `mail.send` permits
   preapproved sandbox reply operations. Review Follow-up receives only its existing
   draft and preapproved-send actions.

SUPPORT and MEMBER cannot start, disconnect or reconnect provider connections.
SUPPORT never inherits provider-management authority through impersonation.

## Execution and refresh

The job envelope carries only `secret_ref`, provider, capability and tenant/company
scope. Before a provider action, Runtime and broker authorization are rechecked.
Near-expiry or expired access is refreshed under a durable PostgreSQL lease. One
worker rotates the vault value and metadata; racing workers observe the updated
expiry or retry. `invalid_grant` and revoked refresh credentials fail closed and move
the connection to `reconnect_required`.

The sandbox email adapter is process-local and cannot contact a network. It supports
the existing Inbox and Review Follow-up email actions, uses stable provider request
IDs, and returns non-sensitive object references. The durable ProviderReceipt now
records its connection ID, so duplicate delivery or a crash after provider success
is reconciled without repeating the provider action.

## Revocation, callbacks and reconciliation

Customer disconnect, signed provider callbacks, scope removal, account deletion,
compromise, suspension and entitlement failure deny new resolution immediately.
Callback signatures are verified before mutation and callback event IDs are claimed
durably once. Invalid/replayed callbacks are audited without payloads or token data.

Reconciliation is represented as a normal durable Runtime schedule. It wakes only
when due and verifies account existence, token validity and granted scope. Provider
health records contain counters, usability, recent error classification, rate-limit
timing and last reconciliation—not provider payloads.

## Customer API

| Method | Route | Authority |
|---|---|---|
| GET, POST | `/api/v1/companies/{company_id}/provider-connections` | View; OWNER/ADMIN manage |
| GET | `/api/v1/companies/{company_id}/provider-connections/{connection_id}` | Company view |
| POST | `.../{connection_id}/disconnect` | OWNER/ADMIN |
| POST | `.../{connection_id}/reconnect` | OWNER/ADMIN |
| POST | `/api/v1/provider-connections/oauth/callback` | Active bound session + one-time state |

Responses deliberately exclude tenant routing, secret refs/locators, token material,
provider payloads, broker locators, IAM details and internal audit records.

## Production blockers

- A real provider adapter requires Darius-approved provider registration, redirect
  URIs, credential scopes and legal/customer-consent UX.
- Provider callback signature schemes differ and require provider-specific adapters.
- Production Secrets Manager locators and task-role policies must be provisioned per
  approved connection strategy; wildcard secret access is forbidden.
- Real email sending stays disabled until recipient policy, consent/suppression,
  content review and delivery monitoring are approved and tested.
