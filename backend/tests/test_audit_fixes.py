from uuid import uuid4

from fastapi.testclient import TestClient

from app.api import create_app
from app.worker import Worker

HEADERS = {"X-Sandbox-Request": "1"}


def test_failed_predecessors_can_be_retried_and_skipped(store):
    tenant, thread = store.create_session()
    worker = Worker(store, immediate=True)
    initial = store.ingest(tenant, thread, str(uuid4()), "Northstar offers USD 2,100 for one reel.")
    worker.step(initial["job_id"])
    old_jobs = []
    for _ in range(5):
        receipt = store.ingest(tenant, thread, str(uuid4()), "An unresolved email.")
        job_id = receipt["job_id"]
        old_jobs.append(job_id)
        job = store.claim("test", identifier=job_id)
        store.finish(job, retry=True, terminal=True, immediate=True)

    last_job = store.ingest(tenant, thread, str(uuid4()), "Updated budget: USD 2,400.")["job_id"]
    worker.step(last_job)
    snapshot = store.snapshot(tenant, thread)
    assert snapshot["status"] == "FAILED"
    assert [item["job_id"] for item in snapshot["recoverable_turns"]] == [*old_jobs, last_job]
    store.recover_turn(tenant, thread, snapshot["revision"], old_jobs[0], "retry")
    worker.step(old_jobs[0])
    for job_id in old_jobs[1:]:
        store.recover_turn(tenant, thread, snapshot["revision"], job_id, "skip")
    store.recover_turn(tenant, thread, snapshot["revision"], last_job, "retry")
    worker.step(last_job)
    recovered = store.snapshot(tenant, thread)
    assert recovered["status"] == "AWAITING_APPROVAL"
    assert recovered["drafts"][-1]["decision"]["total"] == 240000
    assert recovered["drafts"][-1]["decision"]["deliverables"] == {"reel": 1}


def test_thread_creation_idempotency_and_snapshot_etag(store):
    client = TestClient(create_app(store))
    client.post("/api/session", headers=HEADERS)
    payload = {"label": "Test campaign", "event_id": str(uuid4())}
    created = client.post("/api/negotiations", json=payload, headers=HEADERS)
    assert created.status_code == 201
    assert client.post("/api/negotiations", json=payload, headers=HEADERS).json() == created.json()
    route = "/api/negotiations/" + created.json()["id"]
    initial = client.get(route)
    assert initial.headers["X-Dealroom-Schema"] == "1"
    assert client.get(route, headers={"If-None-Match": initial.headers["etag"]}).status_code == 304
    client.post(
        route + "/inbound",
        headers=HEADERS,
        json={
            "event_id": str(uuid4()),
            "body": "USD 2,200 for a reel",
        },
    )
    changed = client.get(route, headers={"If-None-Match": initial.headers["etag"]})
    assert changed.status_code == 200 and changed.headers["etag"] != initial.headers["etag"]


def test_worker_lanes_and_operations_health(store, monkeypatch):
    from app.operations import execution_identity, heartbeat

    monkeypatch.setenv("OPERATIONS_TOKEN", "test-token-only-for-local-verification")
    tenant, thread = store.create_session(str(uuid4()))
    receipt = store.ingest(tenant, thread, str(uuid4()), "USD 2,200 for a reel")
    evaluation = Worker(store, lane="evaluation")
    assert not evaluation.step(receipt["job_id"])
    worker = Worker(store, lane="negotiation")
    assert worker.step(receipt["job_id"])

    heartbeat(store, worker.worker_id, worker.lane, execution_identity("scripted"))
    heartbeat(store, evaluation.worker_id, evaluation.lane, execution_identity("scripted"))
    client = TestClient(create_app(store))
    assert client.get("/api/operations").status_code == 403
    health = client.get(
        "/api/operations", headers={"X-Operations-Token": "test-token-only-for-local-verification"}
    )
    assert health.status_code == 200
    assert health.json()["workers"] == {"negotiation": True, "evaluation": True}


def test_new_checkpoints_keep_raw_email_and_policy_out_of_graph_state(store):
    from langgraph.checkpoint.postgres import PostgresSaver

    from app.models import ScriptedModel
    from app.workflow import build_graph

    tenant, thread = store.create_session()
    raw = "Test private draft marker. Northstar offers USD 2,100 for one reel."
    receipt = store.ingest(tenant, thread, str(uuid4()), raw)
    Worker(store).step(receipt["job_id"])
    with store.connect(autocommit=True) as db:
        graph = build_graph(ScriptedModel(), PostgresSaver(db))
        state = graph.get_state({"configurable": {"thread_id": str(receipt["job_id"])}}).values
    assert {"email", "policy", "previous", "unprocessed", "last_quote"}.isdisjoint(state)
    assert raw not in str(state)
    assert state["decision"]["total"] == 210000


def test_retention_erases_synthetic_checkpoint_with_the_tenant(store):
    from langgraph.checkpoint.postgres import PostgresSaver

    from app.maintenance import erase_demo_tenants
    from app.models import ScriptedModel
    from app.workflow import build_graph

    tenant, thread = store.create_session()
    receipt = store.ingest(tenant, thread, str(uuid4()), "USD 2,100 for one reel.")
    Worker(store).step(receipt["job_id"])
    assert erase_demo_tenants(store, [tenant]) == 1
    with store.connect(autocommit=True) as db:
        graph = build_graph(ScriptedModel(), PostgresSaver(db))
        snapshot = graph.get_state({"configurable": {"thread_id": str(receipt["job_id"])}})
        assert not snapshot.values
        assert not db.execute("SELECT 1 FROM tenants WHERE id=%s", (tenant,)).fetchone()


