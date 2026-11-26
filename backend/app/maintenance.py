from langgraph.checkpoint.postgres import PostgresSaver

_ELIGIBLE_CREATOR_NEGOTIATIONS = """
    SELECT n.id
    FROM negotiations n JOIN creator_workspaces w ON w.tenant_id=n.tenant_id
    JOIN LATERAL (
        SELECT t.created_at FROM timeline t
        WHERE t.tenant_id=n.tenant_id AND t.thread_id=n.id AND t.revision=n.revision
          AND t.kind='state_transition' AND t.details->>'status'='CLOSED'
        ORDER BY t.id DESC LIMIT 1
    ) closed ON true
    WHERE n.tenant_id=%(tenant)s AND n.status='CLOSED'
      AND (%(thread)s::uuid IS NULL OR n.id=%(thread)s)
      AND closed.created_at < now() - GREATEST(%(retention)s, %(replay)s) * interval '24 hours'
      AND NOT EXISTS (
          SELECT 1 FROM jobs j WHERE (j.tenant_id,j.thread_id)=(n.tenant_id,n.id)
          AND j.state NOT IN ('DONE','SKIPPED')
      )
      AND NOT EXISTS (
          SELECT 1 FROM messages m
          WHERE (m.tenant_id,m.thread_id)=(n.tenant_id,n.id)
            AND m.created_at >= now() - %(replay)s * interval '24 hours'
      )
      AND NOT EXISTS (
          SELECT 1 FROM drafts d
          JOIN approvals a ON (a.tenant_id,a.draft_id)=(d.tenant_id,d.id)
          LEFT JOIN mail_approvals ma ON (ma.tenant_id,ma.approval_id)=(a.tenant_id,a.id)
          LEFT JOIN mail_attempts s ON (s.tenant_id,s.approval_id)=(a.tenant_id,a.id)
          LEFT JOIN deliveries r ON (r.tenant_id,r.thread_id,r.delivery_key)=(a.tenant_id,d.thread_id,a.id)
          WHERE (d.tenant_id,d.thread_id)=(n.tenant_id,n.id)
            AND (
                NOT COALESCE(
                    (s.state='ACCEPTED' AND r.mail_provider=ma.provider AND r.provider_receipt_id IS NOT NULL)
                    OR (s.state IN ('REJECTED','BLOCKED') AND r.delivery_key IS NULL), false
                )
                OR s.updated_at >= now() - %(replay)s * interval '24 hours'
                OR r.created_at >= now() - %(replay)s * interval '24 hours'
            )
      )
    ORDER BY closed.created_at,n.id
    LIMIT %(limit)s
"""


def retain_creator_negotiations(
    store, *, workspace, retention_days, replay_days, limit=100, apply=False
):
    """Preview a bounded batch or erase it under the normal mutation and execution fences."""
    if not 1 <= retention_days <= 36500 or not 1 <= replay_days <= 36500 or not 1 <= limit <= 1000:
        raise ValueError("Retention and replay windows must be 1–36500 days. Batch size must be 1–1000.")
    criteria = {
        "tenant": workspace,
        "thread": None,
        "retention": retention_days,
        "replay": replay_days,
        "limit": limit,
    }
    with store.connect() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        candidates = [row["id"] for row in db.execute(_ELIGIBLE_CREATOR_NEGOTIATIONS, criteria)]
        checkpoint_threads = db.execute(
            """SELECT count(*) AS n FROM jobs j
               WHERE j.tenant_id=%s AND j.thread_id=ANY(%s) AND j.kind='analyze'
               AND (EXISTS (SELECT 1 FROM checkpoints c WHERE c.thread_id=j.id::text)
                 OR EXISTS (SELECT 1 FROM checkpoint_blobs b WHERE b.thread_id=j.id::text)
                 OR EXISTS (SELECT 1 FROM checkpoint_writes w WHERE w.thread_id=j.id::text))""",
            (workspace, candidates),
        ).fetchone()["n"]
    deleted = 0
    if apply:
        deleted = sum(
            _erase_creator_negotiation(store, criteria | {"thread": thread}) for thread in candidates
        )
    return {
        "mode": "apply" if apply else "preview",
        "eligible_negotiations": len(candidates),
        "checkpoint_threads": checkpoint_threads,
        "deleted_negotiations": deleted,
        "skipped_negotiations": len(candidates) - deleted if apply else 0,
    }


