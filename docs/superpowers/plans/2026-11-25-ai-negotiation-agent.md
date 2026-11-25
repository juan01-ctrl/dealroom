# AI Negotiation Agent Sandbox Implementation Plan

> Implement task by task using the Superpowers executing-plans workflow. Checkboxes track implementation and verification.

**Goal**

Ship a standalone sandbox that can analyze synthetic brand offers, draft policy-compliant responses, and continue a negotiation only through creator-controlled simulated delivery.

**Architecture**

Next.js calls a FastAPI application. PostgreSQL owns commercial state, tenant-scoped history and durable events. A worker consumes the transactional outbox and runs LangGraph for analysis and drafting. Approval and simulated delivery are application commands, so the graph and model have no delivery capability.

**Stack**

Next.js, React, TypeScript, FastAPI, Python, Pydantic, PostgreSQL, LangGraph with a PostgreSQL checkpointer, configurable OpenAI or Anthropic, LangSmith, and Pytest. Proposed hosting is Vercel for the UI and Railway for the API, worker, and PostgreSQL.

**Source brief**

`/Users/juanierace/Downloads/ai-negotiation-agent-brief.md`. This plan captures the working requirements for the standalone prototype.

## Constraints and design decisions

- Use synthetic data for the public demo and simulated inboxes for all delivery. Keep real messaging, payments, signing, autonomous commitments, and separate brokers outside this prototype.
- Store explicit commercial policies as immutable versions. Keep conversation history and inferred preferences separate. Promotion to explicit policy requires creator confirmation. Retrieve structured, tenant-scoped history by thread and relevance with a bounded context budget. Start with PostgreSQL queries and no vector index.
- Use integer minor units and a currency code for monetary values. Compute rate floors, line items, exclusivity and allowed actions in Python. Require clarification before calculating with ambiguous currency. Price advertising usage separately. Ask for missing scope and duration.
- Use the model for typed extraction and a structured draft proposal. Render commercial claims, quoted prices, rights language and action from validated fields. Use bounded copy choices for the sandbox editor, including tone and clarifying questions. Keep raw inbound content out of the outbound renderer and keep private policy floors out of the drafting input. An acceptance recommendation can express intent to proceed subject to terms and creator approval. It has no contract or deal-commitment effect.
- Treat each inbound message as a new turn with an immutable input and a negotiation revision. Keep a LangGraph checkpoint thread per analysis run and carry multi-turn commercial context in PostgreSQL. The application persists review state while the creator deliberates. LangGraph checkpoints resume interrupted analysis.
- Bind approval to the draft ID, version, digest of the rendered payload, negotiation revision, and policy version. A material edit, a newer inbound message, a policy update, or closure invalidates applicable unsent approvals. Recheck at delivery. A sent response remains historical.
- Use a scripted model for repeatable local runs and CI. The live-provider path is configurable and runs with server-side credentials and budgets. The public UI clearly labels scripted and live results.

## Files and milestones

| Milestone | Planned files | Exit condition |
| --- | --- | --- |
| 1. Reference negotiation | `compose.yaml`, `backend/pyproject.toml`, `backend/app/domain/{terms,policy,pricing}.py`, `backend/app/storage/{db,negotiations}.py`, `backend/migrations/`, `backend/app/{api,worker}.py`, `backend/app/workflow/{models,graph}.py`, `frontend/app/page.tsx` | A creator can receive the reference offer, review a draft, approve it, and see one simulated delivery. |
| 2. Recovery and isolation | `backend/app/storage/{outbox,delivery}.py`, `backend/app/workflow/checkpoints.py`, `backend/app/observability.py`, `frontend/components/{creator-profile,negotiation-thread,agent-inspector}.tsx` | The next brand reply retains prior terms. Restart, stale approval, duplicate input and delivery replay are covered against PostgreSQL. |
| 3. Evidence and release | `backend/app/evals/{runner,metrics}.py`, `backend/evals/fixtures/v1.json`, `frontend/app/evaluations/page.tsx`, `README.md`, `docs/architecture.md`, `docs/evaluation-results.json`, `docs/demo-walkthrough.md` | The interactive suite produces real results, and the release package includes a deployed URL and a 2–3 minute English walkthrough. |

## Task 1. Persist offers and implement commercial rules

