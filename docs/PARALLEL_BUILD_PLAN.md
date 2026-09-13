# Parallel Build Plan

## Safe work now

All work remains in `buisnessbuilder-future` and begins with contracts/fixtures/tests, not production integrations.

| Worker | Owns | Deliverables | Must not do |
|---|---|---|---|
| W1 Company Brain | Head | entity model, provenance/confidence, lifecycle, Billy fixture, migrations as design only | copy Stromation company/state tables; store secrets |
| W2 Nervous System | events, approvals, permissions, budget, audit | contract validators, idempotency rules, append-only event/audit fixtures, policy decisions | connect live queues, spend money, reuse Sol's private authority code |
| W3 Core Orchestration | Torso | provider-neutral capability registry, job state projection, adapter interface, fake provider | implement website engine or generic multi-agent runtime |
| W4 Verification | Left leg | verification catalog, evidence rules, invalidation graph, Ready/Fully Set evaluator | copy website quality gates; call external authorities |
| W5 Setup/Admin | Left arm | archetype requirements, Do It/Guide/Skip state machine, FounderAction templates, location-aware obligation fixtures | file entities, buy domains, select insurance or give professional advice |
| W6 Product UI | Skin | Billy Bob Build Room using canonical mock events, evidence, approvals, founder actions, handoff | read Stromation storage/API directly; copy website-builder localStorage state |
| W7 Website Adapter | Right-arm socket | contract fixtures, translation table, fake adapter and conformance suite | implement real Stromation calls before Phase 1E; alter either source repo |
| W8 AI Workforce Product | Right leg | customer-facing role definitions, capability grants, denied actions, escalation/budget fixtures | rebuild Sol or let agents sign/spend/file/hire |

## Dependencies

- W1 and W2 publish stable IDs/status/provenance primitives first.
- W3, W4, W5 and W8 consume only released contract versions.
- W6 consumes canonical events and projections from mocks.
- W7 can build fixtures and a fake provider now; real mapping waits for Phase 1E.

## Worker rules

1. One subsystem directory per worker; contract changes require a small contract PR first.
2. No worker edits another subsystem's files without explicit ownership transfer.
3. Each PR includes Billy Bob fixture coverage and a boundary test.
4. Cross-subsystem calls use interfaces/contracts, not internal imports.
5. New enum values must be backward-compatible or bump the schema major version.
6. No provider credentials, live URLs, account identifiers or customer data in fixtures.
7. No production code until the architecture skeleton is accepted.

## Recommended assembly order

1. Merge master architecture, ownership map and v1 contracts.
2. Build Company Brain persistence design and nervous-system append-only model.
3. Build core job/capability orchestration against fake capabilities.
4. Build verification/readiness and FounderAction projections.
5. Build setup/admin archetype workflows.
6. Build product UI against Billy Bob canonical fixtures.
7. After Phase 1E proof, implement Website Capability adapter and conformance tests.
8. Run full Billy Bob journey with a recorded Stromation package; fix contract gaps.
9. Add customer-facing AI workforce pilot behind denied-by-default permissions.
10. Only after offline proof, design live infrastructure and migration from the standalone Website Builder surface.

## Duplication risks

| Risk | Failure mode | Prevention |
|---|---|---|
| Second website engine | Workers recreate research, design or verification logic | Website Capability is an external provider boundary; no generator work in future repo. |
| Second Sol | Generic orchestrator duplicates Stromation org/runtime | Core coordinates capabilities/jobs only; customer AI roles are policy contracts. |
| Two Company Brains | Stromation customer records and new graph drift | Business-builder is master product record; adapter sends minimum snapshots and stores provider refs. |
| Two approval truths | UI says approved while engine receipt differs | Digest-bound canonical approval and explicit provider acknowledgment. |
| Two budgets | Provider spends beyond master reservation | Both local and master ceilings must pass; settlement reconciliation required. |
| Verification inflation | Engine QA is presented as customer-ready | Separate provider verification from business readiness. |
| UI state as truth | localStorage milestones masquerade as execution | UI is a projection of canonical events; demo state is labeled fixture data. |
| Contract overfitting | New platform depends on Phase 1E internals | Coarse milestones, opaque provider refs and recorded adapter fixtures. |

## Phase checkpoints

- **A: Skeleton accepted.** Ownership and schemas merged; no code.
- **B: Offline spine.** Company Brain, events, jobs, approvals, budgets and audit run with fixtures.
- **C: Billy Bob product.** UI and readiness operate entirely from canonical fake-provider data.
- **D: Website socket proven.** Phase 1E adapter passes conformance with a recorded real package.
- **E: Controlled pilot.** One concierge customer, founder-owned accounts, zero autonomous irreversible actions.

