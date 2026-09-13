# Build Room UI

Status: offline product-skin proof. No production services, credentials, provider calls or mutation commands.

## Purpose

The Build Room makes the platform's real assembly state legible to a founder. It shows discrete work, dependencies, bounded costs, approvals, founder-only actions, evidence, blockers, readiness and customer-owned handoff. AI activity is never used as decorative progress.

The prototype is in `prototype/`. Open `prototype/index.html` directly; all data and assets are local. The Billy Bob snapshot is intentionally not a final-success screenshot: the website package is complete, but customer-owned deployment and monitoring evidence remain open. This makes the critical distinction visible:

- `package_ready` means the website capability returned an artifact.
- **Ready** comes only from the Verification projection after the deployed customer path is independently verified.
- **Fully Set** means Ready plus selected setup obligations, monitoring and handoff.

## Boundary

`businessbuilder.build_room.adapters` validates canonical v2 mappings or supported Runtime/Verification domain records, then converts them into presentation-only rows. `project_build_room` accepts only those rows plus `ScopedEnvelope` values for source, readiness, budget and handoff, and returns `build-room.projection.v1`. It performs no I/O and exposes no commands. Cross-tenant records or envelopes, invalid blocker values, inexact money, inconsistent readiness, missing scoped evidence and budget overflow fail closed; an open critical blocker forces Ready and Fully Set false.

The browser renders one generated, customer-safe projection from `prototype/data.js`. That file is serialized from the Python fixture, and an exact parity test prevents it from drifting. It does not:

- read or write `localStorage` or `sessionStorage`;
- call Stromation, the website engine or any live API;
- infer approvals, spend authority, deployment or verification;
- contain business-builder state machines;
- approve, purchase, execute, publish or mutate canonical state.

When a real product shell is introduced, its API should return this projection (or a compatible version) from server-side canonical stores. UI actions must submit explicit commands through the nervous-system boundary and then wait for new canonical records; optimistic UI must never become execution truth.

## Projection fields

| Area | Source concept | Presentation rule |
|---|---|---|
| Company | Company Brain snapshot | Identity and approved operating scope only |
| Work | canonical Jobs + Verifications | one card per discrete record, with owner/status/dependencies/cost/evidence |
| Budget | Budget/Spend projection | ceiling, reserved, settled and available; ceiling is not a target |
| Approvals | digest-bound Approval records | show requested/granted/denied/revoked/expired/superseded state, approver role and subject receipt; never infer |
| Evidence | scoped Verification evidence | show artifact, evidence type, capture time and declared test result |
| Founder actions | FounderAction records | explain reason, risk and evidence; browser cannot complete them |
| Blockers | Verification/readiness blockers | critical gates remain prominent and fail closed |
| Activity | customer-visible canonical Events | sequence-ordered; internal/private payloads excluded upstream |
| Ready/Fully Set | Verification evaluator | display only, never recompute success in the browser |
| Handoff | canonical export/handoff projection | customer ownership and lock reasons remain visible |

## Accessibility and responsive behavior

The prototype uses semantic headings, landmark navigation, a skip link, accessible labels, a native dialog, keyboard-focus styles, reduced-motion support and a real progressbar. Desktop uses a persistent section rail; narrow screens use a bottom navigation bar and single-column cards. No external fonts or scripts are required.

## Tests

`tests/build_room/` covers v2 contract validity, domain adapters, deterministic serialization, exact browser-data parity, scoped-envelope isolation, exact-integer budgets, blocker validation, evidence integrity, readiness invariants, the package-ready boundary, discrete dependencies, approval-state rendering, offline-only assets, JavaScript parsing, accessibility-oriented markup and the absence of browser-owned canonical storage.
