import os
from contextlib import contextmanager
from threading import Event, Thread

import psycopg
from psycopg.types.json import Jsonb

WORKFLOW_VERSION = "negotiation-v5"
QUEUE_AGE_LIMIT_SECONDS = 120
MAIL_CONFIRMATION_AGE_LIMIT_SECONDS = 900
LANES = {
    "all": ("analyze", "deliver", "evaluate"),
    "negotiation": ("analyze", "deliver"),
    "evaluation": ("evaluate",),
    "analysis": ("analyze",),
    "delivery": ("deliver",),
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
        identity = execution_identity()
        heartbeats = db.execute(
            """SELECT DISTINCT lane,identity FROM worker_heartbeats
               WHERE seen_at > now()-interval '35 seconds'"""
        ).fetchall()
        lanes = {row["lane"] for row in heartbeats if row["identity"] == identity}
        queues = db.execute("""
            SELECT kind, count(*) FILTER(WHERE state='QUEUED') AS queued,
                count(*) FILTER(WHERE state='QUEUED' AND available_at>now()) AS delayed,
                count(*) FILTER(WHERE state='QUEUED' AND payload->>'deferred_reason'='model_budget')
                    AS budget_deferred,
                count(*) FILTER(WHERE state='RUNNING' AND lease_until<=now()) AS expired_leases,
                count(*) FILTER(WHERE state='RUNNING') AS running,
                count(*) FILTER(WHERE state='FAILED') AS failed,
                coalesce(max(extract(epoch FROM (now()-available_at)))
                    FILTER(WHERE state='QUEUED' AND available_at<=now()),0)::int AS oldest_ready_seconds
            FROM jobs WHERE state IN ('QUEUED','RUNNING','FAILED')
            GROUP BY kind ORDER BY kind
        """).fetchall()
        execution_backlog = db.execute("""
            SELECT payload->'execution' AS identity,
                   count(*) FILTER (WHERE state='QUEUED') AS queued,
                   count(*) FILTER (WHERE state='RUNNING') AS running,
                   count(*) FILTER (WHERE state='FAILED') AS failed
            FROM jobs WHERE kind='analyze' AND state IN ('QUEUED','RUNNING','FAILED')
            GROUP BY payload->'execution'
        """).fetchall()
        pending_mail = db.execute("""
            SELECT count(*) AS pending,
                   coalesce(floor(extract(epoch FROM now()-min(created_at))),0)::int AS oldest_seconds
            FROM mail_attempts WHERE state='PENDING_CONFIRMATION'
        """).fetchone()
    for group in execution_backlog:
        group["claimant_present"] = any(
            worker["lane"] in {"all", "negotiation", "analysis"}
            and (group["identity"] is None or worker["identity"] == group["identity"])
            for worker in heartbeats
        )
    workers = {
        "negotiation": bool({"all", "negotiation"} & lanes) or {"analysis", "delivery"} <= lanes,
        "evaluation": bool({"all", "evaluation"} & lanes),
    }
    healthy = workers["negotiation"] and (workers["evaluation"] or not require_evaluation)
    healthy = healthy and all(row["oldest_ready_seconds"] <= QUEUE_AGE_LIMIT_SECONDS for row in queues)
    healthy = healthy and all(
        row["claimant_present"] or not (row["queued"] or row["running"]) for row in execution_backlog
    )
    healthy = healthy and pending_mail["oldest_seconds"] <= MAIL_CONFIRMATION_AGE_LIMIT_SECONDS
    return {
        "status": "ok" if healthy else "degraded",
        "mail_confirmation": {
            **pending_mail,
            "age_limit_seconds": MAIL_CONFIRMATION_AGE_LIMIT_SECONDS,
        },
        "analysis_execution_backlog": execution_backlog,
        "workers": workers,
        "queues": queues,
        "queue_age_limit_seconds": QUEUE_AGE_LIMIT_SECONDS,
    }