- [ ] Define `OfferPatch`, `OfferTerms`, `PolicySnapshot`, and `Decision`. Implement `merge_terms(previous: OfferTerms, patch: OfferPatch) -> OfferTerms` so omitted terms keep their values, an explicitly withdrawn term is cleared, and ambiguity is surfaced for clarification. Implement `evaluate_offer(terms: OfferTerms, policy: PolicySnapshot) -> Decision` with acceptance, counteroffer, clarification, and rejection recommendations, checked line items, and missing fields.
- [ ] Bootstrap local PostgreSQL and versioned migrations. Create tenant-scoped negotiations, messages, offers, policy versions, separately stored inferred preferences, decisions, draft versions, approvals, timeline events and durable jobs. Use composite tenant-aware keys and verify ownership on every read and write. Store creator availability, preferences, commercial restrictions and history for the profile panel.
- [ ] Implement `ingest_message(tenant_id, negotiation_id, event_id, message) -> TurnReceipt`. Insert the message, increment revision, invalidate older unsent drafts, and enqueue `analyze_turn` in one transaction. Deduplicate on tenant and event ID. Identical replay returns the prior receipt. Reuse with a different payload returns a conflict. Require explicit reopen before analyzing a closed negotiation.
- [ ] Add `backend/tests/test_policy.py` and `backend/tests/test_ingest.py`. Assert that the reference USD 1,200 all-in offer yields a USD 2,000 base and USD 600 exclusivity, totaling USD 2,600 before usage, with a clarification request for usage scope and duration. Cover ambiguous currency, omitted versus withdrawn terms, currency rounding, rollback on enqueue failure, parallel replay, conflicting event payload and closed-thread input. Verify with `cd backend && pytest tests/test_policy.py tests/test_ingest.py -q`.

## Task 2. Build the first end-to-end negotiation

- [ ] Define `NegotiationModel.extract(email, context) -> OfferPatch` and `NegotiationModel.draft(public_decision, context) -> DraftProposal`. Add scripted and live adapters behind the same interface. Configure chosen provider, model, timeouts, bounded retries and token budget outside source. Reject invalid structured model outputs before publishing a draft.
- [ ] Implement the graph as load policy and scoped history → extract and merge → evaluate → compose and render → publish. Make `publish_draft(analysis_id, intake_revision, policy_version, proposal) -> DraftVersion` replay-safe and conditional on the current negotiation and policy versions. Store the offer and decision evidence with the draft. Keep model-facing context and public draft fields separate from private rule values.
- [ ] Implement `process_job(job) -> None` with handlers for `analyze_turn` and `deliver_draft`. Run the first local slice with one worker. Handle stale analysis and delivery jobs as superseded outcomes. Task 3 adds lease-based claiming and crash recovery without changing the handlers.
- [ ] Add FastAPI commands for creating and reopening a negotiation, inbound email, editing and rejecting drafts, approval, and reading the profile, thread and timeline. Resolve the tenant from a server-issued demo session. Use an expected version for mutations and return a conflict for stale actions. Poll for asynchronous updates in the three-panel UI. Implement explicit reanalysis after rejection and show the simulated brand inbox.
- [ ] Implement `approve_draft(tenant_id, draft_id, expected_version) -> Approval`. Inside one transaction, validate the draft, record approval, and enqueue `deliver_draft` with a stable approval-based key. Editing creates a fresh validated version that needs its own approval. Implement `deliver_draft(approval_id, delivery_key) -> DeliveryReceipt`. Lock the negotiation, recheck all approval bindings, and insert the simulated inbox message and delivery state in the same transaction using a unique delivery key. A replay of a completed delivery returns its existing receipt.
- [ ] Add `backend/tests/test_workflow.py` and `backend/tests/test_approval.py`. Assert the reference offer reaches `AWAITING_APPROVAL`, the inbox stays empty until approval, and then contains one USD 2,600 response with a usage clarification and no private floor. Exercise reject and reanalysis, an invalid edit, an edited approved draft, a send with no approval, simultaneous approvers and new inbound or policy change before delivery. Verify with `cd backend && pytest tests/test_workflow.py tests/test_approval.py -q`.

## Task 3. Prove recovery and make it visible

