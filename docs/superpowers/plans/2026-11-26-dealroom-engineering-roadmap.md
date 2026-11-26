# Dealroom Engineering Improvement Plan

> **Execution:** Use Superpowers executing-plans or subagent-driven-development. Keep each task independently reviewable and preserve existing working-tree changes.

**Goal**

Reach a defensible 9/10 production readiness rating by proving the real email workflow, making high-risk code easier to change, and measuring whether creators can use the drafts as written.

**Architecture**

PostgreSQL owns inbound mail, business state, approvals and the durable queue. LangGraph runs bounded, versioned analysis. The model extracts commercial terms, deterministic code owns decisions, and an approved relay performs delivery. Keep analysis, delivery and evaluation capacity independently adjustable.

**Tech stack**

FastAPI, PostgreSQL 17, psycopg, LangGraph, LangChain, Langfuse, Next.js and Playwright.

**Spec**

This plan follows the [production mail implementation plan](2026-11-25-production-mail-readiness.md), the [operations runbook](../../operations.md) and the [held-out live review](../../live-negotiation-review.md).

## Starting point

The last local run covered 112 backend tests, 17 browser tests, 33 scripted cases, and a local migration, worker recovery and database restore drill. The database harness admitted 7,200 submissions into 6,000 unique queue jobs at 30.9 ms p95. It bypassed HTTPS, model inference and mail delivery. Treat those paths as unmeasured until an end-to-end drill runs.

Two code paths deserve early attention. A first relay send runs inside a transaction holding negotiation and tenant locks in mail_delivery.py. A slow relay can delay unrelated writes for that creator. The thread snapshot bounds its timeline but still loads the complete message, draft and receipt histories in read_models.py. Test the impact of both before broad changes.

## Global constraints

- Preserve creator approval of the exact envelope. Keep credentials, pricing authority and delivery outside the model.
- Preserve stable event and approval IDs, per-thread ordering, lease fencing, deduplicated business effects and reconciliation after an ambiguous send. Queue retries may repeat processing.
- Version compatible workflow, prompt, fixture and API changes. Keep existing jobs, checkpoints and read clients operable during an upgrade.
- Use synthetic data for CI and local load. Enable live provider traffic, credential changes and hosted release checks only with explicit approval. Keep customer payloads out of trace exports.
- Keep PostgreSQL while measured load fits its budget. Revisit a managed broker when queue activity persistently breaches agreed database or latency objectives after tuning. A second agent, model call, or vector index needs a measured quality improvement.

## Review focus

- Relay stalls before commit, accepts a handoff but loses its response, or reports an ambiguous result after restart.
- A creator edits policy, revokes a route or receives a new message while delivery is starting.
- A hot workspace competes for workers and connections during replay, a provider throttle, or a rollout.
- Replies, edits and receipt updates arrive while an operator pages through a long conversation.
- A fluent draft misses a material term, cites a revoked brief, or makes an unintended commitment.

## Implementation sequence

### 1. Build a release candidate baseline

**Priority P0 · Owner backend and release engineering · Dependency none**

**Files**

Update CI configuration, scripts/release-smoke.sh and operations.md. Add a release evidence template under docs.

- [ ] Capture the intended changes with an approved candidate SHA. Record the baseline report, migration versions, workflow/model identity, dependency lockfiles and fixture fingerprints.
- [x] Extend CI to execute the scripted release gate explicitly and publish the result for the candidate SHA. Preserve the current quick PR checks. Run the larger load and recovery drill on the release candidate with an isolated database.
- [x] Run the candidate and prior image against the upgraded schema. Seed queued work for both identities and prove compatible workers drain it. Include a deferred job and an already approved delivery.
- [ ] Publish a release evidence record with test results, rollback instructions, and clearly identified missing staging evidence.

**Acceptance:** A second engineer can reproduce the candidate from a clean checkout. CI fails if a critical assertion fails or queued work becomes unserviceable during the upgrade drill.

### 2. Make mail delivery contention and failure behavior explicit

**Priority P0 · Owner backend and mail integration · Depends on task 1 baseline**

**Files**

Start with mail_delivery.py, storage.py, operations.py, test_real_mail.py and test_production_queue.py. Add a concurrency test near the existing relay tests.

