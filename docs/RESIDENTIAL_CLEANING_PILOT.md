# Turnkey Customer Journey — Residential Cleaning Pilot v1

This is one deliberately narrow composition of existing Business Builder
authorities. It supports a founder starting an owner-operated residential
cleaning company in Denton, Texas. It is not a reusable multi-vertical policy
engine and it does not perform filings, payments, provider writes, or launch
actions.

## Customer path

1. An existing provider-neutral authenticated session submits a bounded intake
   to `POST /api/v1/pilots/residential-cleaning/intakes` with a stable
   idempotency key.
2. Identity creates the founder profile when absent, or reuses one unambiguous
   active OWNER organization. With no organization, Identity creates the
   tenant, organization, and OWNER membership.
3. Identity attaches a deterministic pilot company. Company Brain stores the
   intake as fact, the source-cited research packet as fact, and the proposed
   recommendation as inference.
4. Runtime creates a zero-cost `pilot.residential_cleaning.commit_scope` job
   requiring a founder-only approval. Nothing in the proposed recommendation
   is promoted to an approved offer, market, strategy, or price policy before
   that approval.
5. `POST /api/v1/companies/{company_id}/residential-cleaning-pilot/approve`
   submits the signed, server-created principal to Runtime. Runtime revalidates
   current Identity state and commits the exact recommendation digest through
   the Company Brain service.
6. Only after the job succeeds, Commercial creates the catalog-backed
   `BUILD_BUSINESS` order. The default composition leaves it awaiting a real
   authoritative billing event. Tests may opt into a deterministic no-money
   checkout adapter, which creates the normal commercial event, entitlement,
   audit, and outbox records.
7. Company Brain receives the evidence-gated Founder Action plan. Build Room
   projects the actual Runtime job, approval, Company Brain actions, budgets,
   and Verification state. It contains no scripted progress.

`GET /api/v1/companies/{company_id}/residential-cleaning-pilot` returns the
current safe journey projection. Every company-scoped request derives tenant,
organization, membership, role, and company access from Identity. Caller
authority fields are not accepted.

## Scope and responsibility

The committed recommendation covers the target customer, Denton service area,
launch offers and exclusions, bounded price logic, positioning, risks,
administrative requirements, and recommended systems. Work is labeled using
only:

- `BUSINESS_BUILDER`
- `FOUNDER_ACTION`
- `EXTERNAL_PROVIDER/AUTHORITY`

Founder Actions cover entity/administrative setup, EIN or tax-ID handling,
banking, insurance, licenses and permits, domain ownership, business email,
CRM, scheduling, payments, legal name/address confirmation, and the
pilot-specific scope approval. Actions begin `prepared` with empty evidence and
follow the evidence-gated flow documented in
`RESIDENTIAL_CLEANING_FOUNDER_ACTIONS.md`. The scope approval becomes
`verified` only with its durable Runtime approval reference.

## Research boundary

The v1 packet records official source URLs and a capture time. It uses U.S.
Census and BLS material for bounded market/labor context, Texas and City of
Denton authority pages for permit discovery, and SBA/IRS sources for the
administrative sequence. It explicitly does not claim legal completeness,
prove demand, invent a customer price, or replace authority confirmation. A
dated local competitor and public-offer survey remains an open Business Builder
task before public pricing is approved.

## Verification and safety

The scope-commit job is not launch evidence. A narrow Runtime verification
router prevents it from generating website, CRM, scheduling, or other launch
verification proposals. Ready and Fully Set continue to come only from the
existing Verification evaluator and remain false until its real policy gates
are independently satisfied.

This branch performs no real filing, purchase, money movement, domain change,
email, CRM write, provider authorization, external communication, deployment,
or AWS infrastructure mutation.

## Remaining pilot blockers

Before a first real paid residential-cleaning pilot, Business Builder still
needs an approved flagship-site/session handoff into this API; an authoritative
billing-provider event path for the order; execution services for website,
lead capture, quote, scheduling, and owned-account setup; evidence submission
and verification workflows for each Founder Action; current local competitor
research; jurisdiction/legal review; and complete Verification evidence for the
agreed Ready/Fully Set policy. External provider and authority steps still
require founder identity, terms, attestations, signatures, fees, or approval.
