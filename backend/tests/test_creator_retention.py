import json
from uuid import uuid4

import pytest
from test_real_mail import FakeProvider, approve, route_address, verified_payload

from app.identity import IdentityClaims, IdentityStore
from app.mail_delivery import MailDeliveryPending, MailStore, draft_envelope
from app.models import ScriptedModel
from app.storage import NotFound
from app.worker import Worker


@pytest.fixture
def campaign(store):
    member = IdentityStore(store).provision_workspace(
        IdentityClaims("https://retention.example.test", str(uuid4()), "creator@example.test")
    )
    thread = store.create_thread(member.tenant_id, "Synthetic retention campaign")
    mail = MailStore(store)
    mail.configure_route(
        member.tenant_id,
        thread,
        provider="relay",
        inbound_address=route_address(thread),
        sender="creator@send.example.test",
        recipient="brand@example.test",
    )
    return member, thread, mail


def close(store, tenant, thread, *, days):
    current = store.snapshot(tenant, thread)
    store.transition(tenant, thread, current["revision"], "close")
    with store.connect() as db:
        db.execute(
            """UPDATE timeline SET created_at=now()-(%s * interval '1 day')
               WHERE tenant_id=%s AND thread_id=%s AND kind='state_transition'
               AND details->>'status'='CLOSED'""",
            (days, tenant, thread),
        )


def age_incoming(store, tenant, thread, days):
    with store.connect() as db:
        db.execute(
            """UPDATE messages SET created_at=now()-(%s * interval '1 day')
               WHERE tenant_id=%s AND thread_id=%s""",
            (days, tenant, thread),
        )


def checkpoints(store, job_id):
    with store.connect() as db:
        return [
            db.execute(f"SELECT 1 FROM {table} WHERE thread_id=%s LIMIT 1", (str(job_id),)).fetchone()
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
        ]


def run_retention(store, workspace, *, apply=False, replay_days=30, limit=100):
    from app.maintenance import retain_creator_negotiations

    return retain_creator_negotiations(
        store, workspace=workspace, retention_days=10, replay_days=replay_days, limit=limit, apply=apply
    )


def approve_campaign(store, member, thread, mail, provider):
    receipt = mail.ingest(verified_payload(thread))
    assert Worker(store, model=ScriptedModel(), mail_provider=provider).step(receipt["job_id"])
    with store.connect() as db:
        draft = db.execute("SELECT * FROM drafts WHERE thread_id=%s", (thread,)).fetchone()
        envelope = draft_envelope(db, member.tenant_id, thread, draft["analysis_id"], draft["body"])
    return approve(store, member, thread, draft, envelope.digest)


def test_preview_preserves_data_and_apply_erases_checkpoint_and_route(
    store, campaign, monkeypatch, capsys
):
    from app.retention import main

    member, thread, mail = campaign
    tenant = member.tenant_id
    incoming = verified_payload(thread)
    receipt = mail.ingest(incoming)
    assert Worker(store, model=ScriptedModel(), mail_provider=FakeProvider()).step(receipt["job_id"])
    close(store, tenant, thread, days=35)
    age_incoming(store, tenant, thread, days=35)
    assert all(checkpoints(store, receipt["job_id"]))

    monkeypatch.setenv("DATABASE_URL", store.url)
    args = [str(tenant), "--retention-days", "10", "--replay-days", "30"]
    main(args)
    preview = json.loads(capsys.readouterr().out)
    assert preview == {
        "mode": "preview",
        "eligible_negotiations": 1,
        "checkpoint_threads": 1,
        "deleted_negotiations": 0,
        "skipped_negotiations": 0,
    }
    assert mail.ingest(incoming) == receipt
    assert all(checkpoints(store, receipt["job_id"]))

    main([*args, "--apply"])
    applied = json.loads(capsys.readouterr().out)
    assert applied == {**preview, "mode": "apply", "deleted_negotiations": 1}
    assert checkpoints(store, receipt["job_id"]) == [None, None, None]
    with pytest.raises(NotFound):
        store.snapshot(tenant, thread)
    with pytest.raises(NotFound):
        mail.ingest(incoming)
    assert store.policy(tenant) is not None


def test_replay_window_and_latest_close_protect_original_receipt(store, campaign):
    member, thread, mail = campaign
    tenant = member.tenant_id
    incoming = verified_payload(thread)
    receipt = mail.ingest(incoming)
    assert Worker(store, model=ScriptedModel(), mail_provider=FakeProvider()).step(receipt["job_id"])
    close(store, tenant, thread, days=35)
    age_incoming(store, tenant, thread, days=20)
    assert run_retention(store, tenant, apply=True)["eligible_negotiations"] == 0
    assert mail.ingest(incoming) == receipt

    age_incoming(store, tenant, thread, days=35)
    store.transition(tenant, thread, store.snapshot(tenant, thread)["revision"], "reopen")
    store.transition(tenant, thread, store.snapshot(tenant, thread)["revision"], "close")
    assert run_retention(store, tenant, apply=True)["eligible_negotiations"] == 0


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "FAILED", "DONE"])
def test_unfinished_work_or_held_worker_fence_protect_closed_negotiation(store, campaign, state):
    member, thread, mail = campaign
    tenant = member.tenant_id
    receipt = mail.ingest(verified_payload(thread))
    close(store, tenant, thread, days=35)
    age_incoming(store, tenant, thread, days=35)
    with store.connect() as db:
        db.execute("UPDATE jobs SET state=%s WHERE id=%s", (state, receipt["job_id"]))
    with store.connect() as lock:
        lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(receipt["job_id"]),))
        report = run_retention(store, tenant, apply=True)
    assert report["eligible_negotiations"] == (1 if state == "DONE" else 0)
    assert report["skipped_negotiations"] == (1 if state == "DONE" else 0)
    assert report["deleted_negotiations"] == 0
    assert store.snapshot(tenant, thread)["status"] == "CLOSED"


