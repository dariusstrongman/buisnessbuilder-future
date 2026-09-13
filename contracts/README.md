# Shared Contracts

These Draft 2020-12 JSON Schemas are the repository-neutral language of the business builder. They define the seam; they do not implement production behavior.

| Contract | File | Purpose |
|---|---|---|
| Common | `common.schema.json` | IDs, timestamps, provenance, money and typed references |
| Company | `company.schema.json` | Tenant/company identity, archetype, ownership and lifecycle |
| Event | `event.schema.json` | Immutable at-least-once integration event envelope |
| Job | `job.schema.json` | Provider-neutral unit of requested work |
| Capability | `capability.schema.json` | Discoverable subsystem/provider feature declaration |
| Verification | `verification.schema.json` | Proposed → Executed → Tested → Verified with evidence/freshness |
| Approval | `approval.schema.json` | Digest-bound founder/human/external authorization |
| Artifact | `artifact.schema.json` | Customer-owned immutable deliverable/evidence metadata |
| Budget/Spend | `budget-spend.schema.json` | Ceiling, reservation, settlement and pass-through costs |
| FounderAction | `founder-action.schema.json` | Human-only actions and completion evidence |
| AuditEvent | `audit-event.schema.json` | Append-only accountable state-change receipt |
| Website Capability | `website-capability.schema.json` | Stable plug-in seam for Stromation's website engine |

Rules:

- All timestamps are UTC RFC 3339.
- IDs are opaque strings; email/domain is never identity.
- Every mutation command carries an idempotency key.
- Secrets are locators, never values.
- Additive optional fields may remain within v1. Breaking semantic changes require v2.
- Examples and fixtures must use fictional Billy Bob data only.
- Provider-specific state stays under opaque references, not canonical enums.

