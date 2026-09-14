# Residential Cleaning Founder Actions and Evidence v1

This subsystem closes the founder/external-authority loop for the Denton,
Texas residential-cleaning pilot only. It is not a general evidence platform.
No route performs a filing, purchase, account opening, provider activation, or
other external action.

## State model

The exact forward-only path is:

`PREPARED → EXPLAINED → LINKED → FOUNDER_COMPLETED → RESULT_CAPTURED → VERIFIED`

- **PREPARED:** the approved Runtime scope job writes a checklist, reason,
  responsibility, evidence policy, and safe destination into Company Brain.
- **EXPLAINED:** Runtime records that the founder was shown what is prepared,
  what remains uncertain, why founder authority is required, and what evidence
  will be needed.
- **LINKED:** Runtime issues the official or internal provider handoff. This is
  `prepared_handoff_only`; Business Builder does not claim the external action
  was launched or submitted.
- **FOUNDER_COMPLETED:** the signed current founder/OWNER makes an attestation.
  This is not completion or verification; it creates only founder-assertion
  evidence.
- **RESULT_CAPTURED:** opaque evidence references are reloaded inside the
  server-derived tenant/company scope. The action advances only when every
  non-test evidence type required by that action is present.
- **VERIFIED:** a separate internal verifier revalidates current evidence and
  uses the Verification service to move a pilot-specific record through
  Proposed, Executed, Tested, and Verified. There is no customer HTTP route for
  this transition.

Every Company Brain version records timestamps, last actor/authority, and a
safe action history. Runtime separately records the job, founder-only approval
where required, budget reservation/settlement, canonical events, and audit.

## Evidence classes

The implementation keeps four meanings separate:

| Evidence class | Meaning | Can it independently verify? |
| --- | --- | --- |
| Founder attestation | The founder says the scoped step was completed | No |
| Artifact reference | A scoped, hashed broker artifact such as a screenshot or authority result | Only an authority-classified artifact can satisfy authority evidence; a screenshot cannot |
| Provider receipt | A scoped, successful durable receipt for the exact pilot capability and operation | It satisfies the provider-result portion only |
| Business Builder test result | A verifier-created result for `cleaning-founder-action-review` | Only when all other action-specific evidence is current |

Raw taxpayer IDs, bank numbers, credentials, tokens, policy secrets, and source
documents are not stored in the action record. It stores opaque references,
content digests, evidence types, issuer metadata, and timestamps.

Evidence is immutable after capture. Reuse of an idempotency key with a
different command is rejected. Verification rechecks artifact availability,
expiration, content hash, authority provenance, provider receipt status,
provider operation, tenant, company, and founder ownership. A changed or
revoked source invalidates the accepted result rather than silently preserving
Verified.

## Actions and destinations

| Action | Prepared destination | Required evidence before Verification |
| --- | --- | --- |
| Entity/admin | Texas Secretary of State business services | founder attestation + authority confirmation + Business Builder test |
| EIN/tax ID | IRS EIN guidance/application entry point | founder attestation + authority confirmation + Business Builder test |
| Bank | FDIC BankFind for institution research, then founder-selected bank | founder attestation + scoped provider receipt + Business Builder test |
| Insurance | Texas Department of Insurance lookup, then selected licensed provider | founder attestation + scoped provider receipt + Business Builder test |
| Licenses/permits | Texas Business Permit Office and City of Denton resources | founder attestation + authority confirmation + Business Builder test |
| Domain | Business Builder provider-connection handoff | founder attestation + scoped registrar receipt + Business Builder test |
| Business email | Business Builder provider-connection handoff | founder attestation + scoped email-provider receipt + Business Builder test |
| CRM | Business Builder provider-connection handoff | founder attestation + scoped CRM receipt + Business Builder test |
| Scheduling | Business Builder provider-connection handoff | founder attestation + scoped scheduling receipt + Business Builder test |
| Payments | Business Builder provider-connection handoff | founder attestation + scoped merchant-provider receipt + Business Builder test |
| Legal name/address | Internal founder attestation | founder attestation + Business Builder test |

Official links are handoff destinations, not legal advice or proof that a
filing or license is required. Current requirements still need confirmation by
the relevant authority.

## Customer API

For each known pilot action:

- `GET .../residential-cleaning-pilot/founder-actions/{action_id}`
- `POST .../{action_id}/explain`
- `POST .../{action_id}/launch`
- `POST .../{action_id}/complete`
- `POST .../{action_id}/evidence`

All POST routes require a stable idempotency key and the current founder/OWNER.
The evidence route accepts only bounded `artifact` and `provider_receipt`
references. Caller-supplied actors, authorities, evidence classifications,
provider results, and verification results are not accepted.

## Test-only verifier and readiness

`DeterministicResidentialCleaningEvidenceVerifier` exists only for local and
isolated PostgreSQL proofs. Production composition does not enable it by
default. The customer cannot invoke it, and it cannot replace missing founder,
authority, or provider evidence.

Build Room projects the checklist, destination, missing evidence, captured
references, timestamps, history, and Verification result from the real backing
records. Ready and Fully Set remain decisions of the existing Verification
evaluator. Completing or verifying one administrative action cannot set either
flag while other policy gates remain unmet.
