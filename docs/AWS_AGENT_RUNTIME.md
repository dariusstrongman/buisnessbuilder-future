# AWS Agent Execution / Run My Business Runtime v1

## Authority and flow

The AWS execution layer is a bounded adapter under the existing Runtime. It is
not an orchestrator and never creates a permanent process for a customer.

```text
trusted customer or persisted canonical trigger
  -> Identity/company scope validation
  -> active BUILD_AND_RUN grants
  -> AI Workforce exact role/capability/action decision
  -> model eligibility and cost ceiling
  -> JobOrchestrator creates the canonical Job
  -> Runtime approval and budget reservation
  -> Runtime Job + reservation + queue outbox commit together
  -> SQS shared queue
  -> shared ECS/Fargate worker lease
  -> all current authority revalidated
  -> one bounded capability invocation
  -> result, spend settlement, audit, and canonical result event
  -> SQS acknowledgement
```

Identity owns users and organizations. Commercial owns `BUILD_AND_RUN` status.
The AI Workforce policy owns exact role grants. Runtime owns admission, approvals,
budgets, job transitions, idempotency, audit, and canonical events. Company Brain
is read through its public service. Verification receives requests only through
its public Runtime adapter and remains the only readiness authority.

## Credential-free job envelope

`agent-job.v1` includes tenant, company, job, correlation and causation IDs;
capability/version and action; agent role/version; exact entitlement, permission,
and approval references; reserved and maximum spend; model policy and selected
eligible provider/model; input artifact references; retry metadata and stable
idempotency key; trigger provenance; policy evaluation/digest; and creation and
expiry timestamps.

The queue envelope intentionally contains no arbitrary job input, authentication
token, provider secret, AWS credential, customer password, or external-account
credential. The worker loads the canonical Runtime job by the envelope's exact
tenant/company scope and rejects any digest or scope mismatch.

## Entitlement and cancellation policy

New work requires current `ACTIVE` managed grants originating from a
`BUILD_AND_RUN` order. The role mapping additionally requires:

| Role | Required managed grants |
| --- | --- |
| Intake Assistant | `ai_workforce.execute` |
| Quote Drafting Assistant | `ai_workforce.execute` |
| Inbox Assistant | `ai_workforce.execute`, `inbox.autonomous` |
| Review Follow-up Assistant | `ai_workforce.execute`, `outreach.scheduled` |

`EXPIRING` never admits new work. A previously admitted job may drain only while
the paid-through timestamp remains current and the envelope predates the
entitlement transition. `EXPIRED` and `SUSPENDED` reject immediately. The
Runtime cancellation sweep cancels safe queued/retryable work and releases its
reservation. No commercial transition deletes Company Brain or customer-owned
entitlements.

## Delivery and recovery

- Queue publication is driven by a PostgreSQL transactional outbox with stable
  message IDs, leasing, retry, and acknowledgment.
- SQS delivery is at least once. A database execution lease serializes workers.
- The worker renews SQS visibility immediately before capability execution.
- Provider calls carry attempt-scoped idempotency keys. The deterministic
  staging provider performs no external action and replays completed requests.
- A crash before execution is reclaimed after the worker lease. A crash after
  committed execution but before SQS acknowledgment is recognized as a
  duplicate and cannot emit another result event or settle spend twice.
- Retryable refusal releases the reservation and records zero spend. Retries are
  bounded by Runtime and repeated queue failures are sent to the SQS DLQ by its
  redrive policy.

## Model routing

The model router first filters by allowed provider, required capability and tool
or vision support, quality floor, availability, health, quota, latency policy,
currency, and remaining job budget. Only then can measured/list cost rank the
eligible set. Credits cannot make an insecure or under-quality model eligible.
This branch wires only `deterministic-test/bounded-v1`; it does not change
Stromation routing or connect a live model provider.

## Scheduling and worker shape

`RuntimeScheduler` persists due schedules and emits one canonical occurrence
event. It never executes work. `EventBridgeSchedulerAdapter` is a production-
shaped adapter that sends only an opaque schedule reference and uses AWS's
minute-or-longer rate schedule, avoiding busy loops.

`python -m businessbuilder.agent_runtime.worker` is a shared one-message worker.
It uses the ECS task role credential chain, PostgreSQL Runtime state, and SQS
long polling. It is explicitly disabled in production while its only registered
execution provider is the deterministic staging adapter.

## Remaining production work

The capability-scoped secret resolution, artifact authorization, and durable
provider receipt boundary is implemented in `SECRET_ARTIFACT_BROKER.md`. Before
live customer execution, add reviewed live provider adapters, upload quarantine
scanning, workload-identity enforcement for the broker boundary, observability
and alarms, and a reviewed policy for maximum execution duration and in-flight
drain. Those additions must retain the envelope and Runtime authority boundaries
defined here.
