# Verification / Readiness (Worker 3)

Status: offline/dev-safe subsystem. It performs no external calls and trusts no provider status by itself.

## Boundary

`src/businessbuilder/verification/` owns cross-capability evidence evaluation, verification state, dependency invalidation, blockers, and the Ready/Fully Set projection. It does not own Company Brain storage, event/job orchestration, website QA internals, provider integrations, or external authority decisions.

Public entry points:

- `VerificationService`: creates records, enforces transitions/evidence, expires records, records failures, and invalidates changed dependencies.
- `ReadinessEvaluator`: evaluates a `CompanySnapshot` against a configurable `ReadinessPolicy`.
- `VerificationEventHandler`: accepts canonical-shaped dependency-change events and calls the service. Worker 2 remains responsible for durable event delivery/deduplication.
- `CompanySnapshotPort`: the read boundary expected from Worker 1.
- `VerificationRepository`: persistence port with in-memory and atomic local JSON implementations.

## State and evidence rules

The successful path is strictly `proposed -> executed -> tested -> verified`. Direct jumps and repeat transitions raise `IllegalTransitionError`. Tested requires a current passing `test_result` whose test name is registered in the definition. Verified additionally requires a permitted verification method, owner, nonempty scope, and every evidence type required by the definition.

Evidence is metadata pointing at an Artifact (`artifact_ref`), never an embedded screenshot/result blob. A screenshot never counts as a defined test. Provider receipts support only definitions that explicitly require them; they never turn provider completion into verification on their own.

Expiry changes Verified to Expired; Expired may re-enter Executed for a fresh test cycle. A dependency version advance downgrades affected Tested/Verified/Expired records to the definition's safe invalidation state (Executed by default). A recorded failure falls back to the highest prior state still supported by current evidence and retains its reason. Retesting after invalidation and reverifying after failure require newly supplied evidence; old proof cannot silently restore status.

## Readiness

The Billy Bob `mobile_service.v1` policy requires:

- an approved offer and approved service area from Company Brain;
- a verified customer contact path;
- verified lead intake, quote, conditional scheduling, and support paths;
- known ownership;
- a tested rollback/failure path;
- all selected critical founder actions completed;
- no open critical blocker (and no noncritical blocker when policy disallows it).

Ready deliberately does not imply that every optional/admin item is finished.

Fully Set requires Ready plus all selected admin/provider verification definition IDs, all selected founder actions verified, monitoring and handoff verified, no material waiver, and no critical unresolved obligation.

## External truth

`ExternalRecord` distinguishes prepared, submitted, observed, founder-attested, and externally-verified states. `externally_verified` is authoritative only when a named authority and current authority/provider evidence are recorded. The platform does not claim to be a registrar, insurer, bank, payment provider, licensing body, or formation authority.

## Shared contract gap (no schema changed)

The current `contracts/verification.schema.json` predates this implementation and has no fields for `tenant_id`, `definition_id`, named owner, typed evidence metadata, dependency versions, stale reason, created timestamp, or provenance. The domain record stores all of them and exposes `to_contract()` for a schema-compatible v1 projection.

Recommended future additive v1 fields are: optional `tenant_id`, `definition_id`, `owner_ref`, `dependency_refs` (ID plus observed version), `created_at`, `provenance`, and `stale_reason`. Typed Evidence should remain an Artifact-backed record or receive its own shared schema. This branch intentionally does not edit shared contracts while Workers 1 and 2 are active.

## Assembly assumptions

- Worker 1 can expose an immutable, tenant-scoped snapshot containing approved offers/service areas, owners, selected founder actions, scheduling requirements, selected Fully Set obligations, waivers, and dependency/version identifiers. The included `DictCompanySnapshotPort` is only a fixture adapter.
- Worker 2 can deliver at-least-once canonical events with tenant context supplied by its authenticated envelope. Existing Event v1 carries `company_id` but not `tenant_id`; the integration adapter must derive tenant scope from trusted context, never payload free text. Durable deduplication replaces the handler's process-local fixture set.
- Worker 2 calls `VerificationService`; Verification does not create jobs, approvals, budgets, or audit truth.
- Website provider evidence arrives as Artifact references. `package_ready` and provider QA do not satisfy deployment, contact-path, or readiness definitions.