- [x] Write a blocking fake relay test. Pause the network call after handoff starts, then measure independent inbound admission in the same workspace and verify approval integrity. Add worker interruption before the call, lost response after provider acceptance, and route revocation at the handoff boundary.
- [x] Record the current lock order and transaction boundaries. Define the precise point at which a send is authorized and becomes irreversible. Close, revoke and policy change must stop a handoff that has not crossed that boundary.
- [x] If the test shows tenant-wide contention, implement a short transaction that validates the exact bound envelope and durably commits the handoff, then moves provider I/O outside business transactions. Use a final short transaction to record the receipt. Treat records written by older workers as lookup-only when their dispatch history is ambiguous.
- [ ] Require the relay to bind its idempotency key to the exact envelope and support authoritative reconciliation. A generic missing receipt remains unresolved. If pre-dispatch interruption needs automatic recovery, first prove that the relay can safely resume the same key without producing a second effect.
- [x] Report handoff latency, unresolved attempt age and reconciliation outcomes. Confirm all external delivery evidence is checked without logging payloads.

**Acceptance:** The concurrency suite proves exact approval binding and zero duplicate sends in injected failure cases. An unrelated conversation can admit mail while a deliberately slow relay is blocked, or the measured contention is documented as a pilot limit until the relay contract supports a safe redesign.

### 3. Bound reads and make the code easier to change

**Priority P1 · Owner backend and frontend · Start after task 1; coordinate edits with task 2**

**Files**

Start in read_models.py, api_contracts.py, storage.py, mail_delivery.py, frontend/lib.ts and frontend/components/negotiation-thread.tsx.

- [x] Create a fixture with at least 500 turns and multiple draft revisions per turn. Measure snapshot response bytes and query/connection counts. Add per-conversation paging for older history using a stable revision cursor and consistent page boundaries. Keep the active draft, unresolved delivery and recovery actions in the current view.
- [x] Add backend and browser cases for paging while new turns arrive, avoiding duplicates and gaps, cross-workspace access, keyboard focus recovery, and delivery receipts retained with the correct revision. Introduce the paged contract additively and test old browser clients during rollout.
- [x] Add characterization tests before moving SQL. First extract negotiation review and approval commands from Store. Then separate relay transport and receipt parsing from the mail persistence workflow. Keep existing transaction entry points or thin compatibility adapters while callers migrate.
- [x] Type the records crossing each extracted boundary and remove duplicate conversion at those call sites. Keep lock order and atomic writes documented beside the owning transaction. Add focused unit tests for parsers and deterministic decisions, with PostgreSQL tests for invariants.

**Acceptance:** Initial history response size is bounded regardless of total history, active actions remain available, public behavior stays compatible, and a code reviewer can follow one transaction from command to audit event without tracing through a new abstraction layer.

### 4. Build a live quality feedback loop

**Priority P0 · Owner AI engineering and negotiation reviewer · Run alongside tasks 2 and 3**

**Files**

Extend backend/evals/fixtures, evaluations.py, docs/live-negotiation-review.md and the evaluation result view when a new metric needs display.

- [ ] Define a versioned rubric and use the existing 10-case held-out set for the first blind live pass after provider approval. Add representative authorized, de-identified threads after the pilot starts. Record the first failure per turn before changing prompts or model settings.
- [ ] Keep objective gates for material amounts, terms, scope, approvals and floor disclosure. Use human labels for draft usability and tone. Split results by locale, thread length, rights ambiguity and brief availability, and record denominators. Report shortfalls in smaller slices as insufficient evidence.
- [ ] Compare candidates on the same dataset and explicit workflow, prompt, retrieval and model identifiers. Bind document provenance to the commercial terms it supports. Fix the top measured failure type, then rerun the full release gate.
- [x] Capture privacy-safe pilot signals such as sent as written, materially edited, discarded, elapsed time to reviewable draft and estimated cost per usable draft. Keep commercial text in the authorized workspace.

**Acceptance:** Zero observed critical failures, at least 90% usable as written on the agreed live launch set, and a documented review of any meaningful regression by slice. Document the sample size and distinguish the initial 10-case run from broader pilot evidence.

### 5. Turn staging validation into an operational release gate

**Priority P0 · Owner release engineering and on-call · Depends on tasks 1 and 2 and the first live review**

**Files**

