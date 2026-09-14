# Authenticated Customer API v1

This API is a provider-neutral transport over the existing Identity, Company
Brain, Runtime, Verification, Commercial, and Build Room authorities. It accepts
opaque session tokens created by the existing session port. It does not contain
a signup or login provider and does not integrate a live billing provider.

## Trust flow

1. The transport accepts an opaque bearer session and applies the existing
   request-target and one-megabyte body limits.
2. `PrincipalContextAuthority` hashes the token, loads the durable session, and
   rejects an expired session or inactive user.
3. The requested company identifier is only a resource selector. The API maps it
   to an organization and tenant from Identity data, then revalidates the tenant,
   organization, membership, role, company attachment, and any support grant and
   impersonation session.
4. The server mints a short-lived signed `AuthenticatedPrincipal`. Existing
   policy and service boundaries authorize the operation. Caller-supplied actor,
   role, user, tenant, or company authority headers are rejected.
5. Responses are explicit projections. They omit tenant routing, token and
   provider references, payment internals, audit internals, Runtime subject
   digests, and Company Brain provenance.

The transport creates or validates request and correlation IDs and returns them
as `X-Request-ID` and `X-Correlation-ID`. Denied privileged operations are
persisted through the Identity repository with those IDs and without request
payloads.

## Route exposure matrix

| Method | Route | Required authority | Backing authority |
| --- | --- | --- | --- |
| GET | `/api/v1/me` | active session and user | Identity |
| GET | `/api/v1/organizations` | active membership; scoped support session for SUPPORT | Identity |
| GET | `/api/v1/memberships` | active membership; scoped support session for SUPPORT | Identity |
| POST | `/api/v1/companies` | OWNER of exactly one selected organization | Identity + Company Brain |
| GET | `/api/v1/companies` | `view_company_state` per returned company | Identity + Company Brain |
| GET | `/api/v1/companies/{company_id}` | `view_company_state` | Company Brain |
| GET | `/api/v1/companies/{company_id}/build-room` | `access_artifacts` | projection of existing authorities |
| GET | `/api/v1/companies/{company_id}/founder-actions` | `view_company_state` | Company Brain |
| GET | `/api/v1/companies/{company_id}/readiness` | `view_company_state` | Verification only |
| POST | `/api/v1/companies/{company_id}/approvals/{approval_id}` | Runtime approval policy for the server principal | Runtime |
| POST | `/api/v1/orders` | `authorize_spend` | Commercial |
| GET | `/api/v1/orders?company_id=...` | `view_billing` | Commercial |
| GET | `/api/v1/orders/{order_id}?company_id=...` | `view_billing` | Commercial |
| GET | `/api/v1/subscriptions?company_id=...` | `view_billing` | Commercial |
| GET | `/api/v1/entitlements?company_id=...` | `view_company_state` | Commercial |
| GET | `/api/v1/companies/{company_id}/handoff` | `view_company_state` | Verification |
| POST | `/api/v1/companies/{company_id}/handoff` | `perform_handoff`; OWNER only | fail-closed until an authoritative handoff workflow exists |
| POST | `/api/v1/pilots/residential-cleaning/intakes` | active authenticated founder; creates or reuses one OWNER organization | Identity + Company Brain + Runtime |
| GET | `/api/v1/companies/{company_id}/residential-cleaning-pilot` | `view_company_state` | safe composition of Company Brain, Runtime, Commercial, and Verification |
| POST | `/api/v1/companies/{company_id}/residential-cleaning-pilot/approve` | current OWNER/founder through signed principal | Runtime approval, then Company Brain and Commercial |
| GET | `/api/v1/companies/{company_id}/residential-cleaning-pilot/founder-actions/{action_id}` | `view_company_state` | Company Brain projection |
| POST | `/api/v1/companies/{company_id}/residential-cleaning-pilot/founder-actions/{action_id}/explain` | current founder/OWNER | Runtime + Company Brain |
| POST | `/api/v1/companies/{company_id}/residential-cleaning-pilot/founder-actions/{action_id}/launch` | current founder/OWNER | Runtime prepared handoff only |
| POST | `/api/v1/companies/{company_id}/residential-cleaning-pilot/founder-actions/{action_id}/complete` | current founder/OWNER | Runtime founder attestation; never Verification |
| POST | `/api/v1/companies/{company_id}/residential-cleaning-pilot/founder-actions/{action_id}/evidence` | current founder/OWNER | Runtime scoped reference validation + Company Brain |

SUPPORT can only read within an active grant and impersonation-session scope.
SUPPORT cannot approve, spend, change billing, perform handoff, or satisfy
identity verification. Company identifiers and order identifiers are always
looked up inside the server-derived tenant scope, so a cross-tenant selector is
returned as not found.

## Authority preservation

- Readiness is evaluated only by `VerificationService` and
  `ReadinessEvaluator`; commercial and Runtime state cannot set Ready or Fully
  Set.
- Build Room is a read-only projection. Its event input is empty and it never
  becomes execution truth.
- Approvals are sent to `JobOrchestrator` with the signed server-created
  principal. The Runtime principal verifier revalidates it against current
  Identity state.
- Order prices and billing modes come from the existing Commercial catalog. The
  customer request cannot supply an amount or provider payload.
- Handoff mutation remains unavailable with HTTP 409 because no existing
  authoritative workflow safely implements that transition.

## Deployment prerequisites not supplied here

A production session adapter, managed signing-key rotation, rate limiting at a
trusted ingress, and the authoritative handoff workflow remain outside this
repository-only change. The PostgreSQL composition root requires a signing key
of at least 32 bytes and uses the existing PostgreSQL repositories and
migrations; it creates no second persistence layer.
