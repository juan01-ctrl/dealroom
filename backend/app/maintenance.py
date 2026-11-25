from langgraph.checkpoint.postgres import PostgresSaver


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
    return erase_demo_tenants(store, [row["id"] for row in expired])