Extend scripts/queue-load.py or add a dedicated HTTPS staging harness. Update docs/operations.md, docs/real-mail-integration.md and docs/creator-retention.md.

- [ ] Exercise the gateway and relay over signed HTTPS with stable duplicate events, controlled retryable throttles, ordered long threads and independent tenants. Measure ingress acknowledgement, queue age, model duration, draft visible time, pool checkout wait and delivery/reconciliation latency independently.
- [ ] Test the provisional 20-request/s five-minute burst and 10,000-message/day sizing assumption in the approved staging environment. Include a separate hot-workspace run, worker loss and restart, old and new execution identities, provider quota and slow relay scenarios.
- [ ] Prove the on-call can see a missing compatible worker, delayed model-budget work, a growing oldest-ready queue and aged ambiguous delivery. Page the owner in a drill. Restore managed PostgreSQL to a separate environment with mail disabled and reconcile all applicable delivery keys before resuming.
- [ ] Run one end-to-end retention drill covering application rows, checkpoints, provider and trace copies, and backup expiry behavior. Record who verifies each system and how long a replay remains resolvable.

**Acceptance:** Zero lost acknowledged messages and zero duplicate sends in the drill. Ingress p95 below 300 ms, steady-state first draft p95 below 60 seconds, and burst backlog drained within 10 minutes. Treat these as provisional gates until staging accepts or revises them. Record agreed availability and recovery objectives, restore duration, and observed data-loss window.

### 6. Prepare a bounded creator pilot

**Priority P1 · Owner product and operations · Depends on tasks 4 and 5**

- [x] Document onboarding and removal of a workspace, route setup and revocation. Provide a clear error or recovery path for a creator when a brief is unavailable, a delivery outcome is unknown, or a daily limit is reached.
- [ ] Verify the approval and recovery flow with keyboard navigation and a screen reader. Exercise a creator role with read access only and verify the interface guides them appropriately.
- [ ] Agree pilot scope, escalation owner, success metrics and a stop procedure. Halt promotion for a critical quality failure, lost accepted mail, a duplicate send, or a failed restore drill. Review the first pilot exceptions alongside the evaluation results.

**Acceptance:** A second operator can onboard and remove a workspace, a creator can proceed and recover without engineering intervention for expected cases, and every launch decision has linked evidence.

## Implementation evidence

| Workstream | Repository evidence | Open release evidence |
| --- | --- | --- |
| Release qualification | CI scripted gate and candidate artifacts. Local mixed-worker drill includes ready and deferred prior jobs and a prior-approved delivery. | Approve and qualify an exact clean release SHA in CI. |
| Handoff | A blocking relay regression proved same-workspace ingress contention and now passes. The commit binds the immutable envelope before network I/O. Concurrent edits after that commit cannot cancel it. Existing and interrupted attempts remain lookup-only. | Prove the configured relay's idempotency and reconciliation against its upstream provider. |
| History and code quality | Paged browser history cut the 500-turn fixture from 2,479,597 to 74,756 bytes. It retains compatibility for unpaged clients and adds read-only creator guidance. Draft review and relay transport are separate modules. | Manually rehearse the creator approval and recovery flow with a screen reader. |
| Quality and pilot | Versioned blind-review manifest, offline launch-gate report and privacy-safe aggregate pilot metrics. | Obtain a human-reviewed live run and use observed failures to select the first model or prompt improvement. |
| Staging and recovery | The signed loopback probe processed 144 requests into 120 unique jobs with one receipt per event. The local qualification workload claimed all 6,000 unique jobs from 7,200 submissions. Local protected health exposes aged confirmations. | Run the hosted HTTPS load, real provider, alerting, managed restore and data retention drills. |

The [release evidence guide](../../release-evidence.md), [staging runbook](../../operations.md), [live quality review](../../live-negotiation-review.md) and [creator pilot runbook](../../creator-pilot.md) explain how to reproduce the checks.

## First reviewable change

Start with the release evidence baseline and the controlled slow-relay concurrency test. They establish whether the highest-risk boundary needs a transaction redesign and make later refactors easier to validate. Run the live review in parallel once a staging provider budget is approved.

[Anthropic workflow design](https://www.anthropic.com/research/building-effective-agents) · [Hamel Husain and Shreya Shankar on evaluation and error analysis](https://hamel.dev/blog/posts/evals-faq/index.html)
