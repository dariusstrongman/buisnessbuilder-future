# Contract versioning and tenant scope

## Decision

Released v1 contracts remain compatible in name, schema version, and meaning. They continue to project the company-scoped payloads expected by existing consumers. They are not safe as the sole routing envelope because several omit `tenant_id`.

The offline nervous-system boundary therefore introduces additive v2 schemas for every public object whose identity or mutation is tenant-bound:

- Company
- Event
- Job
- Verification
- Approval
- AuditEvent
- Budget/Spend
- FounderAction
- Artifact
- Website Capability request, progress, and result

Each v2 schema retains the corresponding v1 fields and semantics, changes only its schema identifier/version, and requires `tenant_id`. `businessbuilder.integration.tenant_v2_projection()` performs the additive projection without mutating the v1 value.

Capability definitions are intentionally not given a tenant-scoped v2. The capability registry describes globally available provider contracts; tenant authorization, budgets, jobs, requests, and results are scoped elsewhere. Making the definition tenant-bound would confuse availability metadata with authorization.

## Compatibility rules

1. v1 schemas and `to_contract()` projections remain supported.
2. A v1 projection must not be treated as a cross-tenant routing envelope.
3. New nervous-system producers should emit v2 and include the internal `tenant_id` invariant explicitly.
4. A consumer may project v2 to v1 only for a known tenant context. It must not infer tenant identity from `company_id`.
5. Storage and service calls continue to require both `tenant_id` and `company_id`; compatibility never weakens repository scope.
6. The future Website Engine adapter must support website capability v2 before live multi-tenant routing.

## Schema map

| v1 | additive v2 |
|---|---|
| `company.schema.json` | `company.v2.schema.json` |
| `event.schema.json` | `event.v2.schema.json` |
| `job.schema.json` | `job.v2.schema.json` |
| `verification.schema.json` | `verification.v2.schema.json` |
| `approval.schema.json` | `approval.v2.schema.json` |
| `audit-event.schema.json` | `audit-event.v2.schema.json` |
| `budget-spend.schema.json` | `budget-spend.v2.schema.json` |
| `founder-action.schema.json` | `founder-action.v2.schema.json` |
| `artifact.schema.json` | `artifact.v2.schema.json` |
| `website-capability.schema.json` | `website-capability.v2.schema.json` |

This is an additive release, not an in-place reinterpretation. Removal of v1 is not part of offline integration v1.
