# Dealroom release evidence

Complete one record for each candidate. Attach the CI results and link the staging record before promotion. An authorized maintainer records the candidate and previous approved revision.

## Reproduce the repository checks

When the workflow is available on the default branch, run **Verify sandbox** in GitHub Actions on the candidate branch or tag. Set `prior_sha` to the full commit SHA of the previous approved backend release. Confirm the workflow run's SHA matches the candidate. The regular push and pull request workflow runs the scripted suite, backend and browser tests, advisories, and a container recovery drill. The manually dispatched run also builds both revisions and qualifies them against isolated PostgreSQL 17. [GitHub manual workflow runs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)

For a local reproduction, check out the exact candidate in a clean directory and set `PRIOR_SHA` to the same prior commit. Run from the candidate checkout with Docker available.

```bash
set -euo pipefail
test -z "$(git status --porcelain)"
test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"
evidence=$(mktemp -d)
prior_source=$(mktemp -d)
git archive "$PRIOR_SHA" backend frontend/package-lock.json | tar -x -C "$prior_source"
git show "$PRIOR_SHA:docs/evaluation-results.json" > "$evidence/prior-reference-report.json"
printf '%s\n' "$PRIOR_SHA" > "$evidence/prior-sha.txt"
(cd "$prior_source" && shasum -a 256 backend/uv.lock frontend/package-lock.json \
  backend/evals/fixtures/*.json backend/migrations/*.sql) > "$evidence/prior-input-sha256.txt"
docker build --tag "dealroom-prior:$PRIOR_SHA" "$prior_source/backend"
BASELINE_IMAGE="dealroom-prior:$PRIOR_SHA" RELEASE_EVIDENCE_DIR="$evidence" \
  bash scripts/release-smoke.sh 2>&1 | tee "$evidence/release-smoke.log"
printf 'Evidence directory %s\n' "$evidence"
```

The `scripted-evaluations-<SHA>` artifact contains the candidate evaluation report. The `release-evidence-<SHA>` artifact records the source revisions, Docker image IDs, applied migrations, execution identities, lockfile and fixture fingerprints, scripted results and smoke log. The prior scripted report uses the prior fixtures. Compare directly only when fixture fingerprints match.

The smoke log covers two migration runs, API compatibility, queued analysis for both builds, model-budget deferral, one delivery matching a prior approval, evaluation worker loss and recovery, and logical restore. The workload covers 6,000 unique messages, a replay for every fifth event, 60 synthetic workspaces and 16 concurrent clients. It exercises PostgreSQL admission and queue claims. Record HTTPS ingress, model processing and relay outcomes separately in staging.

## Candidate record

| Field | Evidence |
| --- | --- |
| Candidate SHA, previous approved SHA and approving maintainer | Pending |
| Candidate and prior image IDs, deployed image digests and PostgreSQL image | Pending |
| Applied migrations and execution identities for both builds | Pending |
| Candidate and prior fixture, migration and lockfile SHA-256 fingerprints | Pending |
| Baseline report, candidate report and critical assertion failures | Pending |
| CI run URL, backend and browser totals, build and advisory outcomes | Pending |
| Mixed worker drain, deferred work, prior approval and restored receipt | Pending |
| Workload counts, admission p95 and drain duration | Pending |
| Rollback release, operator and queue disposition | Pending |
| Promotion decision, approver and date | Pending |

## Staging evidence required for promotion

| Check | Initial status |
| --- | --- |
| Protected deployment smoke, signed HTTPS ingress, replays, ordering and burst | Pending authorized staging run |
| Approved live model review, model quota, slow relay and ambiguous-send reconciliation | Pending provider approval and staging run |
| On-call alert delivery and managed backup restore into a separate environment | Pending staging drill |
| Creator retention across stores, trace and provider copies, and backup expiry | Pending staging drill |

For rollback, stop promotion and identify pending work by execution identity. Use the previous API and worker image on the upgraded schema only after the compatibility drill passes. Keep workers for any identity that still has queued or deferred work. When restoring a database, pause ingress and delivery, restore to a separate service and reconcile provider delivery keys before resuming. [Operations runbook](operations.md#rollback-and-handoff)
