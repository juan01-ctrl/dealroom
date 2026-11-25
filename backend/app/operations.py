import os
from contextlib import contextmanager
from threading import Event, Thread

import psycopg
from psycopg.types.json import Jsonb

WORKFLOW_VERSION = "negotiation-v4"
QUEUE_AGE_LIMIT_SECONDS = 120
LANES = {
    "all": ("analyze", "deliver", "evaluate"),
    "negotiation": ("analyze", "deliver"),
    "evaluation": ("evaluate",),
}


def execution_identity(mode=None):
    mode = mode or os.getenv("MODEL_MODE", "scripted")
    return {
        "workflow": WORKFLOW_VERSION,
        "mode": mode,
        "provider": os.getenv("MODEL_PROVIDER", "") if mode == "live" else "scripted",
        "model": os.getenv("MODEL_NAME", "") if mode == "live" else "scripted",
    }


def heartbeat(store, worker_id, lane, identity):
    with store.connect(connect_timeout=3) as db:
        db.execute(
            """INSERT INTO worker_heartbeats(worker_id,lane,identity)
               VALUES (%s,%s,%s)
               ON CONFLICT(worker_id) DO UPDATE SET seen_at=now(),
               lane=excluded.lane,identity=excluded.identity""",
            (worker_id, lane, Jsonb(identity)),
        )


@contextmanager
def maintain_heartbeat(store, worker_id, lane, identity):
    stop = Event()

    def pulse():
        while not stop.wait(10):
            try:
                heartbeat(store, worker_id, lane, identity)
            except psycopg.Error:
                # Operations health goes degraded when these updates stop reaching PostgreSQL.
                pass

    heartbeat(store, worker_id, lane, identity)
    thread = Thread(target=pulse, name="worker-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=4)


@contextmanager
def maintain_lease(store, job):
    stop = Event()

    def renew():
        while not stop.wait(25):
            try:
                if not store.outbox.renew_lease(job):
                    break
            except psycopg.Error:
                pass

    thread = Thread(target=renew, name="worker-job-lease", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=4)


def operations_status(store, *, require_evaluation):
    with store.connect(connect_timeout=3) as db:
        db.execute("SET LOCAL statement_timeout=3000")
        lanes = {
            row["lane"]
            for row in db.execute(
                """SELECT DISTINCT lane FROM worker_heartbeats WHERE seen_at > now()-interval '35 seconds'
               AND identity=%s""",
                (Jsonb(execution_identity()),),
            )
        }
        queues = db.execute("""
            SELECT kind, count(*) FILTER(WHERE state='QUEUED') AS queued,
                count(*) FILTER(WHERE state='RUNNING') AS running,
                count(*) FILTER(WHERE state='FAILED') AS failed,
                coalesce(max(extract(epoch FROM (now()-available_at)))
                    FILTER(WHERE state='QUEUED'),0)::int AS oldest_ready_seconds
            FROM jobs WHERE state IN ('QUEUED','RUNNING','FAILED')
            GROUP BY kind ORDER BY kind
        """).fetchall()
    workers = {name: name in lanes or "all" in lanes for name in ("negotiation", "evaluation")}
    healthy = workers["negotiation"] and (workers["evaluation"] or not require_evaluation)
    healthy = healthy and all(row["oldest_ready_seconds"] <= QUEUE_AGE_LIMIT_SECONDS for row in queues)
    return {
        "status": "ok" if healthy else "degraded",
        "workers": workers,
        "queues": queues,
        "queue_age_limit_seconds": QUEUE_AGE_LIMIT_SECONDS,
    }
