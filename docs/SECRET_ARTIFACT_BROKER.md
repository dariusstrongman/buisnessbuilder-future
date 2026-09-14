# Capability-Scoped Secret and Artifact Broker v1

## Authority boundary

The broker is a narrow material-resolution service below the existing Runtime.
It does not admit, schedule, approve, budget, or orchestrate jobs. Every broker
operation first reloads the immutable agent envelope and asks Runtime to
revalidate tenant, organization, company, entitlement, AI Workforce role,
permission, approval, policy digest, and job expiration. The canonical Runtime
job must still be runnable or running.

```text
Runtime-approved bounded job
  -> opaque secret/artifact references in SQS
  -> broker reloads canonical envelope and active Runtime job
  -> exact tenant/company/capability/role/operation grant
  -> active external account + connection + credential reference
  -> process-only secret and hash-verified artifact material
  -> atomic ProviderReceipt claim
  -> bounded provider adapter with stable idempotency key
  -> non-sensitive receipt + append-only audit
  -> temporary material zeroized/discarded
```

Identity, Commercial, AI Workforce, Company Brain, and Verification retain their
existing authority. The broker cannot update Company Brain or readiness.

## External account model

- `ExternalAccount` is a tenant/company-scoped opaque provider account identity.
- `ProviderConnection` is its active, expiring, disconnected, revoked, or
  compromised connection state.
- `ExternalCredentialRef` stores an opaque `secretref_` identifier and an exact
  vault locator, never a value.
- `CredentialScope` and `CapabilityGrant` bind one connection to one role,
  capability, explicit operation set, permitted secret types, artifact
  classifications, and expiration/revocation state.
- `JobSecretRef` is the only credential-related queue projection. It contains
  `secret_ref`, provider, capability, tenant, and company—no locator or value.

Revocation and current entitlement are checked at resolution time. Customer
disconnect, provider revocation, credential expiry, compromised status, tenant
suspension, entitlement suspension, role/capability changes, and grant removal
therefore fail closed immediately.

## Secret material

`AwsSecretsManagerStore` accepts an exact ARN allowlist without wildcards and
uses only the ECS task-role credential chain. A long-lived provider value is
retrieved only after authorization, held in a non-serializable process-local
buffer, and overwritten and discarded when its context closes. Values are not
placed in envelopes, SQS, PostgreSQL records, provider receipts, artifacts, or
audit details.

Future providers should return short-lived capability-scoped sessions whenever
their APIs allow it. The same process-only material boundary applies.

## Artifact material

Artifact metadata records the tenant, company, immutable artifact ID, private
object key, SHA-256, content type, bounded size, classification, provenance,
status, creation time, and optional expiration. Keys are generated under:

```text
tenant/{tenant_id}/company/{company_id}/artifacts/{artifact_id}/{sha256}
```

The S3 adapter has no listing method. It accepts one exact key prefix, uses
server-side encryption, validates uploads, limits reads before buffering, and
can issue short-lived signed downloads bounded by the job expiration. The
broker reads and hashes content before access; quarantined, revoked, expired,
wrong-classification, unreferenced, oversized, cross-scope, or modified objects
are denied.

Signed URLs are temporary access material. They are returned in memory only and
are deliberately excluded from object representations, persistence, audits,
queue payloads, and proof output.

## Provider receipts and crash safety

Before an external action, the repository atomically claims a `ProviderReceipt`
using tenant, company, provider, operation, and a stable job-derived idempotency
key. Concurrent or duplicate delivery has one executor. A succeeded receipt is
replayed without another provider call. A retryable failure can be claimed by
one later attempt.

If a worker crashes after provider acceptance but before receipt completion,
the durable receipt remains `in_progress`; repeat execution is suppressed.
Provider reconciliation uses the stable provider request ID to recover the
result without repeating the action. Live adapters must support either provider
idempotency or authoritative reconciliation before production enablement.

Receipts store only scope, operation, IDs, status, timing, classification,
external object reference, retryability, and attempts. Provider request and
response bodies are never stored.

## Audit

The existing append-only Runtime audit records reference registration,
rotation timestamp metadata, grants, secret/artifact requests and denials,
signed-access issuance, provider attempts and receipts, duplicate suppression,
reconciliation, and revocation. It never records material, vault responses,
signed URLs, provider payloads, or authentication tokens.

## Current production blockers

This branch deliberately enables only deterministic test providers. Production
still requires reviewed live provider adapters with idempotency/reconciliation,
malware scanning and quarantine release for untrusted uploads, a separately
operated broker service boundary or equivalent workload identity enforcement,
rotation automation, alerts, and incident response procedures.
