# Identity and Accounts

Status: offline/dev-safe implementation. No live identity provider is configured.

## Ownership and boundaries

Identity answers who may act. It does not own business truth, readiness, execution, or billing facts.

| Concept | Meaning | Identifier |
|---|---|---|
| User | Authentication identity and account status | `user_id` |
| FounderProfile | Human-facing founder profile linked to a user | `founder_profile_id`, `user_id` |
| Tenant | Isolation and policy boundary | `tenant_id` |
| Organization | Customer account and ownership container | `organization_id`, `tenant_id` |
| Membership | A user's role in one organization | `membership_id`, `tenant_id`, `user_id` |
| Company | Business entity owned by Company Brain | referenced by `company_id` only |

`User`, `FounderProfile`, `Tenant`, `Organization`, and Company are deliberately separate. A team or support user need not have a FounderProfile. Account ownership and ownership transfer require one explicitly. One tenant can contain multiple companies and multiple users. A user may hold memberships in multiple tenants. Email is never a tenant key. Every company-sensitive decision carries `tenant_id`, `user_id`, and `company_id`.

Account creation creates a Tenant, Organization, and active OWNER membership atomically at the service boundary. A company is attached by opaque `company_id`; no Company Brain state is copied. Invites start as `INVITED`, become `ACTIVE` only when the named user accepts, and end as `REMOVED`. Role changes are explicit and audited; OWNER assignment can occur only through ownership transfer. Ownership transfer promotes an existing active member and demotes the prior owner to ADMIN. The only owner cannot be removed or deactivated before transfer. Deactivated users and suspended tenants or organizations fail authorization.

## Authorization

Authorization is evaluated server-side from repository state. UI claims, route state, or a role supplied by a browser are not authoritative. Unknown roles, missing memberships, inactive users, suspended tenants, cross-tenant companies, and unlisted permissions deny by default.

| Permission | OWNER | ADMIN | MEMBER | SUPPORT with grant |
|---|---:|---:|---:|---:|
| View company state | Yes | Yes | Yes | Scoped |
| Approve founder decisions | Yes | No | No | Never |
| Authorize spend | Yes | No | No | Never |
| View billing | Yes | Yes | No | Never |
| Change subscription | Yes | No | No | Never |
| Manage members | Yes | Yes | No | Never |
| Perform handoff | Yes | No | No | Never |
| Access artifacts | Yes | Yes | Yes | Scoped |
| Interact with AI workforce | Yes | Yes | Yes | Never |
| Request support | Yes | Yes | Yes | No |

OWNER is an account role. Founder-only decisions remain subject to the Runtime approval record and identity checks; an OWNER role alone does not fabricate an approval. ADMIN can operate the account but cannot assume financial, founder-only, ownership-transfer, or handoff authority.

## Support access and impersonation

Support is never a hidden superuser path. A support user needs all of the following:

1. An active SUPPORT membership in the tenant.
2. An explicit grant approved by the tenant OWNER.
3. A required reason, allowed permissions, start/end time, tenant scope, and optional company scope.
4. A separate active support impersonation session tied to that grant.
5. An audit event for session start, each material support action, and session end.

Grants are capped at seven days in the offline implementation. Support can receive only scoped company view or artifact access. It can never approve founder decisions, authorize payment or spend, view/change billing, manage members, perform handoff, interact with the AI workforce, bypass identity verification, or weaken Runtime approvals and budgets.

## Authentication and sessions

`AuthenticationProvider` and `FutureMfaProvider` are provider-neutral ports. `SessionService` supplies sign-in, token validation, sign-out, account recovery, and email-verification flows. The offline adapter hashes developer assertions. Session and recovery tokens are random, returned once, and stored only as SHA-256 digests. The repository never stores raw passwords, authentication proofs, provider credentials, or MFA secrets.

The fake adapter is for tests and local development only. A live integration must verify provider signatures/tokens, rotate keys, enforce rate limits, revoke sessions on material account changes, protect recovery against enumeration, and complete a security review. Auth0, Clerk, Cognito, Supabase Auth, and other providers remain unselected.

## Persistence and audit

`IdentityRepository` is the storage port. `InMemoryIdentityRepository` supports deterministic tests. `SQLiteIdentityRepository` provides an offline durable adapter with tenant indexes and database triggers that reject audit updates/deletes. The model is intentionally portable to PostgreSQL; no PostgreSQL service is deployed.

Material membership, role/ownership, support-access, user-deactivation, tenant-suspension, and company-attachment actions include actor, tenant, time, reason/source, target, and company when applicable.
