# Orders, Billing, and Entitlements

Status: provider-neutral commercial domain with in-memory and SQLite offline adapters. No live checkout, payment, subscription, invoice, or refund provider is connected.

## Commercial ownership

This subsystem owns products, package versions, immutable order facts, opaque payment references, subscriptions, refunds/cancellations, and entitlement grants. It references `tenant_id`, `user_id`, and `company_id`. It does not own Company Brain state, verification, readiness, artifacts, or Runtime jobs.

Billing state never writes business facts into Company Brain. Payment, an active subscription, or an entitlement can make fulfillment eligible; none can make a company Ready or Fully Set. Verification remains authoritative for both labels.

## Product and package model

| Code | Customer name | Billing | Initial machine-readable capability set |
|---|---|---|---|
| `BUILD_WEBSITE` | Build My Professional Website | One-time | Website fulfillment plus customer-owned source/artifact export |
| `BUILD_BUSINESS` | Build My Business | One-time | Website and business assembly plus customer-owned company/artifact export |
| `BUILD_AND_RUN` | Build & Run My Business | Recurring | AI workforce, inbox, monitoring, scheduled outreach, optimization, managed support, plus customer-owned read/export |

`ProductVersion` freezes the package and feature definitions bought by an order. `OrderItem` stores the product version, package-name snapshot, billing mode, quantity, and optional amount. The catalog deliberately has no final price: `price_ref` is `None`, because the current product document contains pricing hypotheses rather than approved prices.

`BillingProvider`, `CheckoutProvider`, `SubscriptionProvider`, `InvoiceProvider`, and `RefundProvider` are ports. A future provider adapter owns provider SDKs and translates its identifiers into opaque references. Domain models do not mention Stripe or any other provider.

## Order lifecycle

Normal one-time flow:

`DRAFT → PENDING_PAYMENT → PAID → FULFILLMENT_PENDING → ACTIVE → COMPLETED`

Typed exits include `PAYMENT_FAILED`, `CANCELED`, `PARTIALLY_REFUNDED`, and `REFUNDED`. A failed initial payment may later become paid. Refunds may follow fulfillment or completion. Each state change appends a new Order version; prior order facts and item snapshots are never overwritten.

`CheckoutIntent` is idempotent within tenant/company scope. `PaymentIntentRef` stores only an opaque provider reference and status, never card or bank data. `RefundRecord` and `CancellationRecord` are append-only facts. Refund policy is represented by references/configuration and is not invented in code.

## Entitlements

An `Entitlement` maps a versioned package feature to a capability. An `EntitlementGrant` binds it to tenant, user, company, source order, optional subscription, status, and history.

Statuses are `ACTIVE`, `EXPIRING`, `EXPIRED`, and `SUSPENDED`. Grants have one of two classes:

- `CUSTOMER_OWNED`: durable read/export rights for customer-owned outputs and data.
- `STROMATION_MANAGED`: eligibility for ongoing fulfillment or managed operations.

The Runtime entitlement guard fails closed: a capability is eligible only with an ACTIVE grant, or an EXPIRING grant whose effective end remains in the future. Eligibility still does not bypass Runtime permission, approval, budget, verification, or founder-action gates.

## Persistence and security

`CommercialRepository` is the storage port. `InMemoryCommercialRepository` is deterministic and tenant-safe. `SQLiteCommercialRepository` stores versioned aggregates as append-only rows, indexes tenant/company scope, persists webhook idempotency, and protects audit rows from update/delete. This is an offline adapter designed for later PostgreSQL migration; nothing is deployed.

No model stores card numbers, bank numbers, raw authentication credentials, raw provider webhooks, secret keys, or provider access tokens. Provider event/payment/subscription identifiers are opaque strings only.