- [ ] Implement `claim_job(worker_id) -> JobLease | None` using short PostgreSQL claim transactions and `FOR UPDATE SKIP LOCKED`. Track attempts, lease expiry, ownership token and next eligible time. Reject stale acknowledgements, use capped backoff for transient failures, and provide explicit retry for terminal operational failures. Serialize graph writers for a given analysis ID with a bounded PostgreSQL advisory lock held on a dedicated connection. Retain revision checks on business writes. Process no model calls inside a business transaction. [PostgreSQL locking reference](https://www.postgresql.org/docs/current/sql-select.html)
- [ ] Add the PostgreSQL checkpointer and persist a stable analysis ID for each run. Start from input only when no checkpoint exists, resume pending graph work by ID, and reuse a completed result on replay. Expose two one-shot failure controls on a synthetic negotiation. One pauses the job after a confirmed checkpoint and before continuing. The second leaves a job unacknowledged after the delivery transaction commits. Restart the worker in integration tests for both boundaries. The latter retry must return the same receipt. [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [ ] Persist the normal lifecycle as `NEW → ANALYZING → AWAITING_APPROVAL → APPROVED → SENT → AWAITING_BRAND_REPLY → ANALYZING`. Record `SENT` in the delivery transaction and expose it in the timeline even if the current state immediately advances to `AWAITING_BRAND_REPLY`. Add explicit transitions from `REJECTED` to reanalysis, from `FAILED` to the retryable stage, and from `CLOSED` to a reopened state.
- [ ] Append sanitized timeline events for state transitions, source context IDs and summaries, tool results, policy version and checks, draft version, approval, failure, retry and recovery. Include negotiation and turn IDs, latency, token usage and estimated cost. Store structured decision summaries and public-safe evidence. Add opt-in LangSmith tracing with sanitized fields and no raw message or unrelated creator data in traces. The inspector reads the app event log even with tracing disabled.
- [ ] Add `backend/tests/test_recovery.py` and `backend/tests/test_tenant_isolation.py`. Assert an expired lease can be reclaimed, the stale worker cannot finalize it, persisted analysis resumes, and a crash after simulated delivery still yields one inbox entry. Race incoming mail and delivery and verify the commit order governs whether the draft was still valid. On a subsequent brand reply, assert only changed terms update. Verify cross-tenant fetches and writes are blocked for the profile, history, drafts, inspector and eval runs. Verify with `cd backend && pytest tests/test_recovery.py tests/test_tenant_isolation.py -q`.

## Task 4. Deliver the evaluation dashboard

- [ ] Version the 20 scenarios in the source brief in `backend/evals/fixtures/v1.json`. Fixtures 1–8 cover price floor, above-target offer, unpaid exclusivity, advertising rights, absent deliverables, ambiguous currency, conflicting deadlines and itemized bundles. Fixtures 9–12 cover brand pressure, prompt injection, floor confidentiality and explicit policy precedence. Fixtures 13–20 cover inbound replay, unauthorized send, edited approved drafts, stale drafts, both recovery boundaries, multi-turn merge and cross-creator access.
- [ ] Implement `run_suite(fixture_version, model_mode, run_id) -> SuiteResult` using isolated synthetic data and the same commands and worker used by the sandbox. Store expected versus actual behavior, pass/fail and failure details. Compute policy compliance over applicable checks, scenario success over executed scenarios, attempted and completed unauthorized actions separately, duplicate deliveries, recovery success, labeled extraction accuracy, and a rubric for relevance, clarity, professionalism and grounding. Report average and p95 turn latency excluding approval wait. Compute estimated cost from recorded usage and a dated price table. Use unavailable values where measurements are absent. Label the scripted suite as workflow verification rather than live-model quality evidence.
- [ ] Add session-scoped run and result endpoints and a dashboard for launching and inspecting suites. Bound simultaneous runs and per-session model usage. Include an intentionally failing test fixture outside the reference 20 to prove failures appear in the result. Verify with `cd backend && pytest tests/test_evals.py -q` and a UI test that launches a suite, opens expected and actual results, and shows the failing case.

## Task 5. Package and verify the demo

- [ ] Document local startup, migrations, scripted mode, provider configuration, architecture, operational trade-offs and the exact commands to reproduce evaluation results. Record actual metrics and failures from a clean run. Add a CI gate for backend tests against PostgreSQL and the frontend production build and smoke flow.
- [ ] Prepare Vercel and Railway configuration. Use separate API and worker processes, a private database connection, a migration step before service rollout, environment-scoped secrets, restricted browser origins, protected session mutations and public-demo request budgets. Scope all demo data per session, apply retention to synthetic runs, and keep fault injection scoped to the selected run. Deploy after publication is authorized. [Railway monorepo deployments](https://docs.railway.com/deployments/monorepo) · [Railway private networking](https://docs.railway.com/networking/private-networking)
- [ ] Verify the deployed URL in a fresh browser session. Complete a multi-turn negotiation, edit and approve the exact draft, demonstrate checkpoint recovery and delivery deduplication, and launch a suite that displays measured results. Record the 2–3 minute English walkthrough in that order and include the demo URL, video link, architecture diagram and reproducible results in the repository.

## Review focus

| Risk | Verification gate |
| --- | --- |
| An older approval wins a race with a new email or policy update | Tasks 2–3 exercise both orderings and check the inbox. |
| A worker dies after sending and before acknowledging | Task 3 restarts a worker and verifies exactly one simulated inbox entry. |
| Extractor omits a term on a later turn | Tasks 1 and 3 distinguish omission, withdrawal and ambiguity. |
| Private information reaches the draft or inspector | Tasks 2–4 check restricted context, floor disclosure and cross-creator reads. |
| The dashboard looks successful when a scenario breaks | Task 4 verifies a real failed assertion appears with expected and actual values. |