def test_real_mail_endpoint_requires_verified_workspace_and_bound_approval(store):
    from test_real_mail import ISSUER, NOW, SECRET, identity_token, signed_inbound

    from app.identity import IdentityGateway, IdentityStore
    from app.mail_delivery import InboundGateway, MailStore, ProviderResult

    gateway = IdentityGateway(SECRET, ISSUER, clock=lambda: NOW)
    owner = IdentityStore(store).provision_workspace(gateway.verify(identity_token()))
    address = f"campaign-{uuid4().hex}@receive.example.test"
    thread = store.create_thread(owner.tenant_id, "Test creator campaign")
    MailStore(store).configure_route(
        owner.tenant_id,
        thread,
        provider="relay",
        inbound_address=address,
        sender="creator@send.example.test",
        recipient="brand@example.test",
    )
    client = TestClient(
        create_app(
            store,
            identity_gateway=gateway,
            inbound_gateway=InboundGateway(SECRET, "relay", clock=lambda: NOW),
        )
    )
    raw, timestamp, signature = signed_inbound(
        to=address, event_id=uuid4().hex, body="Northstar offers USD 3,000 for one reel."
    )
    assert client.post("/integrations/mail/inbound", content=raw).status_code == 401
    ingestion = client.post(
        "/integrations/mail/inbound",
        content=raw,
        headers={"X-Mail-Timestamp": timestamp, "X-Mail-Signature": signature},
    )
    assert ingestion.status_code == 202

    class Provider:
        name = "relay"

        def send(self, key, envelope):
            assert envelope.recipient == "brand@example.test"
            return ProviderResult("accepted", f"receipt-{key}")

        def lookup(self, _key):
            return ProviderResult("unknown")

    worker = Worker(store, mail_provider=Provider())
    assert worker.step(ingestion.json()["job_id"])
    thread_url = f"/api/negotiations/{thread}"
    assert client.get(thread_url).status_code == 401
    access = HEADERS | {
        "Authorization": f"Bearer {identity_token()}",
        "X-Workspace-Id": str(owner.tenant_id),
    }
    draft = client.get(thread_url, headers=access).json()["drafts"][-1]
    route = f"{thread_url}/drafts/{draft['id']}"
    approval = {"action": "approve", "version": draft["version"]}
    assert client.post(route, json=approval, headers=access).status_code == 409
    approval["mail_digest"] = draft["mail"]["digest"]
    accepted = client.post(route, json=approval, headers=access)
    assert accepted.status_code == 202
    assert worker.step(accepted.json()["id"])
    assert client.post(route, json=approval, headers=access).json() == accepted.json()
    receipt = client.get(thread_url, headers=access).json()["deliveries"][0]
    assert receipt["mail_provider"] == "relay" and receipt["provider_receipt_id"]


def test_timeline_cursor_pages_without_repeating_events(store):
    from app.persistence_events import event

    tenant, thread = store.create_session()
    with store.connect() as db:
        for i in range(105):
            event(db, tenant, thread, 0, "test_tick", index=i)
    snapshot = store.snapshot(tenant, thread)
    assert snapshot["timeline_has_more"] and len(snapshot["timeline"]) == 100
    older = store.reads.timeline_page(tenant, thread, snapshot["timeline"][0]["id"])
    assert not older["has_more"]
    assert [entry["details"]["index"] for entry in older["timeline"]] == list(range(5))


def test_only_the_current_worker_can_renew_its_lease(store):
    tenant, thread = store.create_session()
    identifier = store.ingest(tenant, thread, str(uuid4()), "USD 2,100 for one reel.")["job_id"]
    old = store.claim("old", identifier=identifier, lease=0)
    new = store.claim("new", identifier=identifier)
    assert not store.outbox.renew_lease(old)
    assert store.outbox.renew_lease(new)


def test_skip_cancels_running_reanalysis_of_the_same_email(store):
    tenant, thread = store.create_session()
    first_id = store.ingest(tenant, thread, str(uuid4()), "USD 2,200 for a reel")["job_id"]
    first = store.claim("test", identifier=first_id)
    store.finish(first, retry=True, terminal=True, immediate=True)
    with store.connect() as db:
        version = store.tenant_lock(db, tenant)
        current = store.thread(db, tenant, thread, lock=True)
        store.reanalyze(db, tenant, thread, current, version)
    with store.connect() as db:
        replacement_id = db.execute(
            "SELECT id FROM jobs WHERE thread_id=%s AND revision=%s",
            (thread, first["revision"] + 1),
        ).fetchone()["id"]
    replacement = store.claim("test", identifier=replacement_id)
    assert replacement["revision"] == first["revision"] + 1
    store.recover_turn(tenant, thread, replacement["revision"], first_id, "skip")
    Worker(store, immediate=True).process(replacement)
    assert not store.finish(replacement)
    snapshot = store.snapshot(tenant, thread)
    assert snapshot["status"] == "NEW"
    assert snapshot["drafts"] == []
    assert snapshot["recoverable_turns"] == []
