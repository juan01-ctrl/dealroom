from contextlib import contextmanager
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api import create_app

HEADERS = {"X-Sandbox-Request": "1"}


def seed_history(store, tenant, thread, turns=500, versions=3):
    with store.connect() as db:
        db.execute(
            """INSERT INTO messages (tenant_id,thread_id,event_id,job_id,revision,body)
               SELECT %s,%s,'history-event-' || r,gen_random_uuid(),r,
                      'Brand offer ' || r || ' ' || repeat('x', 900)
               FROM generate_series(1,%s) AS r""",
            (tenant, thread, turns),
        )
        db.execute(
            """INSERT INTO drafts (tenant_id,thread_id,id,analysis_id,version,revision,policy_version,
                                    decision,proposal,body,digest,status)
               SELECT %s,%s,gen_random_uuid(),a.id,v,a.revision,1,
                      '{"action":"clarification","currency":null,"questions":[],"checks":[],"items":{},"advertising":false}',
                      '{"tone":"warm","invite_followup":true}',
                      'Draft ' || a.revision || ' version ' || v || ' ' || repeat('x', 900),
                      'fixture', CASE WHEN a.revision=%s AND v=%s THEN 'PENDING' ELSE 'STALE' END
               FROM (SELECT r AS revision,gen_random_uuid() AS id FROM generate_series(1,%s) AS r) a
               CROSS JOIN generate_series(1,%s) AS v""",
            (tenant, thread, turns, versions, turns, versions),
        )
        db.execute(
            """WITH approved AS (
                 INSERT INTO approvals (tenant_id,id,draft_id,version,revision,policy_version,digest)
                 SELECT tenant_id,gen_random_uuid(),id,version,revision,policy_version,digest
                 FROM drafts WHERE tenant_id=%s AND thread_id=%s AND version=1 AND revision<%s
                 RETURNING tenant_id,id,revision
               )
               INSERT INTO deliveries (tenant_id,thread_id,delivery_key,body,mail_provider,provider_receipt_id)
               SELECT tenant_id,%s,id,'Delivered revision ' || revision,'fixture',id::text FROM approved""",
            (tenant, thread, turns, thread),
        )
        db.execute(
            """INSERT INTO jobs (id,tenant_id,thread_id,revision,kind,payload,state)
               SELECT job_id,tenant_id,thread_id,revision,'analyze',jsonb_build_object('event_id',event_id),'FAILED'
               FROM messages WHERE tenant_id=%s AND thread_id=%s AND revision=2""",
            (tenant, thread),
        )
        db.execute(
            "UPDATE negotiations SET revision=%s,status='AWAITING_APPROVAL' WHERE tenant_id=%s AND id=%s",
            (turns, tenant, thread),
        )


def workspace(store, client):
    session = client.post("/api/session", headers=HEADERS).json()
    thread = session["threads"][0]["id"]
    with store.connect() as db:
        tenant = db.execute("SELECT tenant_id FROM negotiations WHERE id=%s", (thread,)).fetchone()[
            "tenant_id"
        ]
    return tenant, thread, "/api/negotiations/" + thread


def test_paged_snapshot_bounds_history_and_preserves_legacy_contract(store, monkeypatch):
    client = TestClient(create_app(store))
    tenant, thread, route = workspace(store, client)
    seed_history(store, tenant, thread)
    counts = {"connections": 0, "queries": 0}
    original = store.connect

    class CountedConnection:
        def __init__(self, db):
            self.db = db

        def execute(self, *args, **kwargs):
            counts["queries"] += 1
            return self.db.execute(*args, **kwargs)

    @contextmanager
    def connect():
        counts["connections"] += 1
        with original() as db:
            yield CountedConnection(db)

    with monkeypatch.context() as patch:
        patch.setattr(store, "connect", connect)
        full = client.get(route)
        full_counts = counts.copy()
        counts.update(connections=0, queries=0)
        paged = client.get(route + "?history=paged")
        page_counts = counts.copy()

    assert full.status_code == paged.status_code == 200
    assert paged.headers["X-Dealroom-Schema"] == full.headers["X-Dealroom-Schema"] == "1"
    assert len(full.json()["messages"]) == 500
    assert len(full.json()["drafts"]) == 1500
    assert len(full.content) > 2_000_000
    assert len(paged.content) < 100_000
    assert page_counts["connections"] == full_counts["connections"] == 2
    assert page_counts["queries"] <= full_counts["queries"] + 2
    assert paged.headers["etag"] != full.headers["etag"]
    assert client.get(route, headers={"If-None-Match": paged.headers["etag"]}).status_code == 200
    assert (
        client.get(
            route + "?history=paged", headers={"If-None-Match": paged.headers["etag"]}
        ).status_code
        == 304
    )
    latest = paged.json()
    assert latest["messages"][-1]["revision"] == 500
    assert latest["drafts"][-1]["status"] == "PENDING"
    assert latest["recoverable_turns"][0]["revision"] == 2
    assert latest["history_cursor"]


