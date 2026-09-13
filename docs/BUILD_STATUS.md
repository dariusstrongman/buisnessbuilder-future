# Business Builder Build Status

Last verified: 2026-09-13T03:06:30Z

GitHub refs, commits, checks, and repository contents are execution truth. This file is the durable coordination summary and must be corrected whenever GitHub disagrees.

## Program posture

- Current checkpoint: **offline spine, Setup/Admin, AI Workforce policy, and Build Room are independently reviewed and integrated on the offline branch; combined CI is green**.
- Production posture: **no production integration or infrastructure work is authorized**.
- Real Website Capability: **held** until Claude explicitly declares the Stromation website engine ready for integration.
- Current external capability: `fake.website.build` is intentionally the only fake external capability in the integrated offline journey.
- Ownership: Company Brain is canonical product state. Stromation owns Sol and website-engine internals. `website-builder` remains a transitional standalone surface.

## Canonical repository state

| Repository | Default branch / head | Active branch / head | Responsibility | Coordination decision |
|---|---|---|---|---|
| `dariusstrongman/Stromation` | `main` / `cc10d9ea29bea02d194e2154af276225274b9570` | Claude Phase 1F; PR #121 `e699491c...`, #122 `d8a9fa5f...`, #123 `cc708b00...` | Sol runtime and premium website engine | PR #120 merged with green CI. Hold real adapter: remaining stacked changes and fresh premium-output proof are outstanding. |
| `dariusstrongman/website-builder` | `main` / `c2eafeda43f89416fb135248151422de2799be04` | Draft PR #1 `feat/redesign-intake` / `5187b36ade62219cbe40e08005bd3a6655fa4b5c` | Transitional standalone customer/marketing surface | Preserve; do not make it the unified platform or generation engine. |
| `dariusstrongman/buisnessbuilder-future` | `main` / `b5a44761ce83f0032684f825829123380573536f` | `integration/offline-billy-bob-v1`; accepted combined code checkpoint `1f5e25f9048c400d50faada3327cb19bc53c8f6a`, followed only by status commits | Unified Business Builder platform | Combined offline product branch is green; main is intentionally unchanged. Resolve the live branch ref in GitHub rather than treating this self-referential status file as its own head. |

## Integration preservation record

Worker 4's original local integration commit was:

- commit: `2c8549c6579409d6b114bfb2a8436d4ec23ecfca`
- tree: `11b57ddc0e768cfbc7e1cdb7a7cd750383754c80`
- branch: `integration/offline-billy-bob-v1`

The worker could not push its local-only parent chain. The exact final tree was therefore published directly on top of GitHub `main` as:

- published commit: `fbaee89ae3cd504602f69e5a04d4720248fc515f`
- published tree: `11b57ddc0e768cfbc7e1cdb7a7cd750383754c80`
- published branch: `integration/offline-billy-bob-v1`

The content is byte-identical at the Git tree level. Only the commit SHA and ancestry differ. The literal original commit and parent chain are also stored in a verified Git bundle on `archive/offline-billy-bob-v1-original-bundle` at `archive/offline-billy-bob-v1-original.bundle.b64`; its uploaded bytes exactly match the locally verified bundle and restore to `2c8549c...`. `main` was not modified.

## Subsystem ledger

