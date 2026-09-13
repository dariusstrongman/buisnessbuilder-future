# Business Builder Build Status

Last verified: 2026-09-13T02:30:21Z

GitHub refs, commits, checks, and repository contents are execution truth. This file is the durable coordination summary and must be corrected whenever GitHub disagrees.

## Program posture

- Current checkpoint: **offline spine and literal Worker 4 history are durably preserved; reviewed readiness corrections are integrated; the next isolated build wave is active**.
- Production posture: **no production integration or infrastructure work is authorized**.
- Real Website Capability: **held** until Claude explicitly declares the Stromation website engine ready for integration.
- Current external capability: `fake.website.build` is intentionally the only fake external capability in the integrated offline journey.
- Ownership: Company Brain is canonical product state. Stromation owns Sol and website-engine internals. `website-builder` remains a transitional standalone surface.

## Canonical repository state

| Repository | Default branch / head | Active branch / head | Responsibility | Coordination decision |
|---|---|---|---|---|
| `dariusstrongman/Stromation` | `main` / `cc10d9ea29bea02d194e2154af276225274b9570` | Claude Phase 1F; PR #121 `e699491c...`, #122 `d8a9fa5f...`, #123 `cc708b00...` | Sol runtime and premium website engine | PR #120 merged with green CI. Hold real adapter: remaining stacked changes and fresh premium-output proof are outstanding. |
| `dariusstrongman/website-builder` | `main` / `c2eafeda43f89416fb135248151422de2799be04` | Draft PR #1 `feat/redesign-intake` / `5187b36ade62219cbe40e08005bd3a6655fa4b5c` | Transitional standalone customer/marketing surface | Preserve; do not make it the unified platform or generation engine. |
| `dariusstrongman/buisnessbuilder-future` | `main` / `b5a44761ce83f0032684f825829123380573536f` | `integration/offline-billy-bob-v1`; accepted code checkpoint `988058981d8dde953d6c2b3b8f993015543b4a0e`, followed only by coordination status/CI commits | Unified Business Builder platform | Reviewed base for W5/W6/W8; main is intentionally unchanged. Resolve the live branch ref in GitHub rather than treating this self-referential status file as its own head. |

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
| Offline Integration | W4 + coordinator review | `integration/offline-billy-bob-v1` | Accepted code checkpoint `988058981d8dde953d6c2b3b8f993015543b4a0e`; CI commit `b8b76f62...` | **Durable and accepted as next-wave base; main unchanged** | Company Brain + Runtime + Verification | Final contract/adapter review and PR still required before main | Fresh remote clone 99/99; hosted `Offline Business Builder CI` run #1 passed; package-ready and fresh-retest regressions pass | Safe base for isolated offline work; not yet main-ready | Complete contract/adapter review before PR to main |
| Additive tenant-aware v2 contracts | W4 integration | Integrated branch | `fbaee89a...` | Added without removing v1 | v1 semantics | Must be reviewed as a deliberate public contract release; future real Website adapter must support v2 | Four v2 compatibility tests pass; all company-bound public objects require `tenant_id`; capability definition remains global | Review required | Freeze only after independent schema-diff review |
| Setup/Admin / Left arm | W5 | `worker5/setup-admin` | Base `98805898...`; implementation active | Active, isolated | Integrated public boundaries + released contracts | No filing, purchasing, insurance selection, professional advice, live providers, or production | Required: Billy Bob, tenant isolation, Do It/Guide/Skip history, founder/external authority gates | Pending worker review | Implement only owned setup/admin package and tests |
| AI Workforce Product / Right leg | W8 | `worker8/ai-workforce-product` | Base `98805898...`; implementation active | Active, isolated | Integrated Runtime policy concepts + released contracts | Must not duplicate Sol, agent runtime, provider execution, or model routing | Required: adversarial permissions, denied actions, budgets, escalation, tenant isolation, Billy Bob roles | Pending worker review | Implement policy/domain layer only |
| Product UI / Skin | W6 | `worker6/build-room-ui` | Base `98805898...`; implementation active | Active, isolated | Canonical read-only projection and fixture/event stream | Must not create execution truth, call Stromation directly, or use localStorage as canonical state | Required: deterministic projection, responsive/accessibility smoke checks, Billy Bob build room | Pending worker review | Build only projection/UI boundary and prototype |
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
4. Keep hosted CI green; run #1 passed all 99 tests from commit `b8b76f62...`.
5. Confirm setup/admin and AI-workforce branches touch only their owned directories and public seams.
6. Open a reviewed PR; do not merge merely because tests pass.

## Test command

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test*.py' -v
```
