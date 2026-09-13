# Business Builder Build Status

Last verified: 2026-09-13T02:15:32Z

GitHub refs, commits, checks, and repository contents are execution truth. This file is the durable coordination summary and must be corrected whenever GitHub disagrees.

## Program posture

- Current checkpoint: **offline spine preserved; first independent-review blockers corrected on `fix/offline-readiness-proof`; follow-up review is next**.
- Production posture: **no production integration or infrastructure work is authorized**.
- Real Website Capability: **held** until Claude explicitly declares the Stromation website engine ready for integration.
- Current external capability: `fake.website.build` is intentionally the only fake external capability in the integrated offline journey.
- Ownership: Company Brain is canonical product state. Stromation owns Sol and website-engine internals. `website-builder` remains a transitional standalone surface.

## Canonical repository state

| Repository | Default branch / head | Active branch / head | Responsibility | Coordination decision |
|---|---|---|---|---|
| `dariusstrongman/Stromation` | `main` / `1d0dedb31642a04ff9dbaa01c747e630990555fc` | Claude Phase 1F; latest observed remediation `fix/direction-failure-isolation` / `6415f7f07b4922c3748a7fdd7a6ca0faa4dba5e7` | Sol runtime and premium website engine | Hold real adapter. Do not copy internals or infer readiness from deployment status. |
| `dariusstrongman/website-builder` | `main` / `c2eafeda43f89416fb135248151422de2799be04` | Draft PR #1 `feat/redesign-intake` / `5187b36ade62219cbe40e08005bd3a6655fa4b5c` | Transitional standalone customer/marketing surface | Preserve; do not make it the unified platform or generation engine. |
| `dariusstrongman/buisnessbuilder-future` | `main` / `b5a44761ce83f0032684f825829123380573536f` | `integration/offline-billy-bob-v1` / `fbaee89ae3cd504602f69e5a04d4720248fc515f` | Unified Business Builder platform | Integration branch is the base for integration-dependent work; main is intentionally unchanged. |

## Integration preservation record

Worker 4's original local integration commit was:

- commit: `2c8549c6579409d6b114bfb2a8436d4ec23ecfca`
- tree: `11b57ddc0e768cfbc7e1cdb7a7cd750383754c80`
- branch: `integration/offline-billy-bob-v1`

The worker could not push its local-only parent chain. The exact final tree was therefore published directly on top of GitHub `main` as:

- published commit: `fbaee89ae3cd504602f69e5a04d4720248fc515f`
- published tree: `11b57ddc0e768cfbc7e1cdb7a7cd750383754c80`
- published branch: `integration/offline-billy-bob-v1`

The content is byte-identical at the Git tree level. Only the commit SHA and ancestry differ. `main` was not modified.

## Subsystem ledger

| Subsystem | Owner | Repository / branch | Commit | Status | Dependencies | Blockers | Tests / evidence | Integration readiness | Next action |
|---|---|---|---|---|---|---|---|---|---|
| Architecture + v1 contracts | Architecture | `buisnessbuilder-future/main` | `a9f41060...` and ancestors | Released | None | Contract changes require explicit review | Parsed by compatibility tests | Ready | Preserve v1 compatibility |
| Company Brain / Head | W1 | Integrated branch | Source `b5a44761...`; published integration `fbaee89a...` | Integrated | v1/v2 contracts | None found in offline scope | Included in 89-test combined suite | Integrated offline | Review public adapter only; no internal coupling |
| Core Runtime + nervous system / Torso | W2 | Integrated branch | Source `2148e04f...`; published integration `fbaee89a...` | Integrated and repackaged under `businessbuilder.runtime` | Company Brain reader and Verification ports | Synchronous SQLite/in-process design is dev-only | Included in 89-test combined suite | Integrated offline | Independent diff/API review |
| Verification / Left leg | W3 | Integrated branch | Source `1c6820c...`; published integration `fbaee89a...` | Integrated under `businessbuilder.verification` | Company Brain invalidations; Runtime verification requests | JSON/in-memory repository is dev-only | Included in 89-test combined suite | Integrated offline | Independent diff/readiness review |
| Offline Integration | targeted fix worker | `fix/offline-readiness-proof` based on published `2129e130...` | Local fix commit reported by worker; base integration content `fbaee89a...` | **Acceptance blockers corrected locally; main unchanged** | Company Brain + Runtime + Verification | Follow-up independent review and hosted CI still required | 97/97 canonical tests pass locally; focused package-ready, dependency-retry, tenant-evidence, approval-binding, and scenario-set regressions added | Ready for follow-up review, not merge yet | Inspect exact fix commit, then accept/fix/hold |
| Additive tenant-aware v2 contracts | W4 integration | Integrated branch | `fbaee89a...` | Added without removing v1 | v1 semantics | Must be reviewed as a deliberate public contract release; future real Website adapter must support v2 | Four v2 compatibility tests pass; all company-bound public objects require `tenant_id`; capability definition remains global | Review required | Freeze only after independent schema-diff review |
| Setup/Admin / Left arm | W5 | `worker5/setup-admin` | Pending; worker paused during preservation | Restarting from published integration base | Integrated public boundaries + released contracts | No filing, purchasing, insurance selection, professional advice, live providers, or production | Required: Billy Bob, tenant isolation, Do It/Guide/Skip history, founder/external authority gates | Pending | Rebase/restart on `fbaee89a...`; isolated subsystem only |
| AI Workforce Product / Right leg | W8 | `worker8/ai-workforce-product` | Pending; worker paused during preservation | Restarting from published integration base | Integrated Runtime policy concepts + released contracts | Must not duplicate Sol, agent runtime, provider execution, or model routing | Required: adversarial permissions, denied actions, budgets, escalation, tenant isolation, Billy Bob roles | Pending | Rebase/restart on `fbaee89a...`; policy/domain layer only |
| Product UI / Skin | W6 future | Not started | None | Waiting | Stable integrated facade and canonical fixture/event stream | Starting from internals would hard-code temporary shapes | Prototype is design evidence, not execution truth | Not ready | Define UI projection contract after integration review |
| Website engine / Right arm | Claude Code | `Stromation` Phase 1F | Main `1d0dedb...`; active observed fix `6415f7f...` | Active | Stromation internals only | Real proof and explicit readiness declaration outstanding | Main has prior existing deployment status; Phase 1F is separate and ongoing | **Hold** | Claude completes Phase 1F and explicitly declares adapter-ready |
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
4. Run the 97-test command from a clean checkout and add hosted CI.
5. Confirm setup/admin and AI-workforce branches touch only their owned directories and public seams.
6. Open a reviewed PR; do not merge merely because tests pass.

## Test command

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test*.py' -v
```
