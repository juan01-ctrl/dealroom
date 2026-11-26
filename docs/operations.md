# Dealroom operations

Use this runbook to release and recover the synthetic environment. Keep that environment access-protected and use fictional offers, inboxes, and creator details. A real-mail pilot requires separate approval of its identity, mailbox, model provider, and data-handling settings. See the [production-readiness audit](production-readiness-audit.md).

## Verify a release candidate

Use the [release evidence guide](release-evidence.md) to reproduce the candidate and collect artifacts.

1. Have a second maintainer check out the exact candidate commit in a clean directory. Record the commit and the previous approved commit or image digest in the release record.
2. Check the [CI workflow](../.github/workflows/ci.yml) for locked installs, Ruff lint and format, backend tests, TypeScript, the Next.js build, Chromium tests, dependency advisories, and the Docker recovery drill. Python scans the locked runtime requirements. npm scans the whole lockfile and fails at high severity or above. Review any reported advisories before approval.
3. Run the Docker recovery drill locally from the repository root. It builds the backend, confirms readiness before and after a clean migration, upgrades populated PostgreSQL 17 data, kills and restarts the evaluation worker while the analysis and delivery workers stay healthy, and restores a logical backup into an empty database. It creates its own containers and network and uses scripted models.

   ```sh
   bash scripts/release-smoke.sh
   ```

4. For upgrades, make the previous approved backend image available in the local Docker engine and repeat with `BASELINE_IMAGE` set to its local image reference. This keeps the prior API running against the upgraded schema and checks that both APIs report ready and can read an existing session's profile. The default drill uses the historical `001`–`005` SQL files and the candidate image to initialize the older schema. In staging, run the workflow through both deployed versions.

   ```sh
   BASELINE_IMAGE='your-registry/dealroom-backend@sha256:approved-digest' bash scripts/release-smoke.sh
   ```

5. Agree who approves, deploys, receives alerts, and can restore the database. Give the second maintainer access to the repositories, deployment dashboards, monitoring, and backup controls they need. Record recovery point and recovery time targets, the current backup policy, and the last successful drill.

## Deploy to protected staging