def _erase_creator_negotiation(store, criteria):
    tenant, thread = criteria["tenant"], criteria["thread"]
    with store.connect() as db:
        if not db.execute(
            "SELECT 1 FROM tenants WHERE id=%s FOR UPDATE SKIP LOCKED", (tenant,)
        ).fetchone():
            return False
        if not db.execute(
            "SELECT 1 FROM negotiations WHERE tenant_id=%s AND id=%s FOR UPDATE SKIP LOCKED",
            (tenant, thread),
        ).fetchone():
            return False
        if not db.execute(_ELIGIBLE_CREATOR_NEGOTIATIONS, criteria).fetchone():
            return False

        jobs = db.execute(
            "SELECT id,kind FROM jobs WHERE tenant_id=%s AND thread_id=%s ORDER BY id", (tenant, thread)
        ).fetchall()
        approvals = db.execute(
            """SELECT a.id FROM approvals a JOIN drafts d ON (d.tenant_id,d.id)=(a.tenant_id,a.draft_id)
               WHERE d.tenant_id=%s AND d.thread_id=%s ORDER BY a.id""",
            (tenant, thread),
        ).fetchall()
        fence_keys = [str(job["id"]) for job in jobs]
        fence_keys += [f"mail-delivery:{tenant}:{approval['id']}" for approval in approvals]
        if not all(
            db.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired", (key,)
            ).fetchone()["acquired"]
            for key in fence_keys
        ):
            return False

        saver = PostgresSaver(db)
        for job in jobs:
            if job["kind"] == "analyze":
                saver.delete_thread(str(job["id"]))
        db.execute(
            """DELETE FROM preferences p USING drafts d
               WHERE (p.tenant_id,p.id)=(d.tenant_id,d.id) AND d.tenant_id=%s AND d.thread_id=%s""",
            (tenant, thread),
        )
        db.execute("DELETE FROM negotiations WHERE tenant_id=%s AND id=%s", (tenant, thread))
        return True


def erase_demo_tenants(store, tenants):
    """Delete synthetic data and checkpoint copies under the worker's job fences."""
    deleted = 0
    for tenant in tenants:
        with store.connect() as db:
            if not db.execute(
                """SELECT id FROM tenants WHERE id=%s AND NOT EXISTS (
                       SELECT 1 FROM creator_workspaces w WHERE w.tenant_id=tenants.id
                   ) FOR UPDATE""",
                (tenant,),
            ).fetchone():
                continue
            jobs = db.execute(
                "SELECT id,kind FROM jobs WHERE tenant_id=%s ORDER BY id", (tenant,)
            ).fetchall()
            if not all(
                db.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired",
                    (str(job["id"]),),
                ).fetchone()["acquired"]
                for job in jobs
            ):
                continue
            saver = PostgresSaver(db)
            for job in jobs:
                if job["kind"] == "analyze":
                    saver.delete_thread(str(job["id"]))
            db.execute("DELETE FROM tenants WHERE id=%s", (tenant,))
            deleted += 1
    return deleted


def expire_sessions(store):
    with store.connect() as db:
        expired = db.execute(
            """SELECT id FROM tenants WHERE created_at < now() - interval '7 days'
               AND NOT EXISTS (SELECT 1 FROM creator_workspaces w WHERE w.tenant_id=tenants.id)
               ORDER BY created_at LIMIT 100"""
        ).fetchall()
        db.execute("DELETE FROM worker_heartbeats WHERE seen_at < now()-interval '1 day'")
        db.execute(
            """WITH expired AS (
                   SELECT tenant_id,usage_date FROM creator_mail_daily_usage
                   WHERE usage_date < (now() AT TIME ZONE 'UTC')::date - 31
                   ORDER BY usage_date LIMIT 1000 FOR UPDATE SKIP LOCKED
               ) DELETE FROM creator_mail_daily_usage u USING expired e
                 WHERE (u.tenant_id,u.usage_date)=(e.tenant_id,e.usage_date)"""
        )
    return erase_demo_tenants(store, [row["id"] for row in expired])
