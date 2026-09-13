# Run My Business Lifecycle

## Active service

An active `BUILD_AND_RUN` subscription grants explicit managed-operation capabilities such as bounded AI workforce execution, autonomous inbox handling, monitoring, scheduled outreach, ongoing optimization, and managed support. It also grants durable customer-owned read/export capabilities. The entitlement gate makes a capability eligible; Runtime permissions, approvals, budgets, founder-only decisions, identity verification, and payment authorization remain mandatory.

## Normal cancellation

1. OWNER requests cancellation; policy defaults to period end.
2. Subscription becomes `CANCEL_AT_PERIOD_END`; renewal becomes `WILL_CANCEL`.
3. Managed grants become `EXPIRING` with the paid-through timestamp and remain eligible until then.
4. At period end the scheduler hook moves the subscription to `CANCELED` and managed grants to `EXPIRED`.
5. Runtime denies new managed operational jobs.
6. Customer-owned read/export grants remain ACTIVE. No Company Brain state or customer-owned artifact is deleted.

## Payment failure

1. A normalized recurring payment failure changes the subscription to `PAST_DUE`.
2. A configurable grace period begins. Managed grants become `EXPIRING`, with the grace end recorded.
3. At the configurable restriction time, managed grants become `SUSPENDED`; Runtime denies new nonessential managed operations.
4. If unresolved at grace end, the subscription becomes `SUSPENDED`.
5. A later normalized ACTIVE update clears grace and reactivates managed grants.

The offline defaults are a seven-day grace period and automation restriction after two days. These are engineering defaults for tests, not approved commercial terms. Legal/product must set the production values and customer notices.

## Immediate security stop

Confirmed fraud, abuse, or security risk may use the policy-controlled immediate suspension path. It requires an authorized OWNER context and a reason, appends cancellation/audit facts, suspends managed grants, and emits an operations-suspended event. It does not grant support or staff authority to spend, approve founder decisions, impersonate the founder, delete data, or seize customer accounts.

## Customer retains

Where applicable, cancellation or nonpayment does not remove:

- Website source/export and customer-owned hosting/domain access
- Domain ownership
- CRM/customer data and account exports
- Brand assets and business documents
- Verification evidence and handoff material
- Company history and canonical Company Brain state

The customer retains read/export access through `CUSTOMER_OWNED` entitlements. Retention duration and privacy deletion requests remain a future legal/product policy; subscription cancellation alone never triggers destructive deletion.

## Managed operations that stop

After entitlement expiry/suspension, new ongoing AI workforce work, autonomous inbox handling, recurring monitoring, scheduled outreach, optimization jobs, managed support, and Stromation-funded third-party services are ineligible. In-flight job drain/cancel semantics must be finalized with Core Runtime and the live provider adapters before launch. Customer-funded third-party subscriptions are disclosed and remain under customer control.

## Refunds and fulfillment

Full and partial refunds, cancellation before fulfillment, cancellation after fulfillment begins, period-end recurring cancellation, and immediate security suspension have typed records. Final legal refund/revision terms are not encoded. Refund receipt does not automatically erase delivered assets or Company Brain history; a future policy engine decides unstarted fulfillment cancellation, work already performed, pass-through costs, and notices.
