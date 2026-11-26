# Production Mail Readiness Implementation Plan

**Goal**

Move Dealroom from protected staging toward a creator-mail product that handles bursts without losing accepted mail, repeating approved sends, or weakening negotiation quality.

**Architecture**

PostgreSQL remains the authoritative inbox, business state and queue. The verified relay submits signed mail. One transaction records the delivery identity, message and analysis job before acknowledgement. Replicated workers use fenced leases, per-thread ordering and persistent LangGraph checkpoints. Creator approval binds the exact outgoing envelope. The relay must use a durable idempotency key and give authoritative acceptance evidence.

**Tech stack**

PostgreSQL 17, FastAPI, LangGraph, LangChain, Langfuse, Next.js, Railway, and a provider-neutral identity and mail relay boundary.

**Specification**

The [production-readiness audit](../../production-readiness-audit.md) and latest verified audit findings.

## Capacity assumptions and acceptance

- Provisional test envelope pending actual traffic. 10,000 messages per day across tenants and 20 inbound requests/second for five minutes. Test one hot thread separately because ordering limits its concurrency.
- Starting staging targets. Ingress acknowledgement p95 below 300 ms, no lost acknowledged events or duplicate downstream effects during replay and process failure, first draft p95 below 60 seconds at steady state, and burst backlog drained within 10 minutes. Verify provider rate and token quotas separately.
- At 20 qualified turns/s and 2.5 s mean processing time, about 50 analysis slots keep pace before headroom. Run the queue benchmark and the live-model load test independently. Provision delivery and eval capacity separately from analysis.
- Keep PostgreSQL while measured latency, drain time and database connection, CPU and I/O budgets hold. Introduce a managed broker only if measured contention, fan-out or isolation justifies it. Publish opaque job IDs from the committed outbox, preserve thread order, and deduplicate on the consumer.
- Work is processed at least once. Stable IDs and fences protect business effects. External delivery also depends on relay-enforced idempotency and authoritative receipts. Restore the database with the relay disabled until receipts are reconciled.
- Hosted credential changes, deployment, live provider tests, and production data usage require separate authorization. All repository and workload verification uses synthetic data in isolated schemas.

## Implementation sequence

### 1. Durable mail queue and release drain

- [x] In storage and mail admission, preserve demo quotas and remove creator lifetime limits. Add a per-workspace database-backed UTC daily limit with no reset work on the request path. Replays return the original receipt even when the limit is reached.
- [x] Bound short-transaction connection pools per API and worker process. Keep session-scoped locks on dedicated connections and expose pool waiters in protected health.
- [x] Add partial indexes for ready jobs, expired leases and unresolved predecessors. Isolate analysis and delivery capacity and ensure the existing combined worker can remain during transition.
- [x] Verify per-thread order, parallel claims across threads, worker death and lease fencing, duplicate signed relay event handling, and replay after completed draft publication. Keep idempotent stable job IDs.
- [x] Report queued and running jobs by stored workflow/model identity in the protected operations API. Document a drain procedure and test the case where a newly configured worker cannot claim older work.
- [x] Use concurrent synthetic relay admission and worker claims to measure unique acknowledgements, replay count, queue drain and connection cost. Keep certified staging capacity pending until the representative workload is tested.

### 2. Negotiation quality and regression gates

- [x] In domain policy and the reply renderer, distinguish missing currency from a known currency a creator cannot quote in. Test a known EUR offer under a USD policy.
- [x] In the evaluator, derive private-floor checks from the actual fixture policy and distinguish disclosure from legitimate public quote text. Cover an alternate floor and use critical assertions for material terms and unintended commitments.
- [x] Record fixture and execution identity with results. Define a human review protocol for live draft usability, material omissions and unsafe commitments. Include representative briefs, quoted messages, currencies, multilingual offers and scope changes.

### 3. Permissioned brief retrieval

- [x] Preserve bundled demo evidence. Add tenant-scoped registered briefs with versioned sections, revocation, bounded inputs and provenance locators. Bind lookup to the thread tenant before reading content.
- [x] Verify cross-tenant isolation, revisions, revoked references, abstention and inspection. Keep arbitrary external fetches outside the model workflow.

### 4. Data lifecycle and operations

- [x] Implement an operator-invoked retention workflow for eligible closed creator negotiations. Preview counts, remove associated checkpoints, and protect ambiguous deliveries and queued work. Preserve deduplication metadata over the provider replay window.
- [x] Update architecture and operations guides with retention scope, lock boundaries, queue health, rollout order, backups, alert ownership, and reconciliation before restored delivery.
- [x] Run local backend tests, browser tests, production frontend build, scripted evals, Docker upgrade and restore, and local load verification.
- [ ] Gather hosted staging, live quality and governance evidence before assigning a 9/10 launch rating.

## Local load evidence

The initial connection-per-transaction workload exhausted loopback TCP ports during the 6,000-event run. The bounded pool revision handled the same workload with 16 concurrent clients across 60 workspaces. It acknowledged 7,200 submissions, including 1,200 replays, with 6,000 unique queue jobs and 6,000 successful claims and acknowledgements. Local database admission p95 was 30.9 ms. The claim-and-ack phase took 8.69 s with 16 pooled connections and zero checkout errors. A separate one-workspace, one-thread run acknowledged 720 submissions into 600 unique jobs. Its database admission p95 was 66.3 ms and its claim-and-ack phase took 4.83 s.

The harness invokes repository admission and queue claims. It excludes HTTPS transport, model inference, delivery and hosted network latency. Reproduce it and measure each missing boundary in staging before changing capacity assumptions. [Load harness and operations](../../operations.md#load-and-capacity-verification)

## Release gates

| Gate | Required evidence |
| --- | --- |
| Intake and delivery | Duplicate callbacks return the original receipt. Accepted work survives restart. No duplicate drafts or handoffs after replay, lease loss or relay timeout. |
| Staging | Real gateway and relay verified, capacity test meets objectives, monthly latency and failure objectives monitored, hosted backup restore exercised. |
| AI quality | Versioned scripted checks plus a blind, human-labeled live benchmark calibrated with a negotiation reviewer. |
| Privacy | Approved creator retention and deletion process across database, backups, relay, model provider and Langfuse. |
| Operations | A release owner can drain old jobs, verify healthy workers, reconcile ambiguous sends, and restore with known recovery point and elapsed time. |

[PostgreSQL 17 locking](https://www.postgresql.org/docs/17/sql-select.html) · [Transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html) · [Google SRE service objectives](https://sre.google/workbook/implementing-slos/)
