# AI Workforce Product Policy

Status: offline product-domain reference implementation. It defines what a customer-facing role may request; it does not execute work.

## Boundary

The Business Builder owns tenant-scoped role definitions, grants, denials, ceilings, escalation policy, lifecycle, and customer-safe projections. Stromation continues to own Sol and its private worker runtime. The policy package does not select models, route providers, send messages, spend money, or import Stromation internals. Allowed decisions are inputs to the existing Core Runtime, whose approval, budget, job, and audit controls still must pass.

## Fail-closed laws

1. A capability and action must have an exact grant. Unknown roles, capabilities, and actions are denied.
2. Tenant ID, company ID, role ID, and role version are all required. Company IDs never imply tenant identity.
3. Paused, revoked, and superseded definitions cannot authorize execution.
4. Revisions create a new immutable version and supersede the previous version. Revocation is permanent.
5. A returned `approval_required` or `escalate` decision never permits execution. Runtime must obtain a digest-bound approval or create a human exception path.
6. Budget overruns escalate without changing the ceiling. Currency mismatch denies.
7. AI workers can never sign or submit filings, pay, open accounts, accept provider terms, verify identity, attest licenses, select insurance, hire, raise budgets, bypass approval, approve public claims, or authorize spend.
8. Idempotency keys are scoped by tenant/company. Exact replay returns the original evaluation while its authority remains current; reuse with a different request digest fails.
9. Audit records are append-only product receipts. Projections can be rebuilt from definitions, evaluations, and audit records.
10. Role creation, revision, pause, resume and revocation require an opaque authority proof validated by a trusted adapter as founder or authorized manager. Caller-supplied actor text is never authority.
11. An exact evaluation replay is executable only while its bound definition remains the current active definition with the same digest. Pause, revocation, supersession or mutation fails the replay closed.

## Billy Bob roles

| Role | Objective | Representative grants | Required escalation / denials |
|---|---|---|---|
| Intake Assistant | Capture complete lead facts within approved scope | read, classify, validate, request missing information, draft acknowledgment | out-of-radius, unsupported or unsafe work; cannot accept jobs or promise availability |
| Quote Drafting Assistant | Draft from approved services and quote policy | read policy/scope/capacity, draft standard quote | incomplete facts, regulated scope and capacity exceptions; cannot send quotes, discount or refund |
| Inbox Assistant | Handle approved routine email under policy | read, classify, draft, preapproved reply, archive, spam | legal threats, disputes, refunds, safety, commitments; cannot change mailbox access/forwarding |
| Review Follow-up Assistant | Request honest feedback after confirmed completion | confirm completion/permission, draft and send approved request | opt-out, uncertain completion, negative feedback; cannot fabricate, incentivize or suppress reviews |

The fixture is fictional and uses no provider credentials, live destinations, messages, customer data, or spend.

## Evaluation output

Every evaluation records the bound role version and definition digest, request digest, decision, reason code, required human role if any, estimated cost, and timestamp. Only `allow` permits the caller to proceed to Core Runtime. `approval_required`, `escalate`, and `deny` are non-executable outcomes.
