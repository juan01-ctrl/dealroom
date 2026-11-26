#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
evidence_dir=${RELEASE_EVIDENCE_DIR:-}
if [[ -n "$evidence_dir" && -z "${BASELINE_IMAGE:-}" ]]; then
  echo 'Release evidence requires the previous backend image in BASELINE_IMAGE.' >&2
  exit 1
fi
if [[ -n "$evidence_dir" ]]; then
  source_commit=$(git -C "$repo_dir" rev-parse HEAD)
  checkout_status=$(git -C "$repo_dir" status --porcelain)
  mkdir -p "$evidence_dir"
  if [[ -n "$checkout_status" ]]; then
    printf 'checkout=working-tree-uncommitted\nbase_commit=%s\n' "$source_commit"
  else
    printf 'checkout=clean\ncommit=%s\n' "$source_commit"
  fi > "$evidence_dir/candidate-checkout.txt"
  (cd "$repo_dir" && shasum -a 256 backend/uv.lock frontend/package-lock.json \
    backend/evals/fixtures/*.json backend/migrations/*.sql) > "$evidence_dir/candidate-input-sha256.txt"
fi
scratch=$(mktemp -d)
run_id="dealroom-$(basename "$scratch" | tr '[:upper:]' '[:lower:]')"
image="$run_id:smoke"
database="$run_id-db"
api="$run_id-api"
legacy_api="$run_id-legacy-api"
analysis="$run_id-analysis"
delivery="$run_id-delivery"
evaluation="$run_id-evaluation"
legacy_worker="$run_id-legacy-worker"
proxy_token=local-smoke-proxy-token-not-a-secret
operations_token=local-smoke-operations-token-not-a-secret

cleanup() {
  local status=$?
  trap - EXIT
  if [[ "$status" != 0 ]]; then
    for container in "$api" "$legacy_api" "$analysis" "$delivery" "$evaluation" "$legacy_worker" "$database"; do
      if docker inspect "$container" >/dev/null 2>&1; then
        docker logs --tail 30 "$container" >&2 || true
      fi
    done
  fi
  docker rm -fv "$api" "$legacy_api" "$analysis" "$delivery" "$evaluation" "$legacy_worker" "$database" >/dev/null 2>&1 || true
  docker network rm "$run_id" >/dev/null 2>&1 || true
  docker image rm "$image" >/dev/null 2>&1 || true
  rm -rf -- "$scratch"
  exit "$status"
}
trap cleanup EXIT

run_backend() {
  local db_name=$1
  shift
  docker run --network "$run_id" \
    -e "DATABASE_URL=postgresql://sandbox:smoke@db:5432/$db_name" \
    -e APP_ENV=local -e MODEL_MODE=scripted -e REAL_MAIL_ENABLED=0 -e ENABLE_LANGFUSE=0 \
    -e "API_PROXY_SECRET=$proxy_token" -e "OPERATIONS_TOKEN=$operations_token" "$@"
}

sql() {
  local db_name=$1
  shift
  docker exec -i "$database" psql -X -qAt -v ON_ERROR_STOP=1 -U sandbox -d "$db_name" "$@"
}

api_url() {
  local address
  address=$(docker port "$1" 8000/tcp)
  printf 'http://%s' "$address"
}

wait_http() {
  local expected=$1 url=$2 code
  shift 2
  for _ in $(seq 1 90); do
    code=$(curl -s --max-time 2 -o "$scratch/response.json" -w '%{http_code}' "$@" "$url") || true
    if [[ "$code" == "$expected" ]]; then return 0; fi
    sleep 1
  done
  printf '%s returned %s while waiting for %s\n' "$url" "$code" "$expected" >&2
  return 1
}

start_api() {
  run_backend "$1" -d --name "$2" -p 127.0.0.1::8000 "$3" >/dev/null
}

start_worker() {
  run_backend negotiation -d --name "$1" "${3:-$image}" python -m app.worker --lane "$2" >/dev/null
}

new_session() {
  wait_http 200 "$1/api/session" -X POST -H "X-API-Proxy: $proxy_token" \
    -H 'X-Sandbox-Request: 1' -c "$scratch/session.cookies"
}

read_profile() {
  wait_http 200 "$1/api/profile" -H "X-API-Proxy: $proxy_token" -b "$scratch/session.cookies"
}

execution_identity() {
  docker run --rm --network none -e MODEL_MODE=scripted "$1" python -c \
    'import json; from app.operations import execution_identity; print(json.dumps(execution_identity(), sort_keys=True))'
}

seed_analysis() {
  run_backend negotiation --rm -i "$1" python - "$2" <<'PY'
import os
import sys

from app.storage import Store

store = Store(os.environ["DATABASE_URL"])
event = sys.argv[1]
tenant, thread = store.create_session(f"release-smoke-{event}")
print(store.ingest(tenant, thread, event, "USD 2,400 for one reel.")["job_id"])
PY
}

approve_prior_delivery() {
  run_backend negotiation --rm -i "$BASELINE_IMAGE" python - "$1" <<'PY'
import os
import sys

from app.storage import Store

store = Store(os.environ["DATABASE_URL"])
with store.connect() as db:
    draft = db.execute(
        "SELECT tenant_id,thread_id,id,version FROM drafts WHERE analysis_id=%s", (sys.argv[1],)
    ).fetchone()
print(store.review(draft["tenant_id"], draft["thread_id"], draft["id"], draft["version"], "approve"))
PY
}

wait_job() {
  local identifier=$1 expected=$2 query
  query="SELECT state FROM jobs WHERE id='$identifier'"
  for _ in $(seq 1 45); do
    if [[ $(sql negotiation -c "$query") == "$expected" ]]; then return 0; fi
    sleep 1
  done
  printf 'Job %s did not reach %s\n' "$identifier" "$expected" >&2
  return 1
}

assert_analysis() {
  local identifier=$1 identity=$2
  [[ $(sql negotiation -c "SELECT count(*) FROM drafts WHERE analysis_id='$identifier'") == 1 ]]
  [[ $(sql negotiation -v job="$identifier" -v identity="$identity" <<'SQL'
SELECT count(*) FROM jobs j JOIN worker_heartbeats w ON w.worker_id::text=j.worker_id::text
WHERE j.id=:'job' AND j.state='DONE' AND j.attempts=1
  AND j.payload->'execution'=:'identity'::jsonb AND w.identity=:'identity'::jsonb;
SQL
) == 1 ]]
}

scripted_report() {
  local db_name=$1 report_image=$2 output=$3
  run_backend "$db_name" --rm "$report_image" sh -c \
    'python -m app.evaluations --output /tmp/scripted.json >&2; gate=$?; cat /tmp/scripted.json; exit "$gate"' > "$output"
}

echo 'Building the backend image and starting isolated PostgreSQL'
docker build --tag "$image" "$repo_dir/backend"
docker network create "$run_id" >/dev/null
docker run -d --name "$database" --network "$run_id" --network-alias db \
  -e POSTGRES_USER=sandbox -e POSTGRES_PASSWORD=smoke -e POSTGRES_DB=negotiation postgres:17 >/dev/null
for _ in $(seq 1 45); do
  if docker exec "$database" pg_isready -h 127.0.0.1 -U sandbox -d negotiation >/dev/null; then break; fi
  sleep 1
done

echo 'Verifying readiness and installation against an empty database'
sql postgres -c 'CREATE DATABASE fresh'
start_api fresh "$api" "$image"
url=$(api_url "$api")
wait_http 200 "$url/health"
wait_http 503 "$url/ready"
run_backend fresh --rm "$image" python -m app.migrate
wait_http 200 "$url/ready"
docker rm -f "$api" >/dev/null

if [[ -n "$evidence_dir" ]]; then
  docker image inspect --format '{{.Id}} {{json .RepoDigests}}' "$image" > "$evidence_dir/candidate-image.txt"
  docker image inspect --format '{{.Id}} {{json .RepoDigests}}' "$BASELINE_IMAGE" > "$evidence_dir/prior-image.txt"
  docker image inspect --format '{{.Id}} {{json .RepoDigests}}' postgres:17 > "$evidence_dir/postgres-image.txt"
  scripted_report fresh "$image" "$evidence_dir/candidate-scripted.json"
  python3 -c 'import json, sys; assert json.load(sys.stdin)["release_gate"]["passed"] is True' \
    < "$evidence_dir/candidate-scripted.json"
  sql postgres -c 'CREATE DATABASE prior_evaluation'
  run_backend prior_evaluation --rm "$BASELINE_IMAGE" python -m app.migrate
  scripted_report prior_evaluation "$BASELINE_IMAGE" "$evidence_dir/prior-scripted.json"
fi

echo 'Installing the baseline schema and a synthetic negotiation'
prior_identity=
if [[ -n "${BASELINE_IMAGE:-}" ]]; then
  run_backend negotiation --rm "$BASELINE_IMAGE" python -m app.migrate
  start_api negotiation "$legacy_api" "$BASELINE_IMAGE"
  legacy_url=$(api_url "$legacy_api")
  wait_http 200 "$legacy_url/ready"
  new_session "$legacy_url"
  if prior_identity=$(execution_identity "$BASELINE_IMAGE" 2> "$scratch/prior-identity.log"); then
    prior_job=$(seed_analysis "$BASELINE_IMAGE" prior-ready)
    deferred_job=$(seed_analysis "$BASELINE_IMAGE" prior-deferred)
    approval_job=$(seed_analysis "$BASELINE_IMAGE" prior-approval)
    run_backend negotiation --rm "$BASELINE_IMAGE" python -m app.worker --lane negotiation --once "$approval_job"
    approval=$(approve_prior_delivery "$approval_job")
    sql negotiation -v job="$deferred_job" <<'SQL'
UPDATE jobs SET available_at=now()+interval '1 day',
  payload=payload || '{"deferred_reason":"model_budget"}' WHERE id=:'job';
SQL
  elif [[ -n "$evidence_dir" ]]; then
    cat "$scratch/prior-identity.log" >&2
    echo 'The previous image must support execution identities for release qualification.' >&2
    exit 1
  else
    echo 'Prior image predates execution identities. Checking its schema and API compatibility.'
  fi
else
  # The audit baseline ends at 005. Use a published baseline image once one is available.
  mkdir "$scratch/baseline"
  cp "$repo_dir"/backend/migrations/00[1-5]_*.sql "$scratch/baseline/"
  chmod 755 "$scratch/baseline"
  run_backend negotiation --rm --volume "$scratch/baseline:/app/migrations:ro" \
    "$image" python -m app.migrate
fi

sql negotiation <<'SQL'
INSERT INTO tenants (id) VALUES ('00000000-0000-4000-8000-000000000001');
INSERT INTO negotiations (tenant_id, id, label) VALUES (
  '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000002', 'Release drill'
);
SQL
start_api negotiation "$api" "$image"
url=$(api_url "$api")
wait_http 200 "$url/health"
if [[ -z "${BASELINE_IMAGE:-}" ]]; then wait_http 503 "$url/ready"; fi

echo 'Applying forward migrations twice and verifying existing data'
run_backend negotiation --rm "$image" python -m app.migrate
run_backend negotiation --rm "$image" python -m app.migrate
wait_http 200 "$url/ready"
if [[ -n "${BASELINE_IMAGE:-}" ]]; then
  wait_http 200 "$legacy_url/ready"
  read_profile "$legacy_url"
else
  new_session "$url"
fi
read_profile "$url"
[[ $(sql negotiation -c "SELECT label FROM negotiations WHERE id='00000000-0000-4000-8000-000000000002'") == 'Release drill' ]]

echo 'Verifying authenticated worker status and lane recovery after process loss'
operations_headers=(-H "X-API-Proxy: $proxy_token" -H "X-Operations-Token: $operations_token")
wait_http 403 "$url/api/operations" -H "X-API-Proxy: $proxy_token"
wait_http 503 "$url/api/operations" "${operations_headers[@]}"
start_worker "$analysis" analysis
start_worker "$delivery" delivery
start_worker "$evaluation" evaluation
if [[ -n "$prior_identity" ]]; then
  echo 'Draining queued work for the prior and candidate execution identities'
  candidate_identity=$(execution_identity "$image")
  if [[ -n "$evidence_dir" ]]; then
    printf '%s\n' "$prior_identity" > "$evidence_dir/prior-execution.json"
    printf '%s\n' "$candidate_identity" > "$evidence_dir/candidate-execution.json"
    sql prior_evaluation -c 'SELECT name FROM schema_versions ORDER BY name' > "$evidence_dir/prior-migrations.txt"
    sql negotiation -c 'SELECT name FROM schema_versions ORDER BY name' > "$evidence_dir/candidate-migrations.txt"
  fi
  candidate_job=$(seed_analysis "$image" candidate-ready)
  wait_job "$candidate_job" DONE
  assert_analysis "$candidate_job" "$candidate_identity"
  wait_job "$approval" DONE
  [[ $(sql negotiation -c "SELECT count(*) FROM deliveries x JOIN approvals a ON a.id=x.delivery_key AND a.tenant_id=x.tenant_id JOIN drafts d ON d.id=a.draft_id AND d.tenant_id=a.tenant_id WHERE x.delivery_key='$approval' AND x.body=d.body AND a.digest=d.digest") == 1 ]]
  if [[ "$prior_identity" != "$candidate_identity" ]]; then
    wait_http 503 "$url/api/operations" "${operations_headers[@]}"
    PRIOR_EXECUTION="$prior_identity" python3 -c 'import json, os, sys
prior = json.loads(os.environ["PRIOR_EXECUTION"])
group = next(group for group in json.load(sys.stdin)["analysis_execution_backlog"] if group["identity"] == prior)
assert group["queued"] == 2 and group["running"] == 0 and group["claimant_present"] is False' \
      < "$scratch/response.json"
  else
    # Shared identities can drain each other's work. Let the old worker prove it can
    # still consume against the new schema when the deferred row becomes ready.
    docker stop "$analysis" >/dev/null
  fi
  start_worker "$legacy_worker" negotiation "$BASELINE_IMAGE"
  wait_job "$prior_job" DONE
  assert_analysis "$prior_job" "$prior_identity"
  wait_job "$deferred_job" QUEUED
  # Advance only this synthetic budget delay so the drill does not wait until midnight UTC.
  [[ $(sql negotiation -v job="$deferred_job" <<'SQL'
UPDATE jobs SET available_at=now()
WHERE id=:'job' AND state='QUEUED' AND attempts=0 AND available_at>now()
  AND payload->>'deferred_reason'='model_budget' RETURNING 1;
SQL
) == 1 ]]
  wait_job "$deferred_job" DONE
  assert_analysis "$deferred_job" "$prior_identity"
  if [[ "$prior_identity" == "$candidate_identity" ]]; then docker start "$analysis" >/dev/null; fi
  echo 'Versioned work drained and the existing approval produced exactly one matching synthetic delivery.'
fi
wait_http 200 "$url/api/operations" "${operations_headers[@]}"
docker kill "$evaluation" >/dev/null
wait_http 503 "$url/api/operations" "${operations_headers[@]}"
python3 -c 'import json, sys; assert json.load(sys.stdin)["workers"] == {"negotiation": True, "evaluation": False}' \
  < "$scratch/response.json"
docker start "$evaluation" >/dev/null
wait_http 200 "$url/api/operations" "${operations_headers[@]}"

echo 'Restoring a backup into a fresh database and checking application readiness'
docker exec "$database" pg_dump -U sandbox -d negotiation --format=custom --no-owner --no-acl \
  > "$scratch/database.dump"
sql postgres -c 'CREATE DATABASE restored'
docker exec -i "$database" pg_restore -U sandbox -d restored --no-owner --no-acl \
  --exit-on-error --single-transaction < "$scratch/database.dump"
[[ $(sql restored -c "SELECT label FROM negotiations WHERE id='00000000-0000-4000-8000-000000000002'") == 'Release drill' ]]
if [[ -n "$prior_identity" ]]; then
  [[ $(sql restored -c "SELECT count(*) FROM deliveries WHERE delivery_key='$approval'") == 1 ]]
fi
docker rm -f "$api" >/dev/null
start_api restored "$api" "$image"
url=$(api_url "$api")
wait_http 200 "$url/ready"
read_profile "$url"
echo 'Container startup, migrations, lane recovery, and backup restore passed.'

if [[ -n "$evidence_dir" ]]; then
  echo 'Running the synthetic PostgreSQL workload with 6,000 unique messages'
  sql postgres -c 'CREATE DATABASE workload'
  run_backend workload --rm -i -e LOAD_TEST_DATABASE_URL=postgresql://sandbox:smoke@db:5432/workload \
    "$image" python - --messages 6000 --workers 16 --tenants 60 < "$repo_dir/scripts/queue-load.py" \
    | tee "$evidence_dir/queue-load.json"
fi
