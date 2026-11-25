#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
scratch=$(mktemp -d)
run_id="dealroom-$(basename "$scratch" | tr '[:upper:]' '[:lower:]')"
image="$run_id:smoke"
database="$run_id-db"
api="$run_id-api"
legacy_api="$run_id-legacy-api"
negotiation="$run_id-negotiation"
evaluation="$run_id-evaluation"
proxy_token=local-smoke-proxy-token-not-a-secret
operations_token=local-smoke-operations-token-not-a-secret

cleanup() {
  local status=$?
  trap - EXIT
  if [[ "$status" != 0 ]]; then
    for container in "$api" "$legacy_api" "$negotiation" "$evaluation" "$database"; do
      if docker inspect "$container" >/dev/null 2>&1; then
        docker logs --tail 30 "$container" >&2 || true
      fi
    done
  fi
  docker rm -fv "$api" "$legacy_api" "$negotiation" "$evaluation" "$database" >/dev/null 2>&1 || true
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
    -e APP_ENV=local -e MODEL_MODE=scripted \
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
  run_backend negotiation -d --name "$1" "$image" python -m app.worker --lane "$2" >/dev/null
}

new_session() {
  wait_http 200 "$1/api/session" -X POST -H "X-API-Proxy: $proxy_token" \
    -H 'X-Sandbox-Request: 1' -c "$scratch/session.cookies"
}

read_profile() {
  wait_http 200 "$1/api/profile" -H "X-API-Proxy: $proxy_token" -b "$scratch/session.cookies"
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

echo 'Installing the baseline schema and a synthetic negotiation'
if [[ -n "${BASELINE_IMAGE:-}" ]]; then
  run_backend negotiation --rm "$BASELINE_IMAGE" python -m app.migrate
  start_api negotiation "$legacy_api" "$BASELINE_IMAGE"
  legacy_url=$(api_url "$legacy_api")
  wait_http 200 "$legacy_url/ready"
  new_session "$legacy_url"
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
start_worker "$negotiation" negotiation
start_worker "$evaluation" evaluation
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
docker rm -f "$api" >/dev/null
start_api restored "$api" "$image"
url=$(api_url "$api")
wait_http 200 "$url/ready"
read_profile "$url"
echo 'Container startup, migrations, lane recovery, and backup restore passed.'