def test_an_approval_without_an_attempt_remains_protected(store, campaign):
    member, thread, mail = campaign
    tenant = member.tenant_id
    approval = approve_campaign(store, member, thread, mail, FakeProvider())
    close(store, tenant, thread, days=35)
    age_incoming(store, tenant, thread, days=35)
    with store.connect() as db:
        db.execute("UPDATE jobs SET state='DONE' WHERE id=%s", (approval,))
    assert run_retention(store, tenant, apply=True)["eligible_negotiations"] == 0


def test_pending_handoff_and_recent_provider_receipt_are_protected(store, campaign):
    member, thread, mail = campaign
    tenant = member.tenant_id
    provider = FakeProvider(ambiguous=True)
    approval = approve_campaign(store, member, thread, mail, provider)
    with pytest.raises(MailDeliveryPending):
        mail.deliver(tenant, thread, approval, provider)
    close(store, tenant, thread, days=35)
    age_incoming(store, tenant, thread, days=35)
    with store.connect() as db:
        db.execute("UPDATE jobs SET state='DONE' WHERE id=%s", (approval,))
        db.execute(
            "UPDATE mail_attempts SET updated_at=now()-interval '35 days' WHERE approval_id=%s",
            (approval,),
        )
    assert run_retention(store, tenant, apply=True)["eligible_negotiations"] == 0

    provider.visible = True
    receipt, _ = mail.reconcile(tenant, thread, approval, provider)
    assert receipt["provider_receipt_id"]
    assert run_retention(store, tenant, apply=True)["eligible_negotiations"] == 0

    with store.connect() as db:
        db.execute(
            "UPDATE deliveries SET created_at=now()-interval '35 days' WHERE delivery_key=%s",
            (approval,),
        )
        db.execute(
            "UPDATE mail_attempts SET updated_at=now()-interval '35 days' WHERE approval_id=%s",
            (approval,),
        )
    with store.connect() as lock:
        lock.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"mail-delivery:{tenant}:{approval}",),
        )
        assert run_retention(store, tenant, apply=True)["skipped_negotiations"] == 1
    assert run_retention(store, tenant, apply=True)["deleted_negotiations"] == 1


def test_retention_is_scoped_to_the_requested_creator_workspace(store, campaign):
    member, thread, _ = campaign
    tenant = member.tenant_id
    close(store, tenant, thread, days=35)
    other = IdentityStore(store).provision_workspace(
        IdentityClaims("https://retention.example.test", str(uuid4()), "other@example.test")
    )
    other_thread = store.create_thread(other.tenant_id, "Other workspace")
    demo, demo_thread = store.create_session()
    close(store, other.tenant_id, other_thread, days=35)
    close(store, demo, demo_thread, days=35)
    assert run_retention(store, demo, apply=True)["eligible_negotiations"] == 0
    assert run_retention(store, tenant, apply=True)["deleted_negotiations"] == 1
    assert store.snapshot(other.tenant_id, other_thread)["status"] == "CLOSED"
    assert store.snapshot(demo, demo_thread)["status"] == "CLOSED"


def test_retention_applies_at_most_the_requested_batch(store, campaign):
    member, oldest, _ = campaign
    tenant = member.tenant_id
    closed = store.create_thread(tenant, "Later closure")
    close(store, tenant, oldest, days=40)
    close(store, tenant, closed, days=35)
    assert run_retention(store, tenant, apply=True, limit=1)["deleted_negotiations"] == 1
    with pytest.raises(NotFound):
        store.snapshot(tenant, oldest)
    assert store.snapshot(tenant, closed)["status"] == "CLOSED"


def test_daily_usage_cleanup_is_bounded_and_keeps_recent_days(store, campaign):
    from app.maintenance import expire_sessions

    member, _, _ = campaign
    with store.connect() as db:
        db.execute(
            """INSERT INTO creator_mail_daily_usage (tenant_id,usage_date,messages)
               SELECT %s,(now() AT TIME ZONE 'UTC')::date-age,1
               FROM generate_series(0,1033) age""",
            (member.tenant_id,),
        )
    expire_sessions(store)
    with store.connect() as db:
        assert db.execute(
            """SELECT (now() AT TIME ZONE 'UTC')::date-usage_date AS age
               FROM creator_mail_daily_usage WHERE tenant_id=%s ORDER BY age""",
            (member.tenant_id,),
        ).fetchall() == [{"age": days} for days in range(34)]
