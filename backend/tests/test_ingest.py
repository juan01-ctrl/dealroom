from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from psycopg.errors import UniqueViolation

from app.storage import Conflict, NotFound


def test_concurrent_replay_returns_same_receipt(store):
    tenant, thread = store.create_session()
    identifier = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(lambda _: store.ingest(tenant, thread, identifier, "USD 1200"), range(2))
        )
    assert receipts[0] == receipts[1]
    assert len(store.snapshot(tenant, thread)["messages"]) == 1
    with pytest.raises(Conflict):
        store.ingest(tenant, thread, identifier, "Changed payload")


def test_event_and_job_commit_together(store):
    tenant, thread = store.create_session()
    receipt = store.ingest(tenant, thread, str(uuid4()), "USD 1200")
    with store.connect() as db:
        row = db.execute("SELECT kind FROM jobs WHERE id = %s", (receipt["job_id"],)).fetchone()
    assert row["kind"] == "analyze"


def test_thread_scope_and_close(store):
    tenant, thread = store.create_session()
    other, _ = store.create_session()
    with pytest.raises(NotFound):
        store.snapshot(other, thread)
    store.transition(tenant, thread, 0, "close")
    with pytest.raises(Conflict):
        store.ingest(tenant, thread, str(uuid4()), "USD 1200")
    store.transition(tenant, thread, 1, "reopen")
    assert store.ingest(tenant, thread, str(uuid4()), "USD 1200")["revision"] == 3


def test_enqueue_conflict_rolls_back_message_and_revision(store, monkeypatch):
    tenant, thread = store.create_session()
    unrelated = store.ingest(tenant, thread, str(uuid4()), "First email")
    monkeypatch.setattr("app.storage.uuid4", lambda: unrelated["job_id"])
    with pytest.raises(UniqueViolation):
        store.ingest(tenant, thread, str(uuid4()), "Second email")
    state = store.snapshot(tenant, thread)
    assert state["revision"] == 1
    assert [message["body"] for message in state["messages"]] == ["First email"]
