from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_real_mail import ISSUER, NOW, SECRET, identity_token
from test_retrieval import LINK

from app.api import create_app
from app.campaign_briefs import BriefRegistration, CampaignBriefs
from app.identity import IdentityClaims, IdentityGateway, IdentityStore
from app.models import ScriptedModel
from app.storage import Conflict, NotFound
from app.worker import Worker

URL = "https://campaigns.example.test/summer"


def registration(revision=1, days=30, **changes):
    return BriefRegistration.model_validate(
        {
            "document_id": "summer",
            "source_url": URL,
            "revision": revision,
            "title": "Summer partnership",
            "sections": [
                {"id": "creator-exclusivity", "exclusive_days": days},
                {"id": "digital-rights", "usage_days": 45, "usage_scope": "paid social"},
            ],
        }
        | changes
    )


def workspace(store, subject="creator-44", email="creator@example.test"):
    member = IdentityStore(store).provision_workspace(IdentityClaims(ISSUER, subject, email))
    return member.tenant_id, store.create_thread(member.tenant_id, "Summer campaign")


def analyze(store, tenant, thread, link=URL, *, offer_text="USD 3,000 for one reel."):
    email = f"From: Jordan <jordan@example.test>\nSubject: Campaign\n\n{offer_text}\n{link}"
    receipt = store.ingest(tenant, thread, str(uuid4()), email)
    assert Worker(store, ScriptedModel(), mail_provider=object()).step(receipt["job_id"])
    snapshot = store.snapshot(tenant, thread)
    evidence = next(
        event["details"]["retrieval"]
        for event in reversed(snapshot["timeline"])
        if event["kind"] == "analysis_completed"
    )
    offer = next(
        event["details"]["terms"]
        for event in reversed(snapshot["timeline"])
        if event["kind"] == "offer_extracted"
    )
    return evidence, offer, snapshot["drafts"][-1]["decision"]


def test_registered_brief_is_read_only_by_its_thread_tenant(store):
    briefs = CampaignBriefs(store)
    tenant, thread = workspace(store)
    other, other_thread = workspace(store)
    demo, demo_thread = store.create_session()
    briefs.register(tenant, registration())
    with pytest.raises(NotFound):
        briefs.inspect(tenant, other_thread, "summer", 1)
    with pytest.raises(NotFound):
        briefs.register(demo, registration())
    assert briefs.inspect(tenant, thread, "summer", 1)["sections"][0]["exclusive_days"] == 30

    evidence, offer, _ = analyze(store, tenant, thread)
    assert offer["exclusive_days"] == 30 and offer["usage_days"] == 45
    source = evidence["sources"][0]
    assert source["kind"] == "registered"
    assert source["revision"] == "1"
    assert source["section_ids"] == ["creator-exclusivity", "digital-rights"]
    assert evidence["field_sources"]["exclusive_days"] == [
        {"document_id": "summer", "revision": "1", "section_id": "creator-exclusivity"}
    ]
    overridden, offer, _ = analyze(
        store, tenant, thread, offer_text="USD 3,000 for one reel with 20 days of exclusivity."
    )
    assert offer["exclusive_days"] == 20
    assert "exclusive_days" not in overridden["field_sources"]

    for scope in ((other, other_thread, URL), (demo, demo_thread, URL), (other, other_thread, LINK)):
        evidence, offer, decision = analyze(store, *scope)
        assert evidence["denied_links"] == 1
        assert evidence["sources"] == []
        assert offer["exclusive_days"] is None
        assert decision["action"] == "clarification"
        assert "a shareable campaign brief" in decision["questions"]


def test_registered_revisions_are_immutable_and_revocation_blocks_new_reads(store):
    briefs = CampaignBriefs(store)
    tenant, thread = workspace(store)
    first = registration()
    second = registration(2, 60)
    briefs.register(tenant, first)
    briefs.register(tenant, second)
    briefs.register(tenant, first)
    with pytest.raises(Conflict):
        briefs.register(tenant, registration(2, 75))
    with pytest.raises(Conflict):
        briefs.register(tenant, registration(4))
    assert briefs.inspect(tenant, thread, "summer", 1)["sections"][0]["exclusive_days"] == 30
    assert analyze(store, tenant, thread)[1]["exclusive_days"] == 60

    briefs.revoke(tenant, "summer")
    briefs.revoke(tenant, "summer")
    with pytest.raises(NotFound):
        briefs.inspect(tenant, thread, "summer", 1)
    with pytest.raises(Conflict):
        briefs.register(tenant, registration(3))
    fresh = store.create_thread(tenant, "Separate campaign")
    evidence, offer, decision = analyze(store, tenant, fresh)
    assert evidence["denied_links"] == 1
    assert not evidence["sources"]
    assert offer["exclusive_days"] is None
    assert decision["action"] == "clarification"


def test_conflicting_sections_are_cited_and_the_term_needs_confirmation(store):
    tenant, thread = workspace(store)
    CampaignBriefs(store).register(
        tenant,
        registration(
            sections=[
                {"id": "schedule", "exclusive_days": 30},
                {"id": "rights", "exclusive_days": 60},
            ]
        ),
    )
    evidence, offer, decision = analyze(store, tenant, thread)
    assert evidence["sources"][0]["section_ids"] == ["schedule", "rights"]
    assert evidence["conflicting_fields"] == ["exclusive_days"]
    assert "exclusive_days" not in evidence["field_sources"]
    assert offer["exclusive_days"] is None
    assert decision["action"] == "clarification"
    assert "the conflicting brief terms" in decision["questions"]


@pytest.mark.parametrize(
    "changes",
    [
        {"source_url": "http://campaigns.example.test/summer"},
        {"source_url": "https://api-key@campaigns.example.test/summer"},
        {"source_url": URL + "?token=secret"},
        {"sections": [{"id": "unknown", "body": "An instruction pretending to be evidence"}]},
        {"sections": [{"id": "rights", "usage_days": 0}]},
        {"sections": [{"id": "rights", "usage_days": 30}] * 17},
        {"sections": [{"id": "rights", "usage_days": 30}] * 2},
    ],
)
def test_registration_validates_bounds_and_rejects_freeform_sections(changes):
    with pytest.raises(ValidationError):
        registration(**changes)


def test_brief_inspection_requires_verified_membership_and_thread_scope(store):
    tenant, thread = workspace(store)
    other, other_thread = workspace(store, "other-55", "other@example.test")
    briefs = CampaignBriefs(store)
    briefs.register(tenant, registration())
    briefs.register(tenant, registration(2, 60))
    gateway = IdentityGateway(SECRET, ISSUER, clock=lambda: NOW)
    client = TestClient(create_app(store, identity_gateway=gateway))
    headers = {"Authorization": f"Bearer {identity_token()}", "X-Workspace-Id": str(tenant)}
    url = f"/api/negotiations/{thread}/briefs/summer?revision=1"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={**headers, "X-Workspace-Id": str(other)}).status_code == 401
    assert client.get(url.replace(str(thread), str(other_thread)), headers=headers).status_code == 404
    inspected = client.get(url, headers=headers)
    assert inspected.status_code == 200
    assert inspected.headers["cache-control"] == "no-store"
    assert inspected.json()["sections"][0]["exclusive_days"] == 30
    briefs.revoke(tenant, "summer")
    assert client.get(url, headers=headers).status_code == 404
