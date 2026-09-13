# Commercial Event Model

## Boundary

Provider-specific webhook authentication, signature verification, parsing, and field mapping belong in a future provider adapter implementing `BillingEventTranslator`. Only `NormalizedBillingEvent` enters commercial domain logic. Raw provider payloads are explicitly rejected.

Accepted normalized event names:

- `billing.checkout.completed`
- `billing.payment.succeeded`
- `billing.payment.failed`
- `billing.subscription.created`
- `billing.subscription.updated`
- `billing.subscription.canceled`
- `billing.refund.created`

Every normalized event carries an event ID, provider/event reference, tenant, user, company, time, and correlation ID. Relevant order, checkout, payment, subscription, amount, period, and status fields are explicit. The handler validates the event scope against stored commercial records.

Idempotency is keyed by `(provider, provider_event_ref)` and persisted by both adapters. Duplicate checkout completion, payment success/failure, renewal/update, cancellation, and refund delivery cannot append duplicate order versions or double-grant entitlements. A production adapter must combine event claiming and aggregate writes in a database transaction and retry incomplete processing.

## Outbound canonical events

| Event | Meaning | Runtime behavior allowed |
|---|---|---|
| `checkout.completed` | Checkout provider completed its checkout step | Observe only; not proof of payment |
| `order.paid` | Matching provider payment succeeded | Record commercial fact |
| `commercial.fulfillment.eligible` | A paid order may enter fulfillment | Runtime may create allowed jobs after all other gates |
| `order.payment_failed` | Initial payment failed | Do not begin paid fulfillment |
| `entitlement.activated` | One explicit capability grant is active | Capability becomes eligible, not automatically executable |
| `subscription.activated` | Recurring subscription exists | Observe and project recurring state |
| `subscription.payment_failed` | Grace policy started | Restrict according to grace timing |
| `subscription.updated` | Normalized recurring state changed | Re-evaluate eligibility |
| `subscription.cancellation_scheduled` | Service will end at period boundary | Preserve service until effective time |
| `entitlement.operations_expired` | Normal recurring service ended | Deny new managed-operation jobs |
| `entitlement.operations_suspended` | Payment/security rule suspended operations | Deny new managed-operation jobs |
| `order.refund_recorded` | Full/partial refund fact was appended | Apply separately configured fulfillment/refund policy |
| `order.canceled` | Authorized cancellation was recorded | Stop or avoid fulfillment according to typed timing |

`RuntimeCommercialEventSink` is the only concrete integration adapter. It translates the public `CommercialEvent` into the existing Runtime `Event` and publishes through `LocalEventBus`. Commercial code does not call Runtime orchestrator internals. `RuntimeEntitlementGuard` is a public fail-closed seam; adoption by the production job admission policy is future integration work.

Commercial payloads contain references and state only. They never emit `ready`, `fully_set`, or a verification result. Buying something is not proof it was built, tested, verified, handed off, or legally complete.

## Audit

The commercial ledger records actor/provider, tenant, company, timestamp, action, target, reason/source, and prior status where relevant for order creation/transitions, payment results, refund records, subscription changes, entitlement grants/transitions, cancellations, and security suspension. Provider webhooks retain opaque event references but not raw payloads.
