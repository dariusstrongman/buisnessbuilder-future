# Company Brain Implementation

## Status

Offline/dev-safe implementation of the business-builder Head. It is production-shaped, not production-deployed. It does not access Stromation, Website Builder, Claude Phase 1E or any live provider.

## Package

`src/businessbuilder/company_brain/`

| Module | Responsibility |
|---|---|
| `model.py` | Scope, Company, typed record kinds, knowledge classes, provenance, lifecycle and invalidation data |
| `repository.py` | Provider-neutral storage protocol |
| `sqlite_repository.py` | Local SQLite adapter with scoped queries, transactions and optimistic concurrency |
| `service.py` | Public application/service interface |
| `snapshot.py` | Deterministic, allowlisted, redacted capability snapshot |
| `fixtures.py` | Fictional Billy Bob lawn-care fixture |
| `migrations/` | Versioned local schema |

## Public service interface

`CompanyBrainService` exposes:

- `create_company(company)`
- `get_company(scope)`
- `update_company_profile(scope, expected_version=..., ...)`
- `transition_company(scope, target, expected_version=...)`
- `update_approved_state(...)`
- `record_decision(...)`
- `record_fact(...)`
- `record_estimate(...)`
- `record_inference(...)`
- `attach_artifact(...)`
- `attach_evidence(...)`
- `append_founder_action(...)`
- `append_approval(...)`
- `append_verification(...)`
- `append_audit_event(...)`
- `add_dependency(...)`
- `query_current_state(...)`
- `query_history(...)`
- `query_company_history(...)`
- `compact_snapshot(...)`

Every read/write requires an immutable `Scope(tenant_id, company_id)`. Missing or foreign-scope records return `NotFoundError`; the repository never searches globally to satisfy a scoped request.

## Domain model

The Company object implements the shared `company.v1` contract. Company Brain entities use a common `BrainRecord` envelope and typed `RecordKind`. Supported kinds cover Party, Goal, Strategy, Decision, Offer, Service, Market, Policy, Capacity, Asset, Account, Vendor, Customer/Lead references, Workflow, Agent, Approval/Verification/Evidence references, Risk, Obligation, Metric, FounderAction, AuditEvent and Artifact reference.

This generic envelope is deliberate: it keeps versioning, ownership, provenance, classification and isolation identical while subsystem-specific contracts evolve. It is not an untyped document store; record kinds and knowledge classes are closed enums, required metadata is validated, and material mutations use optimistic versions.

## Evidence semantics

`KnowledgeClass` separates:

- `fact`
- `estimate`
- `inference`
- `founder_decision`
- `external_verification`

Confidence is nullable and bounded from 0 to 1. It records uncertainty for estimates/inferences; it never changes the knowledge class or creates verification. Verification remains a reference to the later verification subsystem.

## History and concurrency

Material records are append-versioned. A change inserts version N+1, records `supersedes_version`, and marks the prior version with `superseded_by_version`. Company profile and lifecycle versions are also copied into an immutable history table before the current pointer advances. Current-state queries return only unsuperseded versions. History returns every version. Expected-version checks reject stale writes.

Company lifecycle uses a fail-closed state graph:

`draft → challenged → approved → assembly → ready → fully_set → operating → paused → archived`

Only explicitly modeled recovery paths are allowed. Illegal transitions and no-op transitions raise `InvalidTransitionError`.

## Dependency and invalidation hooks

The repository stores scoped dependencies from a Company Brain source to an opaque dependent reference. Updating a material Party, Offer, Service, Market, Policy, Account or Workflow record emits append-only `InvalidationNotice` rows. Company-level `company.owners` and `company.jurisdiction` sources cover ownership and jurisdiction changes without inventing a second Company record. The future verification worker consumes these notices and decides how a verification changes. Company Brain does not implement that engine or claim anything was reverified.

## Capability snapshots

`compact_snapshot` produces canonical JSON-compatible data sorted by record kind and ID. It includes only capability-relevant kinds, current versions and provenance digests. Accounts, founder actions, approvals, verification references, metrics, customer/lead references and audit events are excluded. Keys suggesting secrets, tokens, passwords, credentials, private keys or PII are redacted. The digest is computed over canonical JSON, so identical state produces identical output.

This is the future Runtime/Orchestrator consumption boundary. Callers receive a snapshot, never repository access.

## Storage abstraction

The domain service depends on `CompanyBrainRepository`, not SQLite. `SQLiteCompanyBrainRepository` is an adapter for local development. Replacing it with Postgres or a cloud store requires implementing the protocol while preserving scoped operations, append versioning, optimistic concurrency and transaction behavior.

Migrations are local, ordered and idempotent. No cloud migration exists.

## Billy Bob fixture

The fixture creates a fictional Texas mobile-service company with Billy Bob as sole founder, Denton County jurisdiction, a 12-mile service area, mow/edge/blow offer, excluded regulated/complex work, founder-owned equipment/accounts, a 24-hour capacity estimate, limited scope and a founder-only domain-purchase action.

## Intentionally deferred

- Runtime/orchestrator implementation
- Website generation or Stromation adapter
- Verification/readiness engine
- External integrations and provider credentials
- Authentication, authorization transport and secret storage
- Network API/HTTP server
- Production database, backup, encryption and deployment
- Cross-region replication, analytics and search
- Automated filing, purchasing, payment or irreversible action

## Contract decision

No shared contract was changed. The current `company.v1` contract omits `tenant_id`, although the master requirements demand tenant and company scope on every material record. The implementation keeps `tenant_id` in the internal `Scope` and snapshot boundary while `Company.to_contract()` remains compatible with the existing schema's `additionalProperties: false`. A future nervous-system contract review should add tenant scope consistently in a backward-compatible v2 or coordinated v1 amendment rather than this worker changing one shared contract in isolation.

## Tests

Run:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Coverage includes tenant/company read and mutation isolation, lifecycle legality, optimistic conflicts, record and company history, provenance round-trip, knowledge classification, owner/jurisdiction invalidation hooks, deterministic/redacted snapshots, migration idempotency, Billy Bob fixture completeness and shared-contract compatibility.
