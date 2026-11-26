"""Read aggregate draft outcomes for one creator workspace and time window."""

import argparse
import json
import os
from datetime import UTC, datetime
from uuid import UUID

from .identity import is_creator_workspace
from .storage import NotFound, Store


def collect_pilot_metrics(store: Store, tenant: UUID, since: datetime, until: datetime) -> dict:
    with store.connect() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        db.execute("SET LOCAL statement_timeout=5000")
        if not is_creator_workspace(db, tenant):
            raise NotFound("Creator workspace not found.")
        return db.execute(
            """WITH cohort AS (
                SELECT d.tenant_id,d.thread_id,d.analysis_id,d.digest,d.revision,
                       extract(epoch FROM d.created_at-j.created_at) AS draft_seconds
                FROM drafts d JOIN jobs j ON j.id=d.analysis_id AND j.tenant_id=d.tenant_id
                WHERE d.tenant_id=%s AND d.version=1 AND d.created_at>=%s AND d.created_at<%s
            ), outcomes AS (
                SELECT c.*, state.delivered_original, state.delivered_edited, state.explicit_rejection,
                    execution.cost_usd
                FROM cohort c
                CROSS JOIN LATERAL (
                    SELECT bool_or(d.status='SENT' AND d.digest=c.digest) AS delivered_original,
                           bool_or(d.status='SENT' AND d.digest<>c.digest) AS delivered_edited,
                           bool_or(d.status='REJECTED') AS explicit_rejection
                    FROM drafts d WHERE d.tenant_id=c.tenant_id AND d.analysis_id=c.analysis_id
                ) state
                LEFT JOIN LATERAL (
                    SELECT (details->>'cost_usd')::numeric AS cost_usd
                    FROM timeline t WHERE t.tenant_id=c.tenant_id AND t.thread_id=c.thread_id
                        AND t.revision=c.revision AND t.kind='analysis_completed'
                        AND jsonb_typeof(details->'cost_usd')='number'
                    ORDER BY t.id DESC LIMIT 1
                ) execution ON true
            )
            SELECT count(*) AS reviewable_analyses,
                   count(*) FILTER (WHERE delivered_original) AS delivered_as_written,
                   count(*) FILTER (WHERE delivered_edited) AS delivered_with_body_changes,
                   count(*) FILTER (WHERE explicit_rejection AND NOT (delivered_original OR delivered_edited))
                       AS explicitly_rejected,
                   count(*) FILTER (WHERE NOT (delivered_original OR delivered_edited OR explicit_rejection))
                       AS other_outcomes,
                   round(percentile_cont(0.95) WITHIN GROUP (ORDER BY draft_seconds)::numeric,3)::float8
                       AS p95_seconds_to_reviewable,
                   count(cost_usd) AS analyses_with_cost,
                   CASE WHEN count(cost_usd)=count(*) THEN
                     round(sum(cost_usd) /
                         nullif(count(*) FILTER (WHERE delivered_original OR delivered_edited),0),8)::float8
                   END AS estimated_published_analysis_cost_per_delivered_draft_usd
            FROM outcomes""",
            (tenant, since, until),
        ).fetchone()


def utc_timestamp(raw):
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use an ISO-8601 timestamp with timezone.") from exc
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("Use an ISO-8601 timestamp with timezone.")
    return value.astimezone(UTC)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=UUID)
    parser.add_argument("--since", type=utc_timestamp, required=True)
    parser.add_argument("--until", type=utc_timestamp, required=True)
    args = parser.parse_args()
    if args.since >= args.until:
        parser.error("--until must be later than --since.")
    if not os.getenv("DATABASE_URL"):
        parser.error("Set DATABASE_URL in the authorized operator environment.")
    try:
        metrics = collect_pilot_metrics(
            Store(os.environ["DATABASE_URL"]), args.workspace, args.since, args.until
        )
    except NotFound as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
