# Integration Plan

## Exact repository boundaries

### Business builder → Website Capability

The business builder sends a versioned request containing:

- canonical `company_id`, `job_id` and `correlation_id`;
- approved business/website brief snapshot and its digest;
- public-claim allowlist and prohibited claims;
- customer-owned asset references with hashes and permissions;
- requested output class and route requirements;
- budget ceiling and approval policy references;
- callback/event contract version.

It does not send provider credentials, unscoped Company Brain dumps, or instructions to use a particular model/worker.

### Website Capability → Business builder

The adapter returns:

- accepted/rejected acknowledgment with provider job reference;
- coarse canonical milestones: accepted, researching, concepts_ready, founder_choice_required, building, reviewing, revision_required, package_ready, failed;
- customer-safe evidence and artifact references;
- cost reservation/settlement values;
- explicit FounderAction or Approval requirements;
- final package manifest, hashes and provider verification summary.

It does not expose Stromation paths, internal state names, worker traces, prompts, database keys or private scores as dependencies.

### Status translation

| Stromation concept | Canonical boundary |
|---|---|
| order/job created | `website.job.accepted` |
| industry research and directions work | `website.researching` |
| ranked/published direction previews | `website.concepts_ready` + Approval required |
| direction approval ref | canonical Approval external reference |
| proof/full build | `website.building` |
| render/review/rework | `website.reviewing` or `website.revision_required` |
| awaiting customer approval | Approval required: delivery |
| packaged_not_deployed | `website.package_ready`; never `verified` or `live` |

The mapping belongs in a future adapter module in `buisnessbuilder-future`. It must be tested against recorded fixtures, not imported constants.

## Integration seams

1. **Identity seam:** immutable opaque IDs; adapter maintains provider-reference map. No email/domain as primary key.
2. **Command seam:** canonical Job + WebsiteCapability request; idempotency key required.
3. **Event seam:** at-least-once canonical Events; consumers deduplicate by `event_id` and sequence.
4. **Approval seam:** digest-bound Approval with exact scope, expiry and approver; provider verifies the opaque receipt.
5. **Budget seam:** reserve → authorize provider ceiling → report usage → settle/release. Overrun creates FounderAction, never an automatic increase.
6. **Artifact seam:** immutable metadata, customer ownership, content hash, media type and retention/export policy.
7. **Verification seam:** provider evidence can support but cannot automatically satisfy cross-system readiness.
8. **Failure seam:** typed, retryable/nonretryable failure with safe public message and private evidence reference.
9. **Cancellation seam:** cancel request is idempotent; provider confirms stopped/too-late; artifacts remain governed by ownership/retention policy.

## Website Capability versioning

- Contract ID: `website.capability.request.v1` / `website.capability.result.v1`.
- Additive optional fields are allowed within v1.
- Removing/renaming fields, changing enum meaning or weakening gates requires v2.
- Provider adapters declare supported versions and features through the Capability record.
- Contract fixtures include Billy Bob new build, revision, rejected approval, budget exhaustion and package-ready-not-live.

## Billy Bob integration trace

1. `company.created`: Billy Bob Lawn Care, Denton County, mobile service.
2. `decision.approved`: 12-mile radius, mow/edge/blow, no chemicals/irrigation/trees/hardscape.
3. `job.created`: Build website; $4 ceiling; claims limited to founder-approved facts.
4. `budget.reserved`: $4 for Website Capability.
5. `website.job.accepted`: provider ref stored by adapter.
6. `website.concepts_ready`: three artifacts plus customer-safe ranking receipt.
7. `approval.requested`: Billy selects one direction; subject digest binds the selection set.
8. `approval.granted`: adapter forwards opaque receipt.
9. `website.building` then `website.reviewing`.
10. `website.package_ready`: package artifact and provider evidence arrive; $2.71 actual usage settles and $1.29 releases.
11. Business-builder verification tests package structure and later production form/DNS ownership. Package-ready alone cannot make the company Ready.
12. `founder_action.required`: buy domain in Billy's account. Completion requires registrar evidence.

## What waits for Claude Phase 1E

- Freeze the exact Stromation adapter implementation.
- Assert final input/output field mapping against Phase 1E artifacts.
- Set expected website quality evidence and customer-safe score exposure.
- End-to-end test using a real Phase 1E packaged result.
- Decide retry/cancellation semantics around the final engine state machine.
- Any production endpoint, credentials, webhook, queue or deployment work.
- Any claim that the Website Capability is production-ready.

## Pre-integration acceptance

- All schemas validate and examples round-trip.
- No `buisnessbuilder-future` module imports Stromation code.
- Recorded provider fixtures drive adapter tests.
- Duplicate events and commands are harmless.
- Approval subject mismatch, expiry and revocation fail closed.
- Budget overrun fails closed.
- `package_ready` never maps to `live`, Ready or Fully Set by itself.
- Customer can export artifacts and revoke provider access.