1. Select or create a Railway staging environment with its own PostgreSQL database, distinct secrets, and a dedicated provider project or credential with an approved call budget. Preview the target with `railway config plan` before applying [the infrastructure definition](../.railway/railway.ts). For each of `api`, `worker-analysis`, `worker-delivery`, and `worker-evaluation`, connect the approved source and set its service root to `/backend`. Use the backend Dockerfile. Keep PostgreSQL and all workers private. The API needs HTTPS ingress for the Vercel server. [Railway infrastructure reference](https://docs.railway.com/infrastructure-as-code/reference) · [Railway monorepo setup](https://docs.railway.com/deployments/monorepo)
2. If replacing a combined worker, retain its deployment during migration and start the new workers before routing live traffic. Follow the versioned queue drain below before retiring any compatible worker. Their `preDeploy` command runs `python -m app.migrate` under the database advisory lock. Railway's `/ready` check gates the API rollout. Configure continuous polling separately. [Railway healthchecks](https://docs.railway.com/deployments/healthchecks)
3. Configure a protected Vercel deployment with root `frontend`, the Next.js preset, and its own deployment access controls. Scope `BACKEND_URL` and `API_PROXY_SECRET` to the matching Railway environment. Verify that an unauthenticated user cannot open the deployment. [Vercel deployment protection](https://vercel.com/docs/deployment-protection)

| Location | Variables |
| --- | --- |
| Railway shared | `API_PROXY_SECRET`, `OPERATIONS_TOKEN`, `MODEL_PROVIDER`, `MODEL_NAME`, `LIVE_DAILY_MODEL_CALLS` |
| Railway analysis and evaluation workers | The credential for `MODEL_PROVIDER`, either `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` |
| Railway delivery worker | Only the relay credentials if real mail is enabled. |
| Vercel server | `BACKEND_URL` for the Railway API HTTPS origin, and the same `API_PROXY_SECRET` |
| Monitoring service | Railway API HTTPS origin, `API_PROXY_SECRET`, and `OPERATIONS_TOKEN` |

Generate each shared secret separately with `openssl rand -hex 32` in an operator terminal and store it in the authorized secret stores. `LIVE_DAILY_MODEL_CALLS` limits shared live calls for the environment. Keep live evaluations and real mail disabled for the synthetic release. The browser proxy does not forward the operations token.

### Staging smoke and alerts

Check readiness and operations directly on the API from an approved operator terminal. Load the URL and secret values into the shell from the secret store and keep command tracing off. The header values are passed through standard input.

```sh
curl --fail-with-body --silent --show-error --max-time 10 "$BACKEND_URL/ready"
printf 'header = "X-API-Proxy: %s"\nheader = "X-Operations-Token: %s"\n' \
  "$API_PROXY_SECRET" "$OPERATIONS_TOKEN" |
  curl --config - --fail-with-body --silent --show-error --max-time 10 "$BACKEND_URL/api/operations"
```

Expect HTTP 200 with `status` equal to `ok` and both `workers.negotiation` and `workers.evaluation` equal to `true`. HTTP 503 indicates degraded worker or queue status. Configure a monitor to page the operations owner for repeated readiness failures, degraded operations, or a missing heartbeat flag for either expected worker. The operations code lets the API stay healthy when live evaluations are disabled and the evaluation worker is unavailable. Track terminal queue failures, provider budget or error-rate spikes, and backup age. The protected mail_confirmation field reports unresolved attempts and oldest age. The endpoint degrades after 15 minutes of unresolved handoff. Inspect the relay record before deciding how to reconcile an attempt. On a queue alert, examine the affected lane, recent deployments, database availability, and provider health before restarting anything.

With a fresh browser session on the protected frontend, start a negotiation and submit a fictional USD 3,500 offer for one Instagram Reel. Verify that a draft arrives, review its complete terms, approve it, and verify that the simulated inbox gains exactly one receipt. Reload and verify retention of the thread and receipt across the reload. Record end-to-end latency and the corresponding deployment identifiers. Avoid including email bodies, cookies, or secret values in the release record.

### Drain versioned jobs during release

Install the candidate migrations first. Keep at least one worker running the exact previous workflow and model while the new API and worker services start. Stop new work from reaching the previous API when switching producer traffic. Check the protected operations response for `analysis_execution_backlog`; each group reports its identity, queued, running and failed counts, and whether a compatible analysis worker is present. An active group without a compatible worker degrades operations health. The worker accepts legacy rows without an execution identity.

Promote the API before the browser. Current creator clients require the API write capability, and fall back to read-only when an earlier API omits it. The latest API retains the unpaged response for earlier browser deployments.

Retire a previous worker after its queued and running counts reach zero and a release owner has assigned a disposition for failed jobs. A retry of a failed job reuses its original identity and checkpoints. Retain or temporarily restore the previous worker to retry it. For a failed predecessor the creator can explicitly skip the old turn using the recovery control. Avoid rewriting a pending job's execution identity to force it through a different graph. A model-budget deferral waits for midnight UTC and stays visible as both delayed work and a budget-specific count; include deferred jobs in the drain decision.

### Load and capacity verification

API short transactions use up to 16 pooled connections per process. Each worker process uses up to four and may also need dedicated connections for graph persistence, heartbeats and transaction fences. Reserve database capacity for migrations and operators. Use a session-capable database endpoint for advisory locks. Monitor pool waiters and errors in the protected operations response.

Run against a dedicated synthetic PostgreSQL database. The harness creates and drops a randomly named isolated schema.

```sh
cd backend
LOAD_TEST_DATABASE_URL=postgresql://sandbox:sandbox-local@127.0.0.1:55432/negotiation \
  PYTHONPATH=. uv run --locked python ../scripts/queue-load.py --messages 600 --workers 8 --tenants 12
```

This measures repository-side mail admission and queue claims. For approved staging, enroll synthetic routes pointing to test-only mailboxes. Make sure daily limits can accommodate the assigned messages. Store route pairs as a JSON array with to and from_address fields, then load MAIL_INBOUND_SECRET and MAIL_PROVIDER from the staging secret store.

```sh
cd backend
uv run --locked python -m app.ingress_probe --origin "$STAGING_API_ORIGIN" \
  --routes "$SYNTHETIC_ROUTES_FILE" --messages 5000 --rate 20 --duplicate-every 5
```

The signed probe records HTTP latency, status counts and event-to-job consistency. It detects different receipts for the same event and shared receipts for different events and exits nonzero for unconfirmed responses or changed receipts. Reconcile non-202 responses with relay replay in a separate controlled fault drill before evaluating loss. This probe measures ingress only. In hosted staging measure signed HTTPS ingress, duplicate retries, queue wait, provider latency and actual approved delivery separately. Capture p50/p95, connection usage, lock waits, CPU, I/O, provider quota, oldest ready age, and workers by lane. Use the traffic assumptions and release objectives in the [production plan](superpowers/plans/2026-11-25-production-mail-readiness.md). Scale analysis by observed processing duration and arrival rate. Test one hot workspace separately for tenant-lock contention during provider handoff.

## Backup and restore

Verify the managed PostgreSQL backup schedule and retention in the Railway account. If point-in-time recovery is enabled, record its archive health and usable restore window. Test restoration into a separate PostgreSQL service before a handoff. Keep the restored service isolated from the original application and workers while verifying it. [Railway point-in-time recovery](https://docs.railway.com/volumes/point-in-time-recovery)

For a logical drill, use `pg_dump` and `pg_restore` clients compatible with PostgreSQL 17. Define `dealroom-backup-source` for the approved synthetic source and `dealroom-restore-target` for a separate empty test database using libpq service entries and a credential store. Confirm both identities and network permissions before starting. Store the dump in an approved encrypted location and use a restrictive file permission mask.

```sh
umask 077
backup_file="dealroom-$(date -u +%Y%m%dT%H%M%SZ).dump"
pg_dump --dbname='service=dealroom-backup-source' --format=custom --no-owner --no-acl --file="$backup_file"
pg_restore --list "$backup_file" > /dev/null
pg_restore --dbname='service=dealroom-restore-target' --no-owner --no-acl \
  --exit-on-error --single-transaction "$backup_file"
```

Verify migration versions, the graph checkpoint tables, the synthetic negotiation and receipt, and API readiness against the isolated target. Confirm target privileges separately when using `--no-acl`. Record the restore point, elapsed time, and evidence that the expected committed state survived. Apply the agreed retention and access policy to the dump and the test database.

For a real-mail recovery, stop ingestion and delivery before changing the database. Preserve the failed state for reconciliation. Review approvals, queued sends, and provider receipts against the restore point before enabling delivery. A restored outbox may contain work the provider already received. Reconcile ambiguous deliveries before retrying them.

## Rollback and handoff

1. On a bad release, halt promotion and record the failed and previous deployment IDs. Keep the migrated database when the previous revision passed compatibility checks against it. Roll back Railway API and worker releases to the verified compatible versions and roll back the Vercel deployment. If the schema is incompatible, pause writes and recover with the tested forward fix or isolated restore procedure. [Railway deployment actions](https://docs.railway.com/deployments/deployment-actions) · [Vercel rollback](https://vercel.com/docs/deployments/promoting-a-deployment)
2. Verify readiness, operations status, and a synthetic workflow after rollback. If the prior release predates `/api/operations`, use its service logs and the processed synthetic workflow to confirm worker activity. Check the queue and receipts before resuming any paused activity.
3. Hand over the commit or release tag, backend image digest, deployment and database identifiers, secret locations, alert route, known limitations, and CI and recovery evidence. Have the new operator repeat the protected smoke, explain how to handle a stalled lane, and walk through rollback and restore.

The local drill uses synthetic PostgreSQL data and does not establish Railway or Vercel configuration, hosted backup usability, access controls, alert delivery, or provider-account settings. Independent acceptance requires authorized operators to run the staging, restore, and handoff steps in the deployed environment.
