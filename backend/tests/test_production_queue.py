from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from test_real_mail import creator_setup, gateway, verified_payload  # noqa: F401

from app.evaluations import run_job
from app.mail_delivery import CreatorMailBackpressure, MailStore
from app.models import ScriptedModel
from app.operations import execution_identity, heartbeat, operations_status
from app.storage import Limit, ModelBudgetExhausted
from app.worker import Worker
from app.workflow import run_analysis


def test_creator_can_continue_after_demo_lifetime_limit(store, creator_setup):  # noqa: F811
    member, thread, mail = creator_setup
    tenant = member.tenant_id
    for revision in range(13):
        store.create_thread(tenant, f"Campaign {revision}")
    original = verified_payload(thread)
    first = mail.ingest(original, quota=1)
    assert mail.ingest(original, quota=1) == first
    later = verified_payload(thread, event="next")
    with pytest.raises(CreatorMailBackpressure):
        mail.ingest(later, quota=1)
    with store.connect() as db:
        db.execute(
            "UPDATE creator_mail_daily_usage SET usage_date=usage_date-1 WHERE tenant_id=%s",
            (tenant,),
        )
    assert mail.ingest(later, quota=1)["revision"] == 2

    demo, _ = store.create_session()
    for revision in range(11):
        store.create_thread(demo, f"Campaign {revision}")
    with pytest.raises(Limit):
        store.create_thread(demo, "Still subject to demo limits")


def test_concurrent_duplicate_ingress_creates_one_message_job_and_admission(store, creator_setup):  # noqa: F811
    member, thread, _ = creator_setup
    payload = verified_payload(thread)
    barrier = Barrier(8)

    def submit(_):
        barrier.wait(timeout=10)
        return MailStore(store).ingest(payload, quota=1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(submit, range(8)))
    assert all(receipt == receipts[0] for receipt in receipts)
    with store.connect() as db:
        row = db.execute(
            """SELECT
               (SELECT count(*) FROM messages WHERE tenant_id=%(tenant)s) AS messages,
               (SELECT count(*) FROM jobs WHERE tenant_id=%(tenant)s) AS jobs,
               (SELECT sum(messages) FROM creator_mail_daily_usage WHERE tenant_id=%(tenant)s) AS admissions""",
            {"tenant": member.tenant_id},
        ).fetchone()
    assert row == {"messages": 1, "jobs": 1, "admissions": 1}


def test_replay_after_publish_reuses_single_draft(store):
    tenant, thread = store.create_session()
    first = store.ingest(tenant, thread, str(uuid4()), "USD 2,400 for one reel.")
    second = store.ingest(tenant, thread, str(uuid4()), "USD 2,500 works for the same scope.")
    other_tenant, other = store.create_session()
    independent = store.ingest(other_tenant, other, str(uuid4()), "USD 2,100 for one reel.")
    claim = store.claim("first", identifier=first["job_id"], lane="analysis")
    assert store.claim("second", identifier=second["job_id"], lane="analysis") is None
    other_claim = store.claim("other", identifier=independent["job_id"], lane="analysis")
    assert other_claim and store.finish(other_claim)

    with store.connect(autocommit=True) as db:
        run_analysis(store, PostgresSaver(db), claim, ScriptedModel())
        db.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (claim["id"],))
    worker = Worker(store, model=ScriptedModel(), immediate=True, lane="analysis")
    assert worker.step(first["job_id"])
    assert not store.finish(claim)
    assert worker.step(second["job_id"])
    drafts = store.snapshot(tenant, thread)["drafts"]
    # The superseded first offer is kept for context. The final revision gets one reviewable draft.
    assert [draft["revision"] for draft in drafts] == [2]


def test_published_current_revision_is_not_duplicated_after_lease_expiry(store):
    tenant, thread = store.create_session()
    receipt = store.ingest(tenant, thread, str(uuid4()), "USD 2,400 for one reel.")
    claim = store.claim("original", identifier=receipt["job_id"], lane="analysis")
    with store.connect(autocommit=True) as db:
        run_analysis(store, PostgresSaver(db), claim, ScriptedModel())
        db.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (claim["id"],))
    original = store.snapshot(tenant, thread)["drafts"][0]
    worker = Worker(store, model=ScriptedModel(), immediate=True, lane="analysis")
    assert worker.step(claim["id"])
    assert not store.finish(claim)
    assert store.snapshot(tenant, thread)["drafts"] == [original]


