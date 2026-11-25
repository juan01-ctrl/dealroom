"""Job claims, ownership and terminal-state updates."""

from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .operations import LANES
from .persistence_events import event


class Outbox:
    def __init__(self, store):
        self.store = store

    def claim(
        self, worker, *, identifier=None, lease=120, lane="all", identity=None, real_allowed=False
    ):
        with self.store.connect() as db:
            return db.execute(
                """WITH candidate AS (
                SELECT j.id FROM jobs j WHERE j.available_at<=now()
                AND (j.state='QUEUED' OR (j.state='RUNNING' AND j.lease_until<=now()))
                AND j.kind=ANY(%s)
                AND (j.kind<>'analyze' OR NOT j.payload ? 'execution'
                     OR %s::jsonb IS NULL OR j.payload->'execution'=%s)
                AND ((%s::uuid IS NULL AND (EXISTS (
                    SELECT 1 FROM tenants t WHERE t.id=j.tenant_id AND t.session_hash IS NOT NULL)
                    OR (%s AND EXISTS (SELECT 1 FROM creator_workspaces w WHERE w.tenant_id=j.tenant_id))))
                    OR j.id=%s)
                AND (%s OR NOT EXISTS (SELECT 1 FROM creator_workspaces w WHERE w.tenant_id=j.tenant_id))
                AND (j.kind<>'analyze' OR NOT EXISTS (
                    SELECT 1 FROM jobs earlier
                    WHERE earlier.tenant_id=j.tenant_id AND earlier.thread_id=j.thread_id
                    AND earlier.kind='analyze' AND earlier.revision<j.revision
                    AND earlier.state IN ('QUEUED','RUNNING')))
                ORDER BY j.available_at,j.id FOR UPDATE OF j SKIP LOCKED LIMIT 1
            ) UPDATE jobs j SET state='RUNNING',attempts=attempts+1,
                lease_until=now()+(%s * interval '1 second'), lease_token=%s, worker_id=%s
                FROM candidate WHERE j.id=candidate.id RETURNING j.*""",
                (
                    list(LANES[lane]),
                    Jsonb(identity) if identity else None,
                    Jsonb(identity) if identity else None,
                    identifier,
                    real_allowed,
                    identifier,
                    real_allowed,
                    lease,
                    uuid4(),
                    worker,
                ),
            ).fetchone()

    def renew_lease(self, job):
        with self.store.connect(connect_timeout=3) as db:
            db.execute("SET LOCAL statement_timeout=3000")
            row = db.execute(
                """UPDATE jobs SET lease_until=now()+interval '90 seconds'
                   WHERE id=%s AND lease_token=%s AND state='RUNNING' RETURNING id""",
                (job["id"], job["lease_token"]),
            ).fetchone()
            return row is not None

    def finish(self, job, *, retry=False, simulated=False, busy=False, immediate=False, terminal=False):
        tenant, thread, revision = job["tenant_id"], job["thread_id"], job["revision"]
        state = (
            "QUEUED"
            if busy or (retry and not terminal and (simulated or job["attempts"] < 4))
            else ("FAILED" if retry else "DONE")
        )
        delay = 0 if immediate else (1 if busy or simulated else min(2 ** job["attempts"], 30))
        with self.store.connect() as db:
            current = None
            if thread and not busy and (retry or job["attempts"] > 1):
                current = self.store.thread(db, tenant, thread, lock=True)
            updated = db.execute(
                """UPDATE jobs SET state=%s,lease_until=NULL,available_at=now()+(%s * interval '1 second'),
                                 attempts=attempts-(CASE WHEN %s THEN 1 ELSE 0 END)
                                 WHERE id=%s AND lease_token=%s AND state='RUNNING' RETURNING id""",
                (state, delay, busy, job["id"], job["lease_token"]),
            ).fetchone()
            if not updated:
                return False
            if current is not None:
                event(
                    db,
                    tenant,
                    thread,
                    revision,
                    "job_retry"
                    if state == "QUEUED"
                    else ("job_failed" if state == "FAILED" else "job_recovered"),
                    attempt=job["attempts"],
                    job=job["kind"],
                )
                if (
                    state == "FAILED"
                    and current["revision"] == revision
                    and current["status"] in {"ANALYZING", "APPROVED"}
                ):
                    self.store.set_state(db, tenant, thread, "FAILED", revision)
            if state == "FAILED" and job["kind"] == "evaluate":
                db.execute(
                    """UPDATE eval_runs SET status='FAILED' WHERE tenant_id=%s AND id=%s
                           AND status<>'COMPLETE'""",
                    (tenant, UUID(job["payload"]["run_id"])),
                )
            return True
