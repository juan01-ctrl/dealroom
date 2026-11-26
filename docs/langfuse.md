# Langfuse tracing and evaluations

Dealroom uses the Langfuse Python v5 SDK. Use Langfuse Cloud or a compatible self-hosted release. [SDK compatibility](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v4-to-v5)

## Enable tracing

Set these environment variables on the negotiation and evaluation workers. For a local CLI or worker, add the values to the ignored `backend/.env.live`. Put production values in the worker secret store. Use separate Langfuse projects for staging and production.

| Setting | Value |
| --- | --- |
| `ENABLE_LANGFUSE` | Set to `1` to enable export. |
| `LANGFUSE_BASE_URL` | Project region or self-hosted HTTPS URL. Defaults to `https://cloud.langfuse.com`. |
| `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` | Project keys from Langfuse. |
| `LANGFUSE_TRACING_ENVIRONMENT` | Optional override for the environment label. Defaults to `APP_ENV`. |
| `LANGFUSE_RELEASE` | Optional release identifier. Railway's Git SHA takes precedence when available. |

From `backend/`, validate configuration and start a local worker.

```sh
uv run --locked --env-file .env.live python -m app.preflight
uv run --locked --env-file .env.live python -m app.worker --lane all
```

After a turn completes, copy its Langfuse trace ID from **Agent inspector → Turn evidence** and search for it in Langfuse. Search by job ID to see individual retry attempts. Worker traces record revision, workflow version, model identity, and attempt. Child spans show brief retrieval, term extraction, policy evaluation, negotiation tool invocation, and reply composition. Live generation spans report token usage when the provider supplies it. Queue health and terminal failures are available through the existing protected operations endpoint.

Migrate the selected local database before running these commands. The scripted evaluation CLI uses synthetic data and requires no model key. Omit `--case` for the full suite or repeat it to select multiple cases.

```sh
uv run --locked --env-file .env.live python -m app.evaluations --case 30 --output /tmp/dealroom-eval.json
```

In Langfuse, open `dealroom.eval.suite` and select a `dealroom.eval.case` observation. Each case carries Boolean scores for the scenario verdict, its assertions, and any extraction or draft-quality checks. CI runs the full suite without Langfuse credentials. Scripted cases verify workflow behavior. Use the [held-out review cases](live-negotiation-review.md) to assess live model quality.

## Data sent to Langfuse

The integration sends internal job and thread IDs, release and model metadata, timings, token counts, retrieval counts, policy outcomes, and evaluation verdicts. It omits email and draft text, prompts, URLs, sender details, commercial amounts, and provider exception messages. Approve the project's retention and access controls before enabling export in production. LangSmith remains independently configurable through its existing flag.
