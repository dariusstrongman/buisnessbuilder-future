# Repository Ownership Map

Audit basis: repository default branches and current GitHub history inspected 2026-09-13. Ventures context is authoritative when repo prose conflicts with the locked product direction.

## Executive map

| Repository | Current contents | Status | Final responsibility |
|---|---|---|---|
| `dariusstrongman/Stromation` | Large private Python system: Sol/company governance, runtime loop, customer and venture machinery, database migrations, infrastructure material, website order/project/payment/delivery paths, premium website engine, quality gates and extensive tests. | **Active.** Current website-engine Phase 1E work is landing on `main`; latest inspected head began `f2333438`. | Own Sol's internal runtime and the premium Website Capability implementation. Export a stable adapter boundary only. |
| `dariusstrongman/website-builder` | Static GitHub Pages marketing/service site with 31 routes, original demo studies, browser-local workspace, intake/checkout UI and a direct endpoint link to Stromation. No LLM engine. | **Active product/demo surface, but architecturally legacy for the future unified UI.** Latest inspected `main` began `c2eafeda`. | Preserve as the standalone Website Builder marketing surface until migration. Do not evolve it into the master platform or duplicate its demos in core. |
| `dariusstrongman/buisnessbuilder-future` | Product definition, Billy Bob Company Brain example and interactive Build Room prototype. | **Active future integration layer.** Architecture/docs only at audit start. | Own master contracts, Company Brain, core orchestration, setup/admin, product AI workforce contracts, cross-capability verification and unified customer UI. |

## Stromation detail

Active, reusable internals already present:

- Direction research and industry-excellence binding.
- Divergent direction generation and compiled design blueprints.
- Study builders, deterministic rendering/checks, repair and re-review.
- Independent ranking, digest-bound direction approval and preview flows.
- Proof-slice then full production build.
- Visual review, revision, customer approval and static package delivery.
- Website progress, authority, budget, project and payment modules.
- Company governance, decision ledger, spending enforcement and audit patterns.
- Hundreds of website/runtime/company tests and CI workflows.

Current truth and limitation:

- Phase 1E is active and changing quickly.
- `packaged_not_deployed` is the present terminal engine state; deployment is a separate concern.
- Paid creative stages and a complete real paid journey remain unproven.
- Quality claims remain unproven until the planned rerun completes.

Do not rebuild outside Stromation:

- Direction/study/ranking logic.
- Website validators, score floors, repair loops or worker prompts.
- Proof/full-build sequencing.
- Website-specific durable job files or internal state machine.
- Package integrity checks.
- Stromation's Sol org, constitution, private capability registry or runtime loop.

## website-builder detail

Active surface features:

- Public marketing, pricing, quality and case-study routes.
- Premium demo pages and motion lab.
- Browser-local first-look/workspace journey.
- Website brief intake and connection to Stromation's public intake endpoint.
- Static-site verification scripts and GitHub Pages deployment.

Legacy/experimental boundaries:

- It is not the website engine.
- Its browser-local project model is not the Company Brain.
- Its milestone/status presentation is a product prototype, not canonical execution truth.
- Its package prices and copy are prototype positioning, not proven business-builder pricing.
- Direct coupling to a Stromation endpoint is acceptable for the standalone surface but must not become the new master boundary.

Preserve, then migrate selectively:

- Preserve visual research, case studies, public copy evidence and standalone launch path.
- Reuse interaction lessons, not source files, in the future unified Skin.
- Do not add Company Brain, AI workforce, admin/setup or master verification logic here.

## buisnessbuilder-future detail

Own now:

- Architecture and contracts.
- Billy Bob reference fixtures.
- Company Brain canonical model.
- Capability registry and provider-neutral jobs/events.
- Cross-capability permissions, approvals, budgets and audit.
- Setup/admin workflow definitions.
- FounderAction queue.
- Verification catalog and readiness projection.
- Unified customer-facing Build Room and handoff.

Do not own:

- Website generation internals.
- Stromation/Sol private implementation.
- A second copy of Website Builder marketing pages.
- Live infrastructure configuration during the architecture phase.

## Overlap and resolution

| Overlap | Repositories | Resolution |
|---|---|---|
| Customer project/milestones | all three | Canonical company/job/readiness state moves to business-builder. Stromation exposes coarse capability progress. website-builder remains a temporary presenter. |
| Approvals | Stromation + business-builder | Stromation keeps internal website approval enforcement. Business-builder owns canonical founder approvals and passes a signed opaque approval reference. Never share tables. |
| Budgets | Stromation + business-builder | Business-builder reserves capability budget; Stromation enforces its local ceiling. Settlement reconciles provider usage. Both must pass; neither can raise the other's ceiling. |
| Verification | Stromation + business-builder | Stromation verifies website construction quality/package integrity. Business-builder verifies customer readiness, ownership, live workflows and freshness. |
| AI workforce | Stromation + business-builder | Stromation owns Sol and internal workers. Business-builder owns customer product-role contracts. Adapters may invoke capabilities, never import the org. |
| UI | website-builder + business-builder | website-builder remains standalone until the unified Skin replaces it. No shared mutable browser storage or copied state machines. |
| Company/customer records | Stromation + business-builder | Business-builder is master product record. Stromation receives minimum tenant-bound request fields and returns opaque references. |