def test_budget_exhaustion_preserves_job_until_utc_reset(store):
    class AtLimit(ScriptedModel):
        def extract(self, *_):
            raise ModelBudgetExhausted

    tenant, thread = store.create_session()
    receipt = store.ingest(tenant, thread, str(uuid4()), "USD 2,200 for one reel.")
    worker = Worker(store, model=AtLimit(), lane="analysis", immediate=True)
    assert worker.step(receipt["job_id"])
    assert not worker.step(receipt["job_id"])
    with pytest.raises(ModelBudgetExhausted):
        run_job(worker, receipt["job_id"])
    with store.connect() as db:
        queued = db.execute(
            "SELECT state,attempts,available_at FROM jobs WHERE id=%s", (receipt["job_id"],)
        ).fetchone()
    assert (queued["state"], queued["attempts"]) == ("QUEUED", 0)
    utc = queued["available_at"].astimezone(UTC)
    assert utc > datetime.now(UTC) and (utc.hour, utc.minute) == (0, 0)
    assert store.snapshot(tenant, thread)["status"] == "ANALYZING"


def test_unserviceable_revision_is_visible_to_release_operator(store, monkeypatch):
    monkeypatch.setenv("MODEL_MODE", "live")
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("MODEL_NAME", "prior-model")
    old = execution_identity()
    tenant, thread = store.create_session(str(uuid4()))
    receipt = store.ingest(tenant, thread, str(uuid4()), "USD 2,100 for one reel.")
    monkeypatch.setenv("MODEL_NAME", "next-model")
    new = execution_identity()
    heartbeat(store, str(uuid4()), "analysis", new)
    heartbeat(store, str(uuid4()), "delivery", new)

    assert store.claim("new", identifier=receipt["job_id"], lane="analysis", identity=new) is None
    status = operations_status(store, require_evaluation=False)
    assert status["workers"]["negotiation"] is True
    backlog = next(group for group in status["analysis_execution_backlog"] if group["identity"] == old)
    assert backlog == {
        "identity": old,
        "queued": 1,
        "running": 0,
        "failed": 0,
        "claimant_present": False,
    }
    assert status["status"] == "degraded"
    heartbeat(store, str(uuid4()), "analysis", old)
    backlog = next(
        group
        for group in operations_status(store, require_evaluation=False)["analysis_execution_backlog"]
        if group["identity"] == old
    )
    assert backlog["claimant_present"]
    claim = store.claim("old", identifier=receipt["job_id"], lane="analysis", identity=old)
    assert store.finish(claim)


def test_signed_ingress_backpressure(store, creator_setup, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient
    from test_real_mail import NOW, SECRET, route_address, signed_inbound

    from app.api import create_app
    from app.mail_delivery import InboundGateway

    _, thread, _ = creator_setup
    monkeypatch.setenv("CREATOR_DAILY_MAIL_LIMIT", "1")
    app = create_app(store, inbound_gateway=InboundGateway(SECRET, "relay", clock=lambda: NOW))

    def submit(client, event):
        raw, timestamp, signature = signed_inbound(to=route_address(thread), event_id=str(event))
        return client.post(
            "/integrations/mail/inbound",
            content=raw,
            headers={"X-Mail-Timestamp": timestamp, "X-Mail-Signature": signature},
        )

    with TestClient(app) as client:
        event_id = uuid4()
        accepted = submit(client, event_id)
        assert accepted.status_code == 202
        limited = submit(client, uuid4())
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "3600"
        assert submit(client, event_id).json() == accepted.json()


def test_production_api_lifecycle_reuses_pool_and_rolls_back_failed_transactions(store, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.storage import pooled_store

    with pooled_store(store.url, max_size=1) as pooled:
        with pooled.connect() as first:
            pid = first.info.backend_pid
        with pytest.raises(RuntimeError, match="cancel transaction"), pooled.connect() as second:
            assert second.info.backend_pid == pid
            second.execute("CREATE TEMPORARY TABLE pool_rollback_probe (value integer)")
            raise RuntimeError("cancel transaction")
        with pooled.connect() as third:
            assert third.info.backend_pid == pid
            assert third.execute("SELECT to_regclass('pool_rollback_probe') AS probe").fetchone() == {
                "probe": None,
            }

    monkeypatch.setenv("DATABASE_URL", store.url)
    monkeypatch.setenv("OPERATIONS_TOKEN", "local-test-operations-token-only")
    with TestClient(create_app()) as client:
        assert client.get("/ready").status_code == 200
        assert client.post("/api/session", headers={"X-Sandbox-Request": "1"}).status_code == 200
        health = client.get(
            "/api/operations", headers={"X-Operations-Token": "local-test-operations-token-only"}
        )
        assert health.json()["api_connection_pool"]["pool_size"] >= 1
