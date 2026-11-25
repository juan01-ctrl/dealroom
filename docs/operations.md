# Dealroom operations

Use this runbook to release and recover the synthetic environment. Keep that environment access-protected and use fictional offers, inboxes, and creator details. A real-mail pilot requires separate approval of its identity, mailbox, model provider, and data-handling settings. See the [production-readiness audit](production-readiness-audit.md).

## Verify a release candidate

1. Have a second maintainer check out the exact candidate commit in a clean directory. Record the commit and the previous approved commit or image digest in the release record.
2. Check the [CI workflow](../.github/workflows/ci.yml) for locked installs, Ruff lint and format, backend tests, TypeScript, the Next.js build, Chromium tests, dependency advisories, and the Docker recovery drill. Python scans the locked runtime requirements. npm scans the whole lockfile and fails at high severity or above. Review any reported advisories before approval.
3. Run the Docker recovery drill locally from the repository root. It builds the backend, confirms readiness before and after a clean migration, upgrades populated PostgreSQL 17 data, kills and restarts the evaluation worker while the negotiation worker stays healthy, and restores a logical backup into an empty database. It creates its own containers and network and uses scripted models.

   ```sh
   bash scripts/release-smoke.sh
   ```

4. For upgrades, make the previous approved backend image available in the local Docker engine and repeat with `BASELINE_IMAGE` set to its local image reference. This keeps the prior API running against the upgraded schema and checks that both APIs report ready and can read an existing session's profile. The default drill uses the historical `001`–`005` SQL files and the candidate image to initialize the older schema. In staging, run the workflow through both deployed versions.

   ```sh
   BASELINE_IMAGE='your-registry/dealroom-backend@sha256:approved-digest' bash scripts/release-smoke.sh
   ```

5. Agree who approves, deploys, receives alerts, and can restore the database. Give the second maintainer access to the repositories, deployment dashboards, monitoring, and backup controls they need. Record recovery point and recovery time targets, the current backup policy, and the last successful drill.

## Deploy to protected staging

1. Select or create a Railway staging environment with its own PostgreSQL database, distinct secrets, and a dedicated provider project or credential with an approved call budget. Preview the target with `railway config plan` before applying [the infrastructure definition](../.railway/railway.ts). For each of `api`, `worker-negotiation`, and `worker-evaluation`, connect the approved source and set its service root to `/backend`. Use the backend Dockerfile. Keep PostgreSQL and both workers private. The API needs HTTPS ingress for the Vercel server. [Railway infrastructure reference](https://docs.railway.com/infrastructure-as-code/reference) · [Railway monorepo setup](https://docs.railway.com/deployments/monorepo)
2. If replacing a previously deployed all-purpose worker, use a staged configuration that keeps the prior service while adding the two new services. Inspect the plan before applying. Verify the new negotiation and evaluation workers before retiring the previous worker. Their `preDeploy` command runs `python -m app.migrate` under the database advisory lock. Railway's `/ready` check gates the API rollout. Configure continuous polling separately. [Railway healthchecks](https://docs.railway.com/deployments/healthchecks)
3. Configure a protected Vercel deployment with root `frontend`, the Next.js preset, and its own deployment access controls. Scope `BACKEND_URL` and `API_PROXY_SECRET` to the matching Railway environment. Verify that an unauthenticated user cannot open the deployment. [Vercel deployment protection](https://vercel.com/docs/deployment-protection)

| Location | Variables |
| --- | --- |
| Railway shared | `API_PROXY_SECRET`, `OPERATIONS_TOKEN`, `MODEL_PROVIDER`, `MODEL_NAME`, `LIVE_DAILY_MODEL_CALLS` |
| Railway negotiation and evaluation workers | The credential for `MODEL_PROVIDER`, either `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` |
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

Expect HTTP 200 with `status` equal to `ok` and both `workers.negotiation` and `workers.evaluation` equal to `true`. HTTP 503 indicates degraded worker or queue status. Configure a monitor to page the operations owner for repeated readiness failures, degraded operations, or a missing heartbeat flag for either expected worker. The operations code lets the API stay healthy when live evaluations are disabled and the evaluation worker is unavailable. Track terminal queue failures, provider budget or error-rate spikes, and backup age. On a queue alert, examine the affected lane, recent deployments, database availability, and provider health before restarting anything.

With a fresh browser session on the protected frontend, start a negotiation and submit a fictional USD 3,500 offer for one Instagram Reel. Verify that a draft arrives, review its complete terms, approve it, and verify that the simulated inbox gains exactly one receipt. Reload and verify retention of the thread and receipt across the reload. Record end-to-end latency and the corresponding deployment identifiers. Avoid including email bodies, cookies, or secret values in the release record.

For an upgrade, keep the prior API and workers available in staging while the new schema is installed. Exercise an existing browser session through the prior API, then through the candidate API. Verify that queued work remains serviceable while old and new workers overlap. Remove the old worker only after the replacement has a fresh heartbeat and the smoke succeeds. If old binaries cannot safely interpret a candidate job, pause submission during rollout and verify that existing work drains or resumes as planned.

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
