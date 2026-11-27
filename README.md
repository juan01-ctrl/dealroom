# Dealroom

Dealroom is a standalone AI negotiation sandbox for creator brand deals. It extracts terms from simulated brand emails, applies versioned creator policies, drafts a response, and delivers it to a simulated inbox after the creator approves the exact draft version.

Watch the sample negotiation or see how Next.js, FastAPI, PostgreSQL, and the LangGraph workers fit together. [Demo walkthrough](docs/dealroom-walkthrough.mp4) · [Mermaid architecture diagram](docs/architecture.md)

## Run locally

You need Docker Compose, Node.js 22, Python 3.12, and uv. The quickstart uses the scripted model and needs no API key. Start each terminal block from the repository root.

### Terminal 1 — Database and API

```sh
docker compose up -d --wait db
cd backend
uv sync --locked --python 3.12
export DATABASE_URL=postgresql://sandbox:sandbox-local@127.0.0.1:55432/negotiation
uv run --locked python -m app.migrate
uv run --locked uvicorn app.api:app --host 127.0.0.1 --port 8000
```

### Terminal 2 — Worker

```sh
cd backend
export DATABASE_URL=postgresql://sandbox:sandbox-local@127.0.0.1:55432/negotiation
uv run --locked python -m app.worker
```

### Terminal 3 — Web app

```sh
cd frontend
npm ci
npm run dev
```

Open the workspace, select **Load example offer**, then **Add to thread**. The agent reads the linked Northstar brief and counters the USD 1,200 Reel offer with USD 2,600 before paid usage. The inspector shows which brief revision and sections informed the draft. Approve the draft to see one receipt in the simulated inbox. [Open the local workspace](http://localhost:3000)

If port `55432` is in use, set `DEALROOM_DB_PORT` before starting Compose and use that same port in both `DATABASE_URL` exports.

## Configure a live model locally

From the repository root, run this in an interactive terminal.

```sh
cd backend
uv run --locked --python 3.12 python -m app.configure_live
```

Choose `openai` or `anthropic`, enter a model ID available to your provider project, and paste the API key and PostgreSQL connection URI when prompted. The two secrets are entered without terminal echo. The command creates a git-ignored `backend/.env.live` with owner-only access and preserves any existing file.

For a local PostgreSQL instance, run `docker compose up -d --wait db` from the repository root and enter `postgresql://sandbox:sandbox-local@127.0.0.1:55432/negotiation` at the database prompt. For hosted PostgreSQL, use the provider's direct connection URI and its recommended TLS settings. The worker uses session-level PostgreSQL advisory locks, which require a direct connection or session pooling.

Apply migrations to the selected database. Run preflight to validate configuration and schema. The optional provider smoke test makes one billable extraction call with synthetic data.

```sh
uv run --locked --env-file .env.live python -m app.migrate
uv run --locked --env-file .env.live python -m app.preflight
uv run --locked --env-file .env.live python -m app.preflight --check-model
```

If the scripted API and worker are running, stop those processes first. Start the live API and worker in separate terminals in `backend/`.

```sh
uv run --locked --env-file .env.live uvicorn app.api:app --host 127.0.0.1 --port 8000
uv run --locked --env-file .env.live python -m app.worker
```

With the frontend running, reload the workspace. The header displays **Live model · simulated delivery**. The API and worker use the same `MODEL_PROVIDER` and `MODEL_NAME`, and the worker uses the provider key. Keep provider keys server-side. [Open the local workspace](http://localhost:3000)

A regular live turn makes one extraction call. Dealroom applies policy and renders the draft locally. The worker caps each model call at 1,000 output tokens and uses bounded job retries. Set `LIVE_DAILY_MODEL_CALLS` for a shared call cap across workers that resets at midnight UTC. Live evaluations call the model across the suite and require `ALLOW_LIVE_EVALUATIONS=1` on the API. Set `PRICE_AS_OF`, `PRICE_INPUT_USD_PER_MILLION`, and `PRICE_OUTPUT_USD_PER_MILLION` on the worker to display cost estimates. `ENABLE_LANGSMITH=1` and LangSmith project credentials enable optional sanitized turn summaries.

## Verify

Run the backend suite against a dedicated PostgreSQL database. Each test run creates and removes a fresh schema.

```sh
cd backend
TEST_DATABASE_URL=postgresql://sandbox:sandbox-local@127.0.0.1:55432/negotiation uv run --locked pytest -q
uv run --locked ruff check app tests
```

Keep the local web app, API, and worker running in the quickstart configuration. The browser suite expects `MODEL_MODE=scripted` on the API and worker because it checks exact draft text and runs the scripted evaluation suite. From the repository root, run the following commands.

```sh
cd frontend
npm run build
npx playwright install chromium
npm test
```

To regenerate the evaluation artifact from `backend/`, migrate the chosen database first and set `DATABASE_URL`. The command uses isolated synthetic tenants and the same worker and pricing rules as the dashboard.

```sh
uv run --locked python -m app.evaluations --output ../docs/evaluation-results.json
```

The dashboard shows observed outcomes and fixture version. Suite v4 contains 33 cases and reports linked-evidence recall and precision, sender evidence, negotiation moves, and average reply length. Scripted scores measure workflow behavior. Latency varies by machine, and live model quality and estimated cost vary by provider. [Recorded evaluation run](docs/evaluation-results.json) · [Held-out live review guide](docs/live-negotiation-review.md)

## Langfuse observability

Set `ENABLE_LANGFUSE=1` and the Langfuse project keys on the worker to export job, model, and evaluation observations. The inspector shows the trace ID under **Turn evidence**. CI runs the synthetic evals and tracing tests without external credentials. Follow the [Langfuse configuration and verification guide](docs/langfuse.md).

## Deploy to staging

1. In Railway staging, connect the API and the analysis and delivery workers to the staging PostgreSQL database. Use `/backend` as the service root, set `REAL_MAIL_ENABLED=0`, and give the API an HTTPS domain. Confirm that `/ready` returns HTTP 200.
2. In Vercel, set the project root to `frontend` and configure a protected Preview. Set `BACKEND_URL` to the Railway staging API’s HTTPS origin and set `API_PROXY_SECRET` as a sensitive Preview variable using the same value as the Railway API. Deploy a new Preview after saving the variables.
3. Open the protected Preview and submit a synthetic offer. In live mode, confirm **Live model · simulated delivery**, review and approve the draft, then reload. The thread should still show exactly one receipt in the simulated inbox.

The core smoke test uses the analysis and delivery workers. The full deployment adds a separate evaluation worker. Run the isolated local release drill with `bash scripts/release-smoke.sh`. [Operations runbook](docs/operations.md) · [Capacity assumptions and launch gates](docs/superpowers/plans/2026-11-25-production-mail-readiness.md)

The default workspace uses simulated mail. The [real-mail integration guide](docs/real-mail-integration.md) specifies the opt-in identity gateway and mail relay, including workspace membership, authenticated inbound events, sender and recipient binding, and receipt reconciliation. Verify the chosen gateway, provider account, workspace access, and data retention policy in staging before onboarding creators.

[Production audit and remediation status](docs/production-readiness-audit.md)