def test_history_cursor_has_no_gaps_when_a_turn_arrives_and_scopes_reads(store):
    client = TestClient(create_app(store))
    tenant, thread, route = workspace(store, client)
    seed_history(store, tenant, thread, turns=40, versions=80)
    first = client.get(route + "?history=paged").json()
    cursor = first["history_cursor"]
    foreign = TestClient(create_app(store))
    workspace(store, foreign)
    assert foreign.get(route + "/history", params={"before": cursor}).status_code == 404
    assert client.get(route + "/history?before=bad").status_code == 422
    assert client.get(route + "/history?before=2147483649:1:" + str(uuid4())).status_code == 422

    assert (
        client.post(
            route + "/inbound", headers=HEADERS, json={"event_id": str(uuid4()), "body": "New offer"}
        ).status_code
        == 202
    )

    pages = [first]
    while cursor:
        response = client.get(route + "/history", params={"before": cursor})
        assert response.status_code == 200
        page = response.json()
        assert len(response.content) < 100_000
        pages.append(page)
        cursor = page["history_cursor"]
    messages = [m["revision"] for page in pages for m in page["messages"]]
    drafts = [(d["revision"], d["version"]) for page in pages for d in page["drafts"]]
    receipts = [receipt for page in pages for receipt in page["deliveries"]]
    assert sorted(messages) == list(range(1, 41))
    assert sorted(drafts) == [(r, v) for r in range(1, 41) for v in range(1, 81)]
    assert sorted(receipt["revision"] for receipt in receipts) == list(range(1, 40))
    assert all(
        receipt["provider_receipt_id"] == receipt["delivery_key"]
        and receipt["body"] == f"Delivered revision {receipt['revision']}"
        for receipt in receipts
    )


def test_creator_viewer_reads_history_but_cannot_write(store):
    from test_real_mail import ISSUER, NOW, SECRET, identity_token

    from app.identity import IdentityGateway, IdentityStore

    gateway = IdentityGateway(SECRET, ISSUER, clock=lambda: NOW)
    token = identity_token(sub=uuid4().hex)
    member = IdentityStore(store).provision_workspace(gateway.verify(token))
    thread = store.create_thread(member.tenant_id, "Read-only campaign")
    client = TestClient(create_app(store, identity_gateway=gateway))
    headers = HEADERS | {"Authorization": "Bearer " + token, "X-Workspace-Id": str(member.tenant_id)}
    assert client.post("/api/session", headers=headers).json()["capabilities"]["can_write"] is True
    with store.connect() as db:
        db.execute(
            "UPDATE workspace_memberships SET role='viewer' WHERE tenant_id=%s AND account_id=%s",
            (member.tenant_id, member.account_id),
        )
    session = client.post("/api/session", headers=headers).json()
    assert session["capabilities"]["can_write"] is False
    assert session["threads"][0]["id"] == str(thread)
    route = f"/api/negotiations/{thread}"
    assert client.get(route + "?history=paged", headers=headers).status_code == 200
    cursor = "1:2147483648:00000000-0000-0000-0000-000000000000"
    assert client.get(route + "/history", params={"before": cursor}, headers=headers).status_code == 200
    assert client.post("/api/negotiations", json={"label": "Denied"}, headers=headers).status_code == 403
    assert (
        client.post(
            route + "/transition", json={"revision": 0, "action": "close"}, headers=headers
        ).status_code
        == 403
    )


def test_snapshot_surfaces_receipt_for_an_already_loaded_older_draft(store):
    client = TestClient(create_app(store))
    tenant, thread, route = workspace(store, client)
    seed_history(store, tenant, thread, turns=20)
    with store.connect() as db:
        receipt = db.execute(
            """DELETE FROM deliveries WHERE tenant_id=%s AND thread_id=%s
               AND body='Delivered revision 1'
               RETURNING delivery_key,body,mail_provider,provider_receipt_id""",
            (tenant, thread),
        ).fetchone()
    snapshot = client.get(route + "?history=paged")
    cursor = snapshot.json()["history_cursor"]
    older = client.get(route + "/history", params={"before": cursor}).json()
    assert any(item["revision"] == 1 for item in older["drafts"])
    assert all(item["revision"] != 1 for item in older["deliveries"])

    with store.connect() as db:
        db.execute(
            """INSERT INTO deliveries (tenant_id,thread_id,delivery_key,body,mail_provider,provider_receipt_id)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (tenant, thread, *receipt.values()),
        )
        db.execute(
            """INSERT INTO timeline (tenant_id,thread_id,revision,kind,details)
               VALUES (%s,%s,1,'provider_delivery_accepted',jsonb_build_object('approval_id',%s::text))""",
            (tenant, thread, receipt["delivery_key"]),
        )
    updated = client.get(
        route + "?history=paged", headers={"If-None-Match": snapshot.headers["etag"]}
    ).json()
    assert all(item["revision"] != 1 for item in updated["deliveries"])
    assert [item["revision"] for item in updated["recent_receipts"]] == [1]
    assert updated["recent_receipts"][0]["provider_receipt_id"] == receipt["provider_receipt_id"]
    assert updated["recent_receipts"][0]["draft_id"] == next(
        item["id"] for item in older["drafts"] if item["revision"] == 1 and item["version"] == 1
    )


def test_terminal_delivery_failure_remains_readable_to_legacy_clients(store):
    client = TestClient(create_app(store))
    tenant, thread, route = workspace(store, client)
    seed_history(store, tenant, thread, turns=1, versions=1)
    with store.connect() as db:
        db.execute(
            "UPDATE drafts SET status='DELIVERY_FAILED' WHERE tenant_id=%s AND thread_id=%s",
            (tenant, thread),
        )

    assert client.get(route).json()["drafts"][0]["status"] == "REJECTED"
    assert client.get(route + "?history=paged").json()["drafts"][0]["status"] == "DELIVERY_FAILED"
