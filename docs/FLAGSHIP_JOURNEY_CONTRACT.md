# Flagship Journey Integration Contract v1

The customer-facing `businessbuilder-site` consumes the authenticated customer
API in this repository. This contract is deliberately limited to the Denton,
Texas residential-cleaning pilot.

## Authority boundary

- The site holds the opaque customer session only in a server-managed,
  HttpOnly cookie and forwards it as a bearer credential from its server-side
  API boundary. The browser cannot supply tenant, company, actor, or role
  authority headers.
- Company selectors in URLs remain non-authoritative. The customer API derives
  tenant, organization, membership, role, and company scope from current
  Identity records and defaults to deny.
- Company Brain owns the captured intake and recommendation. Runtime owns the
  digest-bound approval and Founder Action transitions. Commercial owns the
  pending order. Verification alone owns Ready and Fully Set.

## Journey mapping

| Site operation | Customer API |
| --- | --- |
| Open residential-cleaning build | `POST /api/v1/pilots/residential-cleaning/intakes` |
| Reload journey | `GET /api/v1/companies/{company_id}/residential-cleaning-pilot` |
| Approve exact recommendation | `POST /api/v1/companies/{company_id}/residential-cleaning-pilot/approve` |
| Load canonical Build Room | `GET /api/v1/companies/{company_id}/build-room` |
| Explain/link/attest Founder Action | corresponding `POST .../founder-actions/{action_id}/{operation}` |
| Submit evidence | `GET|POST .../evidence-submissions` |
| Review evidence | `GET|POST .../evidence-reviews` with a scoped SUPPORT session |

The four site starting points map exactly to `idea`, `started`, `existing`, and
`running`. The value is stored in `intake_residential_cleaning_v1`; it conveys
founder context only and grants no authority.

## Commercial truth

Approving the recommendation creates or reuses the canonical
`BUILD_BUSINESS` order. Without an authoritative billing event it remains
`draft` in `awaiting_authoritative_billing_event` mode and grants no
entitlement. The integration never simulates payment or activation.

## Retry behavior

The site sends a stable idempotency key for the original intake and a fresh
stable key for each explicit mutation. A response loss may be retried with the
same key and body. Reusing a key with different input is a conflict. Reads after
any mutation are authoritative; the site does not promote optimistic progress
to canonical state.
