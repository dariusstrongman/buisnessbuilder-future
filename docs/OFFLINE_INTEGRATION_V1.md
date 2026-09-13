# Offline Business Builder integration v1

## Result

The first offline Business Builder body is coherently assembled from the real Company Brain, Runtime, and Verification implementations. Only `fake.website.build` remains fake. No production system, credential, provider, or Stromation repository is involved.

## Package structure

```text
src/businessbuilder/
  company_brain/     canonical company state and SQLite dev persistence
  runtime/           events, jobs, approvals, budgets, audit, capabilities
  verification/      verification state, evidence, invalidation, readiness
  integration/       narrow service-to-service adapters
  fixtures/          one canonical integrated Billy Bob fixture
```

The former `businessbuilder_runtime` package moved to `businessbuilder.runtime`. The root `pyproject.toml` installs one project, discovers packages under `src`, includes Company Brain migrations, and discovers all tests.

## Ownership and boundaries

| Data or decision | Owner | Integration access |
|---|---|---|
| Company state, history, dependency notices | Company Brain | `CompanyBrainService`, `Scope`, `compact_snapshot()` |
| Jobs, events, approvals, budgets, spend, audit, retries | Runtime | Runtime services and ports |
| Verification, evidence, stale state, Ready/Fully Set | Verification | `VerificationService`, event handler, evaluator |
| Website build output | Fake capability | `fake.website.build` only |

`CompanyBrainRuntimeAdapter` implements Runtime's read ports through Company Brain services, never SQLite.

`CompanyBrainVerificationAdapter` is the explicit readiness projection for approved offers, service areas, owners, requirements, obligations, and founder actions. Founder actions remain excluded from `compact_snapshot()` as designed.

`RuntimeVerificationAdapter` implements `VerificationPort`. A completed job creates deterministic, idempotent Proposed records through the real service. Repetition does not duplicate records or dependencies.

`VerificationInvalidationAdapter` converts Company Brain notices into Verification's existing dependency-change event. Verification alone determines stale state and readiness.

## Canonical Billy Bob

The integration fixture calls Company Brain's `load_billy_bob()` as its source of truth: Billy Bob Lawn Care in Texas, one founder, a 12-mile service area, mow/edge/blow, regulated and complex work excluded, limited capacity, founder-owned equipment/accounts, and a founder-only domain action.

Worker 2's old fixture path is only a compatibility re-export. Verification unit fixtures share the same tenant, company, and owner identifiers and are state-machine inputs, not another integration source of truth.

## End-to-end proof

1. Company Brain creates Billy Bob and Runtime records `company.created`.
2. Runtime creates a website job in `waiting_approval`.
3. Founder `party_billy` grants the digest-bound founder-only approval.
4. Runtime reserves 400 minor units against a 1,000-unit local budget.
5. `fake.website.build` returns one package artifact for 271 minor units.
6. Runtime settles 271, releases 129, and leaves zero reserved.
7. Runtime records capability progress, completion, job completion, and `verification.requested`.
8. The adapter creates ten Proposed records through `VerificationService`.
9. Deterministic evidence advances customer-path records through Proposed → Executed → Tested → Verified.
10. Readiness changes from false/false to true/false.
11. The founder action is versioned to verified and selected admin requirements follow the same real state machine.
12. Readiness changes to true/true.
13. Company Brain versions the service area from 12 miles to 10 miles.
14. Four geography-bound records become stale/Executed; readiness returns to false/false.

## Runtime trace

```text
company.created
job.requested
founder.approved
capability.requested
job.started
capability.progress × 6
capability.completed
job.completed
verification.requested
```

## Safety properties proved

- Tenant/company mismatch fails closed across adapters.
- Duplicate Runtime verification requests do not duplicate side effects.
- Stale Company Brain versions and invalid Verification transitions are rejected.
- Budget ceilings and founder-only approval remain enforced.
- Company Brain dependency invalidation propagates to Verification and readiness.
- No subsystem directly reads another subsystem's SQLite tables.

## Tenant model

All service/repository operations require `(tenant_id, company_id)`. Additive v2 contracts make tenant scope explicit at nervous-system boundaries while v1 remains compatible. See `CONTRACT_VERSIONING.md`.

## Replacing the fake capability

Claude Phase 1E in `dariusstrongman/Stromation` must later provide an adapter implementing the capability interface and website capability v2. It replaces only the fake registration; Runtime approvals, budgets, idempotency, artifacts, and Verification requests remain.

Blocked on Phase 1E:

- real research, design, build, revision, ranking, rendering, and website QA
- real artifacts, progress, costs, failure mapping, cancellation, and deployment observations
- conversion of those observations into real verification evidence

Not blocked on Phase 1E:

- setup/admin work
- Build Room UI against offline boundaries
- AI workforce contract design

## Intentionally offline/dev-only

- synchronous in-process event bus
- SQLite Runtime and Company Brain repositories
- in-memory/JSON Verification repository
- deterministic evidence
- fake website execution and spend
- no cloud, production deployment, credentials, domains, or customer data

## Test command

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test*.py' -v
```
