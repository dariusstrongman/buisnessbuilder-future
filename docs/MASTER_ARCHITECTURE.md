# Master Architecture

Status: architecture-only skeleton. No production code or infrastructure changes.

## System thesis

The business builder is a tenant-scoped company assembly platform. It turns a founder's real situation into an owned, evidence-backed operating company. It does not expose Stromation internals and does not equate generation with verification.

The product journey remains:

`Idea → Understand Me → Challenge/Recommend → Approve → Watch My Company Get Built → Founder Actions → Verification → Ready → Handoff → Operate/Grow`

The three offers share one architecture:

- **Build My Website** invokes the Website Capability only.
- **Build My Business** coordinates multiple capabilities and verification.
- **Run My Business** operates explicitly permitted workflows after handoff.

## Body model

| Body part | Subsystem | Owner now | Boundary |
|---|---|---|---|
| Head | Company Brain | `buisnessbuilder-future` | Canonical tenant facts, decisions, provenance, confidence and lifecycle. It does not store provider secrets or mirror engine internals. |
| Torso | Core runtime/platform | `buisnessbuilder-future` | Capability registry, job orchestration, state projections, adapter interfaces and durable coordination. |
| Right arm | Website engine | `Stromation` | Existing premium website pipeline behind the Website Capability adapter. |
| Left arm | Setup/admin/integrations | `buisnessbuilder-future` | Guidance and coordination for accounts, formation/admin choices and provider integrations. Founder/external authority performs controlled actions. |
| Right leg | AI workforce | `buisnessbuilder-future` for product contracts; `Stromation` remains owner of Sol/runtime internals | Tenant-scoped worker definitions and permissions. Never import Sol's private org implementation. |
| Left leg | Verification | `buisnessbuilder-future` | Cross-capability verification records, evidence freshness and readiness policy. Website-internal QA stays in Stromation. |
| Skin | Customer-facing UI | `buisnessbuilder-future` | Build Room, approvals, founder actions, evidence, Ready/Fully Set and handoff. |
| Nervous system | Events, permissions, approvals, budgets, audit | `buisnessbuilder-future` | Shared contracts and append-only records. Adapters translate provider/engine events into canonical events. |

## Architectural laws

1. Every record is scoped by `company_id`; customer identity never rides in free text.
2. The Company Brain is the system of record for product facts and decisions. Capability providers remain authoritative for their own execution details.
3. Cross-boundary communication uses versioned contracts, never imports from another repository.
4. Commands are idempotent. Events are append-only. Projections may be rebuilt.
5. Approval cannot be inferred from conversation, progress or payment. It is a bound record with subject digest, approver and expiry.
6. Confidence is not verification. Proposed, Executed, Tested and Verified are separate states.
7. Founder-only, external-authority and irreversible actions cannot be completed by an AI worker.
8. Budgets are ceilings, not targets. Reservation precedes execution; settlement follows evidence.
9. Customer accounts, domains, data, assets and outputs remain customer-owned and exportable.
10. The larger platform cannot depend on Stromation job-state names, database tables, worker prompts, file paths or model choices.

## Canonical control flow

1. UI submits a founder-approved objective and source facts to the Company Brain.
2. Core creates a canonical `Job` and evaluates permission, approval and budget requirements.
3. Capability router chooses a registered capability implementation.
4. An adapter converts the canonical job into the provider request.
5. Provider emits status/evidence. Adapter converts these to canonical `Event`, `Artifact`, `Verification` and `AuditEvent` records.
6. Readiness projection evaluates required verifications and founder actions.
7. UI displays the projection and evidence. It never reads provider storage directly.

## Billy Bob end-to-end

Billy Bob approves a 12-mile recurring mow/edge/blow offer. The Company Brain stores the service radius, exclusions, equipment, capacity hypothesis and decision provenance. Core creates `job_bb_website_001` with a website capability requirement and a $4 provider ceiling. The Website adapter submits only the approved brief, real assets, public-claim allowlist and callback contract. Stromation performs its existing research, divergent directions, study/ranking, direction approval, proof/full build, review and package pipeline. The adapter reports coarse milestones, not Stromation's private states. Billy selects a direction through a canonical Approval. The returned static package becomes an Artifact; Stromation's internal review evidence becomes supporting evidence, while business-builder verification separately tests ownership, production URL, lead form and handoff. Billy purchases the domain himself. Only then can the company become Ready. It becomes Fully Set after selected admin, insurance, monitoring and handoff requirements are verified.

## Data ownership

| Data | System of record | Notes |
|---|---|---|
| Founder/company facts and decisions | Company Brain | Provenance and confidence required. |
| Engine execution trace, studies, internal scores | Stromation | Referenced by opaque IDs/evidence receipts only. |
| Public website marketing content | `website-builder` | Legacy standalone Website Builder surface; not runtime truth. |
| Product UI state and readiness projection | business-builder platform | Derived from canonical events/verifications. |
| Secrets | customer/provider secret store | Contracts carry locators, never values. |
| Approval and audit receipts | business-builder nervous system | Append-only and digest-bound. |
| Generated website package | customer-owned artifact store | Exportable; content hash recorded. |

## Readiness

**Ready** means one narrowly defined customer can safely traverse the tested lead-to-service path, critical assets are owned and verified, and no critical blocker is open.

**Fully Set** means Ready plus every selected formation/admin/provider obligation, monitoring, recovery and handoff requirement is current and verified, with no material waiver.

## Contract catalog

Canonical schemas live in `/contracts`. `common.schema.json` supplies shared identifiers, provenance and status primitives. The public contracts are Company, Event, Job, Capability, Verification, Approval, Artifact, Budget/Spend, FounderAction, AuditEvent and WebsiteCapability.

The earlier `docs/company-brain-v1.json` remains a useful populated design example. It is not the canonical interchange schema and must not be imported as one.

