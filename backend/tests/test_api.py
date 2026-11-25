from uuid import uuid4

from fastapi.testclient import TestClient

from app.api import create_app
from app.worker import Worker

HEADERS = {"X-Sandbox-Request": "1"}


def test_session_isolation_and_guarded_mutations(store):
    app = create_app(store)
    creator = TestClient(app)
    second = TestClient(app)
    assert creator.post("/api/session").status_code == 403
    assert (
        creator.post(
            "/api/session", headers={"X-Sandbox-Request": "1", "Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    first = creator.post("/api/session", headers=HEADERS).json()
    thread = first["threads"][0]["id"]
    assert second.get(f"/api/negotiations/{thread}").status_code == 401
    second.post("/api/session", headers=HEADERS)
    assert second.get(f"/api/negotiations/{thread}").status_code == 404
    assert second.get("/api/profile").json()["data"]["minimum_reel"] == 150000
    response = creator.post(
        f"/api/negotiations/{thread}/inbound",
        headers=HEADERS,
        json={"event_id": str(uuid4()), "body": "USD 1200 for one reel"},
    )
    assert response.status_code == 202
    Worker(store).step(response.json()["job_id"])
    draft = creator.get(f"/api/negotiations/{thread}").json()["drafts"][0]
    assert "1,500" not in draft["body"]
    assert (
        second.post(
            f"/api/negotiations/{thread}/drafts/{draft['id']}",
            headers=HEADERS,
            json={"action": "approve", "version": 1},
        ).status_code
        == 404
    )


def test_evaluation_run_belongs_to_browser_session(store):
    first, second = TestClient(create_app(store)), TestClient(create_app(store))
    first.post("/api/session", headers=HEADERS)
    second.post("/api/session", headers=HEADERS)
    response = first.post("/api/evaluations", headers=HEADERS)
    assert response.status_code == 202
    run = response.json()["id"]
    assert second.get(f"/api/evaluations/{run}").status_code == 404
    assert first.get(f"/api/evaluations/{run}").json()["status"] == "QUEUED"
    assert first.post("/api/evaluations", headers=HEADERS).status_code == 429
