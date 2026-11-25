import os
import subprocess
import sys
from uuid import uuid4

from test_workflow import EMAIL

from app.worker import Worker


def run_worker_process(store, job_id):
    subprocess.run(
        [sys.executable, "-m", "app.worker", "--once", str(job_id)],
        check=True,
        timeout=20,
        env=os.environ | {"DATABASE_URL": store.url, "MODEL_MODE": "scripted"},
    )


def test_worker_resumes_from_postgres_checkpoint(store):
    tenant, thread = store.create_session()
    store.arm_fault(tenant, thread, 0, "checkpoint")
    receipt = store.ingest(tenant, thread, str(uuid4()), EMAIL)
    run_worker_process(store, receipt["job_id"])
    assert store.snapshot(tenant, thread)["status"] == "ANALYZING"
    run_worker_process(store, receipt["job_id"])
    snapshot = store.snapshot(tenant, thread)
    assert snapshot["status"] == "AWAITING_APPROVAL"
    assert sum(row["kind"] == "draft_created" for row in snapshot["timeline"]) == 1
    assert any(row["kind"] == "job_recovered" for row in snapshot["timeline"])


def test_delivery_commit_then_worker_restart_is_idempotent(store):
    tenant, thread = store.create_session()
    receipt = store.ingest(tenant, thread, str(uuid4()), EMAIL)
    Worker(store).step(receipt["job_id"])
    draft = store.snapshot(tenant, thread)["drafts"][0]
    store.arm_fault(tenant, thread, 1, "delivery")
    approval = store.review(tenant, thread, draft["id"], 1, "approve")
    run_worker_process(store, approval)
    assert len(store.snapshot(tenant, thread)["deliveries"]) == 1
    run_worker_process(store, approval)
    timeline = store.snapshot(tenant, thread)["timeline"]
    assert sum(row["kind"] == "simulated_delivery" for row in timeline) == 1
    assert any(row["kind"] == "job_recovered" for row in timeline)


def test_expired_worker_cannot_acknowledge_new_lease(store):
    tenant, thread = store.create_session()
    receipt = store.ingest(tenant, thread, str(uuid4()), EMAIL)
    first = store.claim("first", identifier=receipt["job_id"], lease=0)
    second = store.claim("second", identifier=receipt["job_id"])
    assert not store.finish(first)
    assert store.finish(second)


def test_policy_update_restarts_failed_analysis_with_current_policy(store):
    from app.domain import Policy

    tenant, thread = store.create_session()
    receipt = store.ingest(tenant, thread, str(uuid4()), EMAIL)
    for _ in range(4):
        job = store.claim("failed-worker", identifier=receipt["job_id"])
        store.finish(job, retry=True, immediate=True)
    assert store.snapshot(tenant, thread)["status"] == "FAILED"
    store.update_policy(tenant, 1, Policy(exclusivity_pct=40))
    with store.connect() as db:
        new_job = db.execute(
            "SELECT id FROM jobs WHERE tenant_id=%s AND thread_id=%s AND state='QUEUED'",
            (tenant, thread),
        ).fetchone()
    assert new_job
    Worker(store).step(new_job["id"])
    state = store.snapshot(tenant, thread)
    assert state["status"] == "AWAITING_APPROVAL"
    assert state["drafts"][-1]["decision"]["total"] == 280000
