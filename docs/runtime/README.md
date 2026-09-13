# Core Runtime / Nervous System

Status: offline/dev-safe provider-neutral runtime. No cloud queues, live providers, credentials, billing, deployment, Company Brain internals, verification policy, website generation, or Sol runtime code.

## Architecture

`JobOrchestrator` coordinates a tenant/company-scoped job through five replaceable boundaries:

1. `CompanyStateReader` confirms the company exists. `CompanySnapshotProvider` defines the future read-only snapshot seam.
2. `CapabilityRegistry` resolves a provider by stable capability name and version. Providers implement `validate_request`, `estimate`, `execute`, `status`, `cancel`, and `collect_result`.
3. `RuntimeRepository` owns persistence behavior. `SQLiteRuntimeRepository` is the v1 local adapter and applies migration version 1 at startup.
4. `LocalEventBus` synchronously persists then dispatches events in handler-registration order. `event_id` is the deduplication key.
5. `VerificationPort` receives a verification request after successful artifact/spend completion. It does not decide verification or readiness.

The runtime lifecycle is:

`queued → waiting_dependencies → waiting_approval → runnable → running → succeeded | failed | cancelled`

States that are not needed are bypassed through explicit legal transitions. Illegal transitions raise `IllegalTransition`. A retryable provider failure may move `running → runnable` only while the bounded retry policy has attempts remaining.

## Released contract compatibility

Runtime models are richer than the released schemas without changing those schemas:

- Runtime `Event` includes `tenant_id`, `type`, `source`, and integer `version`; `Event.to_contract()` projects these onto Event v1's `event_type`, `producer`, and `schema_version`. Tenant scope remains an internal storage/routing invariant because Event v1 does not yet expose it.
- Runtime `Job` has explicit dependency and runnable states. `Job.to_contract()` maps `waiting_dependencies` to v1 `blocked` and `runnable` to v1 `queued`.
- Audit records retain runtime `tenant_id`; `to_audit_contract()` removes that internal routing field for AuditEvent v1 validation.
- `FakeWebsiteCapability` validates Website Capability v1 requests and produces a validated v1 result projection. It does not implement website generation.

No files under `/contracts` are changed.

## Safety and money

- Permission modes are `autonomous`, `approval_required`, `founder_only`, and `prohibited`.
- Approval receipts are subject-digest-bound, role-bound, expiration-aware, and explicit. Founder-only work never accepts a non-founder actor.
- Money uses integer minor units only. Budget reservation precedes provider execution; settlement cannot exceed the reservation; unused reservation is released.
- Jobs and capability attempts have scoped idempotency keys. Duplicate events, job requests, runs, cancellation requests, and completed capability requests do not repeat side effects.
- SQLite triggers reject every update or delete against `audit_events`.

## Fakes and integration proof

Registered local providers are `fake.website.build`, `fake.crm.setup`, and `fake.email.setup`. The executable Billy Bob fixture is `fixtures/runtime/billy_bob.py`. It proves:

`company.created → website job waiting on founder → founder.approved → fake package/spend → job.completed → verification.requested`

It deliberately does not calculate Ready or Fully Set.

## Run tests

```bash
PYTHONPATH=src:. python -m unittest discover -s tests/runtime -v
```
