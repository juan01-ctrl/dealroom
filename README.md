# Dealroom

Dealroom is a standalone AI negotiation sandbox for creator brand deals. It extracts terms from simulated brand emails, applies versioned creator policies, drafts a response, and delivers it to a simulated inbox after the creator approves the exact draft version.

Open [the local workspace](http://localhost:3000) after starting the services. Load the example offer to see a full inbound email and the linked Northstar campaign brief. The agent combines the USD 1,200 offer with the exclusivity and paid-usage terms in the brief, then proposes USD 2,600 before paid usage. The inspector shows which document revision and sections supported the draft.

## Run locally

Prerequisites are Docker Compose, Node.js 22, Python 3.12, and uv. Compose publishes PostgreSQL on local port 55432. Set `DEALROOM_DB_PORT` before starting Compose to use a different port, and use the same port in `DATABASE_URL`.

```sh
docker compose up -d --wait db
cd backend
uv sync --locked --python 3.12
export DATABASE_URL=postgresql://sandbox:sandbox-local@127.0.0.1:55432/negotiation
uv run --locked python -m app.migrate
uv run --locked uvicorn app.api:app --host 127.0.0.1 --port 8000
```

In a second terminal, run the worker from `backend/` with the same `DATABASE_URL`.

```sh
uv run --locked python -m app.worker
```

In a third terminal, start the web app from `frontend/`.

```sh
npm ci
npm run dev
```

The default scripted model handles the synthetic reference cases and makes repeatable runs possible without credentials. Set `MODEL_MODE=live` in the API and worker to try a provider. Set the same `MODEL_PROVIDER` and `MODEL_NAME` for the API and worker, and supply the provider key to the worker. Keep provider keys server-side. The worker caps each model call at 1,000 output tokens and uses bounded job retries. A regular live turn performs one provider extraction call. Proposal presentation runs locally. A live evaluation consumes model calls across the suite and requires `ALLOW_LIVE_EVALUATIONS=1` on the API. For estimates, record rates with `PRICE_AS_OF`, `PRICE_INPUT_USD_PER_MILLION`, and `PRICE_OUTPUT_USD_PER_MILLION` on the worker. Set `ENABLE_LANGSMITH=1` and the LangSmith project credentials to export sanitized turn summaries.

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

With the frontend running, reload `http://127.0.0.1:3000`. The header displays **Live model · simulated delivery**. For a local shared model-call cap, add `LIVE_DAILY_MODEL_CALLS` to the environment file. It counts requests reserved before each model call, across workers, and resets at midnight UTC.

## Verify

Run the backend suite against a dedicated PostgreSQL database. Each test run creates and removes a fresh schema.

```sh
cd backend
TEST_DATABASE_URL=postgresql://sandbox:sandbox-local@127.0.0.1:55432/negotiation uv run --locked pytest -q
uv run --locked ruff check app tests
```

With all three services running, run the browser suite from `frontend/`.

```sh
npm run build
npx playwright install chromium
npm test
```

To regenerate the evaluation artifact from `backend/`, migrate the chosen database first and set `DATABASE_URL`. The command uses isolated synthetic tenants and the same worker and pricing rules as the dashboard.

```sh
uv run --locked python -m app.evaluations --output ../docs/evaluation-results.json
```

The dashboard shows observed outcomes and fixture version. Suite v4 contains 33 cases and reports linked-evidence recall and precision, sender evidence, negotiation moves, and average reply length. Scripted scores measure workflow behavior. Latency varies by machine, and live model quality and estimated cost vary by provider. See [the recorded run](docs/evaluation-results.json), [the held-out live review guide](docs/live-negotiation-review.md), and [the walkthrough](docs/dealroom-walkthrough.mp4).

## Langfuse observability

Set `ENABLE_LANGFUSE=1` and the Langfuse project keys on the worker to export job, model, and evaluation observations. The inspector shows the trace ID under **Turn evidence**. CI runs the synthetic evals and tracing tests without external credentials. Follow the [Langfuse configuration and verification guide](docs/langfuse.md).

## Deploy

Follow the [operations runbook](docs/operations.md) for the Railway API, separate analysis, delivery, and evaluation workers, and Vercel frontend. The runbook covers credentials, migration, health checks, backups, rollout and rollback. Run the isolated local release drill with `bash scripts/release-smoke.sh`. See the [production mail implementation plan](docs/superpowers/plans/2026-11-25-production-mail-readiness.md) for capacity assumptions and launch gates.

The default workspace uses simulated mail. The [real-mail integration guide](docs/real-mail-integration.md) specifies the opt-in identity gateway and mail relay, including workspace membership, authenticated inbound events, sender and recipient binding, and receipt reconciliation. Verify the chosen gateway, provider account, workspace access, and data retention policy in staging before onboarding creators.

See [architecture and trade-offs](docs/architecture.md) and the [production audit and remediation status](docs/production-readiness-audit.md).
# dealroom
