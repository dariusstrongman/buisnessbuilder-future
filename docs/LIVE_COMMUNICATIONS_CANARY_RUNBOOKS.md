# Live Communications Canary Operator Runbooks

These are future-live operational procedures. Today the system must remain SANDBOX, with both live gates false and only the deterministic emulator registered.

## A. Before canary

1. Confirm approved change ticket, founder approval, legal/security review, monitoring owner, and rollback owner.
2. Confirm exactly one tenant, company, provider connection, sender, purpose (`reply_to_inbound`), and one or two internal recipients.
3. Verify provider connection health and least-privilege scopes; never copy token values into the ticket.
4. Verify domain ownership, SPF, DKIM, DMARC, alignment, sender verification, and reputation from independent evidence.
5. Verify consent/inbound context, suppression/opt-out, callback authenticity, reconciliation, alert delivery, and retention policy.
6. Exercise global, tenant, company, provider, and email-channel kill switches in sandbox and record the test reference.
7. Confirm cap at no more than three total and one per hour, expiry no longer than one hour, and no wildcard recipient.

## B. Start canary

1. Two humans read back target scope, recipients, purpose, cap, start/expiry, monitoring owner, and rollback reference.
2. Privileged operator opens the approved server-side ceremony; customer UI, SUPPORT, agent, and callback paths are forbidden.
3. Re-run readiness immediately. Stop on any failed gate.
4. Record permit and approval IDs without raw recipient addresses; independently compare recipient digest.
5. Keep the global kill command and dashboard visible before admitting the first job.

## C. Monitor canary

1. Watch admission/execution decisions, provider receipts, callbacks, reconciliation backlog, bounce, complaint, suppression, rate pressure, and cap use.
2. Match every attempt to one Runtime job, permit reservation, ProviderReceipt, and delivery state.
3. Treat an uncertain provider result as reconciliation work, never permission to resend.
4. Stop at expiry, cap, any unexpected recipient, any complaint, or any unexplained record mismatch.

## D. Hard bounce

1. Engage company kill switch before investigation.
2. Verify callback authenticity and event deduplication.
3. Confirm immediate recipient suppression and high alert.
4. Do not retry. Inspect domain/sender reputation and recipient evidence; require a new ceremony to resume.

## E. Complaint

1. Engage global kill switch immediately for the tiny first canary.
2. Verify callback authenticity, then preserve complaint evidence and suppress the recipient.
3. Open security/compliance incident review; revoke the permit.
4. Do not resume until root cause, content, consent, and recipient-scope reviews are approved.

## F. Provider outage

1. Engage provider-connection kill switch and stop admissions.
2. Classify failures; preserve stable request IDs and receipts.
3. Schedule bounded reconciliation for uncertain results. Never blind-retry sends.
4. Resume only after provider health, token, callback, and backlog checks pass under a new permit if expired.

## G. Credential compromise

1. Engage global and provider-connection kill switches.
2. Revoke provider connection and broker secret reference; do not expose or paste token material.
3. Rotate provider credentials through the approved vault path and inspect audit/provider activity.
4. Require security incident closure, reconnect, and a new ceremony.

## H. Unexpected recipient or send

1. Engage global kill switch immediately.
2. Preserve job, permit, digest, receipt, and audit references; do not collect extra PII.
3. Verify there was no provider action. If there was, escalate as a critical privacy/security incident.
4. Revoke the permit and inspect scope/signature/cap enforcement before any resumption.

## I. Kill all outbound

1. Use the trusted platform path to engage the global kill switch with an incident reference.
2. Confirm new and queued jobs fail their immediate pre-provider recheck.
3. Confirm provider action counts no longer increase and alert/metric/audit records exist.
4. Revoke active permits and provider connections where compromise is suspected.

## J. Rollback

1. Engage global kill, revoke the canary permit, and disable the provider connection if needed.
2. Drain or cancel queued nonessential work through Runtime; do not delete customer-owned state.
3. Reconcile every uncertain receipt; preserve compliance evidence.
4. Confirm rollout and live gates are off, sandbox adapter is the only usable adapter, and record rollback completion.

## K. End canary

1. Stop at planned expiry or cap; do not extend or renew a permit.
2. Reconcile all receipts/delivery states and verify no queued work remains eligible.
3. Engage company send pause until post-canary review.
4. Archive safe metrics, alerts, approvals, and evidence references.

## L. Post-canary review

1. Account for every permitted attempt and recipient digest.
2. Review delivery, defer, bounce, complaint, suppression, callback, auth, cap, duplicate, and reconciliation results.
3. Document exceptions and whether rollback worked within target time.
4. Require explicit approval for the next canary; never carry the prior permit or allowlist forward silently.
