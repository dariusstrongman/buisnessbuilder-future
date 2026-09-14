# Residential Cleaning Evidence Submission and Review v1

This is a narrow execution path for the Denton residential-cleaning pilot. It
does not create a general document-management system and it performs no filing,
purchase, account change, provider action, or communication.

## Authority and state flow

The Founder Action state model remains:

`PREPARED → EXPLAINED → LINKED → FOUNDER_COMPLETED → RESULT_CAPTURED → VERIFIED`

Evidence adds an orthogonal review flow:

`PENDING_REVIEW → ACCEPTED | REJECTED | MORE_EVIDENCE_REQUIRED`

`MORE_EVIDENCE_REQUIRED → new immutable submission → PENDING_REVIEW`

- An authenticated OWNER submits evidence in the server-derived action,
  tenant, and company scope.
- File bytes go to the configured private artifact store. PostgreSQL stores
  only the scoped artifact/evidence metadata and SHA-256 identity.
- A file begins quarantined. Only a clean scanner result makes its artifact
  available. The default scanner is fail-closed and leaves the upload pending.
- An authorized Business Builder operator is represented by an active SUPPORT
  membership plus a founder-granted, company-scoped `artifacts.access` grant
  and an unexpired audited impersonation session.
- The operator may accept, reject, or request specific additional evidence.
  The founder, OWNER, ADMIN, MEMBER, model, and caller-supplied role cannot
  manufacture the operator authority.
- Runtime moves the Founder Action to `RESULT_CAPTURED` only when the exact
  current accepted-review set contains the action's required strong evidence.
- Verification independently reloads the active reviews, evidence ownership,
  hashes, expiry, authority provenance, and provider receipt before it can
  produce `VERIFIED`. Acceptance by itself is not Verification.

When evidence storage is configured, the production-shaped composition uses
`ResidentialCleaningReviewedEvidenceVerifier` for that independent integrity
test. The older deterministic test verifier is not required for this path.

Company Brain remains canonical for the Founder Action and its forward state.
Runtime remains the only transition/execution authority. Evidence submissions,
scan records, and immutable reviews use the existing Runtime/PostgreSQL broker
record persistence; there is no new database or orchestrator. Verification
remains the authority for Ready and Fully Set.

## Evidence data

An `EvidenceSubmission` persists:

- opaque submission ID, tenant, company, and Founder Action;
- server-derived submitting founder;
- evidence type and source classification;
- immutable artifact, provider-receipt, authority, or structured reference;
- SHA-256 content/reference identity;
- safe filename, validated content type, and byte size when applicable;
- scan state, submission time, expiry/revocation readiness; and
- optional prior submission superseded by a resubmission.

An immutable `EvidenceReview` persists:

- scoped review and submission IDs;
- decision and bounded reason code;
- bounded operator note and requested additional evidence;
- server-derived reviewer identity and operator authority;
- timestamp, review version, request digest; and
- the prior review it supersedes, when correcting history.

An accepted review is never edited. A correction uses a new review with an
explicit `supersedes_review_id`. Likewise, resubmission never rewrites prior
evidence. Historical submissions and reviews remain visible in the safe Build
Room projection.

## Evidence classes and strength

The customer-safe model distinguishes:

| Type | Customer source | Verification strength |
| --- | --- | --- |
| `FOUNDER_ATTESTATION` | Existing founder completion transition | Assertion only |
| `AUTHORITY_CONFIRMATION` | Opaque prevalidated authority-artifact reference | Can satisfy only the matching action authority requirement |
| `PROVIDER_RECEIPT` | Existing successful scoped receipt for the exact capability/operation | Can satisfy only that action's provider requirement |
| `BUSINESS_BUILDER_TEST` | Verification-internal only | Cannot be customer supplied |
| `SUPPORTING_SCREENSHOT` | PNG/JPEG upload | Supplemental unless an action definition explicitly requires a screenshot |
| `SUPPORTING_DOCUMENT` | PDF upload | Supplemental; upload alone is not authority provenance |
| `OTHER_SUPPORTED_REFERENCE` | Bounded structured reference | Supplemental and deliberately unverified |

A screenshot or uploaded PDF cannot claim to be an authority confirmation.
An arbitrary structured object cannot claim a provider receipt or Business
Builder test.

## File controls

- Supported files: PDF, PNG, and JPEG only.
- Maximum decoded size: 512 KiB, which fits the existing one-megabyte HTTP
  request ceiling after base64 and JSON overhead.
- Filename length and character/path validation; no path components.
- Extension, declared MIME type, and magic bytes must agree.
- Bytes are never interpreted or rendered by this service.
- Private tenant/company object keys and no public access.
- Quarantine by default; `PendingMalwareScanner` fails closed when no scanner
  exists.
- `DeterministicMalwareScanner` is a test adapter only and is not production
  malware protection.
- Review and download re-read bytes and compare SHA-256 against both the
  immutable submission and artifact metadata.
- Downloads require a current scoped principal, clean evidence, and issue a
  120-second artifact-store URL. The object key and evidence contents are not
  included in Build Room.
- Access, submission, scanning, and review are audited without payload bytes.

Production still requires a real malware-scanning adapter and private object
storage. The PostgreSQL composition root accepts both through explicit ports;
it does not silently install a test scanner or an in-memory production store.

## Customer API

For a residential-cleaning Founder Action:

- `GET|POST .../{action_id}/evidence-submissions`
- `GET .../{action_id}/evidence-submissions/{submission_id}`
- `POST .../{action_id}/evidence-submissions/{submission_id}/access`
- `GET|POST .../{action_id}/evidence-reviews`

The submission POST accepts either a bounded base64 file upload or an opaque
pre-existing authority/provider reference. Extra fields are rejected. The
review POST derives the reviewer from the authenticated scoped operator
session and does not accept a reviewer ID or role from the body.

When this subsystem is configured, the older direct `.../{action_id}/evidence`
transition fails closed with `evidence_submission_required`; it cannot bypass
quarantine and review.

## Honest limitations

- The included deterministic scanner proves control flow only.
- The base64 upload is deliberately small; larger documents require a future
  direct-to-private-object-store upload ceremony with checksum-bound finalize.
- Authority/provider records must already have been created by an authenticated
  adapter. This branch does not call a government or commercial provider.
- Scheduled expiry/revocation reconciliation is not implemented; records carry
  the timestamps and references required for that later worker.
- Human-review staffing, operator identity provisioning, retention/legal policy,
  and production artifact access logging require launch approval.
