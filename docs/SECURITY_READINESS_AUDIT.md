# Security and Production-Readiness Audit

Audit branch: `codex/security-readiness-v1`
Starting checkpoint: `485521571c78742ba89065082346706cf60f5ba6`

## Readiness conclusion

The repository is safer for staging and offline proof use after the fixes on this
branch, but it is not approved for production. The current HTTP process exposes a
read-only prototype, service discovery, and database readiness only. It does not
implement an authenticated customer API or bind authenticated session identity to
tenant/company context.

Company Brain remains canonical for company state, Runtime remains authoritative
for execution/approval/budget controls, and Verification remains the sole Ready and
Fully Set authority.

## HTTP route exposure matrix

| Methods | Routes | Classification | Authentication | State change | Production default |
|---|---|---|---|---|---|
| `GET`, `HEAD` | `/`, `/index.html`, `/app.js`, `/data.js`, `/styles.css` | Public static prototype | None | None | Enabled |
| `GET`, `HEAD` | `/api/v1` | Public service discovery | None | None | Enabled; minimal metadata |
| `GET`, `HEAD` | `/healthz` | Health/readiness | None | None | Enabled; returns only health state |
| `GET`, `HEAD` | `/proof`, `/api/v1/proofs/billy-bob-commercial` | Development/proof-only | None | Runs fictional PostgreSQL proof | Hidden unless explicitly enabled in `development`, `local`, or `test` |
| `GET`, `HEAD` | `/api/v1/proofs/billy-bob-commercial/state` | Development/proof-only | None | None | Hidden unless explicitly enabled in `development`, `local`, or `test` |
| `OPTIONS` | Any path | Protocol metadata | None | None | Enabled without cross-origin grants |
| `POST`, `PUT`, `PATCH`, `DELETE`, `TRACE`, `CONNECT` | Any path | Denied | N/A | None | `405 Method Not Allowed` |
| `GET`, `HEAD` | Any unlisted path | Denied | N/A | None | `404 Not Found` |

There are currently no authenticated, tenant-scoped, or support-only HTTP routes.
Identity, commercial, Company Brain, Runtime, and Verification are application
services and storage ports, not remotely exposed APIs.

## Fixed findings

| Severity | Finding | Resolution |
|---|---|---|
| High | Unauthenticated `GET` requests could execute the PostgreSQL Billy Bob proof and read its stored state. | Proof routes are hidden by default and cannot be enabled in staging or production environments. Offline proof remains available through an explicit local/dev/test opt-in. |
| High | Billing-event duplicate detection was a check-then-write race, and a failure could leave partial commercial state while allowing unsafe retries. | Repository transactions now serialize/claim delivery, roll back aggregate/audit/idempotency state on failure, and atomically claim provider events in PostgreSQL. |
| Medium | A late payment-failure event emitted a failure event after an order had already reached paid fulfillment state. | Late failure delivery is recorded as processed without emitting false current-state output. |
| Medium | Normalized billing events accepted empty, overlong, and timezone-naive boundary values. | Contract-aligned identifier, length, and timezone validation was added. |
| Medium | Health checks ran migrations and disclosed environment, version, instance, and persistence details. | Health is read-only, requires an already migrated schema, and returns only `ok` or `unhealthy`. Startup migration remains a separate operation. |
| Medium | Concurrent startup migration calls could repeatedly replace triggers and race. | A PostgreSQL transaction advisory lock and applied-version no-op protect migration startup. |
| Medium | Static serving implicitly trusted all files beneath the prototype directory. | Public files are explicitly allowlisted; directory listing and unlisted paths are denied. |
| Medium | Default HTTP errors/logs could disclose interpreter version, query strings, or exception text. | Server branding, error bodies, and request/error logging are sanitized. |
| Medium | Request framing and size were not bounded. | Duplicate/invalid lengths and transfer encoding are rejected; bodies are capped at 1 MiB and request targets at 8 KiB. |
| Low | Dynamic PostgreSQL conflict clauses triggered unsafe-query analysis despite using internal constants. | Queries now use psycopg composable SQL; all record values remain bound parameters. |

## Verified controls

- Identity authorization is default-deny over persisted active user, tenant,
  organization, membership, company, support grant, and support-session state.
- OWNER, ADMIN, MEMBER, and SUPPORT permissions and support expiry/scope are covered
  by existing identity tests.
- Company Brain, commercial, Runtime, Verification, and Build Room reads fail closed
  across tenant/company boundaries.
- Founder-only Runtime approvals are digest-bound, role-bound, and expiry-aware at
  the Runtime layer.
- Commercial operations cannot change Ready or Fully Set. Billing cancellation does
  not delete customer-owned state.
- SQL values are parameterized. PostgreSQL schema identifiers are allowlisted and
  composed with `psycopg.sql.Identifier`.
- CORS grants are absent by default. Security headers apply to JSON, static, error,
  and method-denial responses.

## Remaining production blockers

1. **High — no authenticated HTTP application boundary.** There is no live
   authentication adapter, session middleware, server-derived tenant selection, or
   authenticated customer API. Do not expose future domain routes until this exists.
2. **High — Runtime approval identity is caller asserted.** `approve_job` accepts
   `actor_id` and `actor_role`; Runtime does not yet verify them against the Identity
   service. Changing this boundary may affect external Runtime/website adapters and
   requires an integration decision rather than a unilateral change in this audit.
3. **High — no durable commercial outbox.** Commercial PostgreSQL writes are atomic,
   but publication to the separately persisted Runtime event bus is not atomically
   coupled to the commercial transaction. Production requires an outbox/inbox or
   equivalent delivery design before real billing events.
4. **High — PostgreSQL behavioral verification needs an isolated test database.**
   The PostgreSQL suite is intentionally skipped without
   `BUSINESSBUILDER_TEST_POSTGRES_DSN`. This audit did not use staging RDS because
   deployed infrastructure was out of scope.
5. **Medium — denied-action audit is incomplete.** Successful privileged actions are
   audited, but authorization denials do not yet produce a durable, rate-controlled
   security event.
6. **Medium — production edge controls are absent.** The standard-library HTTP
   server has no application-level rate limiting, trusted-proxy policy, TLS/HSTS,
   or production request timeout controls. These require an approved serving/edge
   architecture.
7. **Medium — live provider verification is intentionally absent.** The identity and
   billing ports have offline adapters only. No provider webhook signature verifier
   or production identity verifier is configured.
8. **Low — static analysis notes container-wide binding and runtime assertions.**
   Binding to `0.0.0.0` is required by the current container model and must be
   constrained by deployment networking. Assertions in offline fixtures, plus one
   logically unreachable Runtime assertion, should not be treated as production
   validation.

Items 1–3 require Darius approval because they select or alter integration/security
boundaries. Items 4 and 6 require an expressly isolated test/serving environment.
