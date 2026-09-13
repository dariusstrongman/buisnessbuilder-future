# Setup/Admin (Left Arm)

Status: offline/dev-safe implementation. No live providers, filings, purchases, credentials, payments, or production actions.

## Boundary

`businessbuilder.setup_admin` turns an archetype and locality into tenant-scoped setup requirements. It records whether the founder chose **Do It**, **Guide Me**, or **Skip**, generates FounderAction v2-compatible templates, preserves consequences, and projects blockers for Fully Set. Mode selection requires a verified, tenant/company-bound founder or authorized-human identity assertion.

“Do It” means the system may prepare and coordinate reversible work. A no-authority reversible item may be completed by the system only through the idempotent, typed-evidence completion command. “Guide Me” instead waits for the named founder/authorized human to follow the guide and submit evidence. Neither mode allows an AI to sign, attest, file, accept terms, pass identity checks, choose insurance, authorize spend, or pay. An external status is complete only after typed evidence from a verified, scoped provider/authority actor. The catalog is product workflow metadata, not legal, tax, insurance, or financial advice.

High-risk FounderActions bind an effective Approval v2 projection: named approver identity and role, exact target, exact monetary ceiling and currency when applicable, subject digest, approval reference, and expiration. Public FounderAction v2 output carries the expiry, approval dependency, plain-language target/amount preview, and evidence receipt references without changing the released schemas.
Completion evidence is accepted only with a verified, tenant-scoped source authorization and an exact binding to the current FounderAction, approval reference and digest, target, monetary scope, and submitting actor identity. Generic provider receipts and same-ID/different-role submissions cannot satisfy a high-risk action.

## State model

`undecided → planned → in_progress/waiting_founder → waiting_external → completed`

Choosing Skip moves the requirement to `skipped` while preserving its stated consequence. A dependency version change moves an affected completed or active item to `invalidated`; the founder must choose a mode again and fresh evidence must traverse the gates.

Illegal transitions fail closed. Every mutating command is digest-bound and idempotent. Reusing an idempotency key with different command content is rejected. Reissuing the same plan with a new key returns the existing plan without overwriting progress; changing plan identity fails closed.
Starting work requires a verified trusted-system, authorized-agent, founder, or authorized-human actor binding. FounderAction lifecycle versions are append-only; dependency re-evaluation creates a new action generation while the expired generation and its receipt history remain retrievable.

## Tenancy, provenance, and history

Every item, FounderAction, typed evidence record, and history event carries `tenant_id` and `company_id`. Repository reads require both and do not reveal cross-tenant existence. Evidence is subject-bound, source-typed, verified before use, and cannot be rebound under the same reference. History is append-only, sequence ordered, and binds before/after snapshots with SHA-256 digests. Fixtures contain only fictional Billy Bob data and store evidence references, never secrets.

## Billy Bob catalog

The mobile lawn-service fixture includes founder-owned domain setup, neutral formation coordination, insurance review, supported-service scope attestation, and an account-recovery map. The scope is mow/edge/blow with regulated work excluded. Skipping selected material requirements stays visible and blocks Fully Set.

## Verification

Run the subsystem suite:

```bash
python -m pytest tests/setup_admin -q
```

Run the repository suite:

```bash
python -m pytest -q
```