| Subsystem | Owner | Repository / branch | Commit | Status | Dependencies | Blockers | Tests / evidence | Integration readiness | Next action |
|---|---|---|---|---|---|---|---|---|---|
| Architecture + v1 contracts | Architecture | `buisnessbuilder-future/main` | `a9f41060...` and ancestors | Released | None | Contract changes require explicit review | Parsed by compatibility tests | Ready | Preserve v1 compatibility |
| Company Brain / Head | W1 | Integrated branch | Source `b5a44761...`; accepted integration `98805898...` | Integrated | v1/v2 contracts | None found in offline scope | Included in 99-test combined suite | Integrated offline | Review public adapter only; no internal coupling |
| Core Runtime + nervous system / Torso | W2 | Integrated branch | Source `2148e04f...`; accepted integration `98805898...` | Integrated and repackaged under `businessbuilder.runtime` | Company Brain reader and Verification ports | Synchronous SQLite/in-process design is dev-only | Included in 99-test combined suite | Integrated offline | Independent diff/API review |
| Verification / Left leg | W3 + targeted fix | Integrated branch | Source `1c6820c...`; accepted integration `98805898...` | Integrated with corrected readiness/retest gates | Company Brain invalidations; Runtime verification requests | JSON/in-memory repository is dev-only | Fresh remote clone: 99/99; recycled pre-failure/invalidation evidence rejected | Integrated offline | Keep verification as sole readiness authority |
| Offline Integration | W4 + coordinator review | `integration/offline-billy-bob-v1` | Combined code checkpoint `1f5e25f9048c400d50faada3327cb19bc53c8f6a` | **Durable, combined, and green; main unchanged** | Company Brain + Runtime + Verification + W5/W6/W8 | Final v2 contract/adapter review still required before main | Fresh remote clone: 184 passed + 80 subtests; hosted CI run `34734633923` passed; Node checks passed | Integrated offline; not yet main-ready | Complete remaining contract/adapter review, then open reviewed PR to main |
| Additive tenant-aware v2 contracts | W4 integration | Integrated branch | `fbaee89a...` | Added without removing v1 | v1 semantics | Must be reviewed as a deliberate public contract release; future real Website adapter must support v2 | Four v2 compatibility tests pass; all company-bound public objects require `tenant_id`; capability definition remains global | Review required | Freeze only after independent schema-diff review |
| Setup/Admin / Left arm | W5 + independent reviewers | `worker5/setup-admin` → PR #2 → integration | Published `e051adf3...`; merge `db35170a...` | Accepted and integrated offline | Integrated public boundaries + released contracts | Live filing, purchase, professional advice and provider execution remain forbidden | Branch pytest: 136 + 4 subtests; confused-deputy, receipt-binding, idempotency and append-only history probes pass | Integrated offline | Connect to shared projections only through reviewed public seams |
| AI Workforce Product / Right leg | W8 + independent reviewers | `worker8/ai-workforce-product` → PR #3 → integration | Published `b99612ad...`; merge `471595ff...` | Accepted and integrated offline | Integrated Runtime policy concepts + released contracts | No Sol/runtime/provider/model execution; real operational adapters remain future work | Branch pytest: 121 + 25 subtests; lifecycle epoch, cached-ALLOW and founder-bound vocabulary probes pass | Integrated offline | Keep policy layer separate from Sol and provider execution |
| Product UI / Skin | W6 + independent reviewers | `worker6/build-room-ui` → PR #1 → integration | Published `02259d6c...`; merge `1f5e25f9...` | Accepted and integrated offline | Canonical read-only projections and fixture/event stream | Offline UI only; no live product API or production deploy | Branch pytest: 125 + 59 subtests; contract adapters, parity, Node syntax and accessibility-oriented checks pass | Integrated offline | Use as offline product proof; design live API separately after core acceptance |
| Website engine / Right arm | Claude Code | `Stromation` Phase 1F | Main `cc10d9ea...`; active PRs #121-#123 | Active | Stromation internals only | Remaining stacked remediations and explicit reliable premium-output proof outstanding | PR #120 merged green; #121 green/mergeable; #122-#123 green but currently non-mergeable stacked changes | **Hold** | Monitor only; Claude must explicitly declare adapter-ready |
| Website Capability adapter / Right-arm socket | W7 future | Not started | None | Held except offline fake | Stable contracts + explicit Claude readiness | Exact real mappings, retry/cancellation, evidence, and deployment observations unresolved | Fake capability proves only the platform seam | Not ready | Do not integrate the real engine yet |
| Standalone Website Builder | Existing owner | `website-builder/main` | `c2eafeda...` | Stable transitional surface | Existing standalone flow | Browser-rendered visual QA not performed in current audit | Local release verification passed: 31 pages, 64 Node tests, link/copy/privacy/interaction checks | Preserve only | No unified-platform logic here |
| Live infrastructure / production migration | Future | None | None | Explicitly deferred | Accepted offline system + Darius authorization | Production approval required | None | Not ready | Do not start |

## Verified offline journey

The preserved integrated code proves:

1. the real Company Brain supplies canonical Billy Bob state;
2. Runtime creates a tenant/company-scoped website job and enforces founder-only digest-bound approval;
3. Runtime reserves 400 minor units, the fake website capability settles 271, and 129 is released;
4. Runtime emits canonical progress, completion, audit, and `verification.requested` events;
5. the real Runtime-to-Verification adapter creates deterministic Proposed records without duplicate side effects;
6. the `package_ready` checkpoint leaves all verification Proposed and readiness `false/false`, with no deployment or live inference;
7. a distinct deterministic offline deployment/QA phase supplies explicitly non-live evidence for deployment, HTTPS, links, mobile, and the remaining customer path;
8. Verification then moves readiness to Ready `true` / Fully Set `false`, then `true/true` after admin completion;
9. a Company Brain service-area version change invalidates geography-bound verification and returns readiness to `false/false`;
10. tenant/company mismatches, same-company cross-tenant evidence, unapproved offer/market facts, partial dependency writes, incomplete scenario sets, stale writes, invalid transitions, budget overrun, and unauthorized approval fail closed.

## Review gates before merging integration to main

1. Compare every v2 schema to v1 and confirm the only semantic change is required `tenant_id` plus version identifiers.
2. Inspect `CompanyBrainRuntimeAdapter`, `CompanyBrainVerificationAdapter`, `RuntimeVerificationAdapter`, and `VerificationInvalidationAdapter` for internal imports or duplicated authority.
3. Re-review the corrected proof that `package_ready` cannot become live, Ready, or Fully Set without a distinct deployment/QA evidence phase.
4. Keep hosted CI green; combined run `34734633923` passed and a fresh remote clone passed 184 tests plus 80 subtests.
5. Preserve the reviewed ownership boundaries when wiring Setup/Admin, AI Workforce and Build Room into future live adapters.
6. Open a reviewed PR; do not merge merely because tests pass.

## Test command

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test*.py' -v
```
