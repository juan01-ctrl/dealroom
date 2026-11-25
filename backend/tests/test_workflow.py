from uuid import uuid4

from app.domain import Policy, Proposal
from app.worker import Worker

EMAIL = (
    "Northstar offers USD 1,200 for one reel, including exclusivity and paid advertising usage rights."
)


def test_reference_offer_requires_exact_approval(store):
    tenant, thread = store.create_session()
    worker = Worker(store)
    receipt = store.ingest(tenant, thread, str(uuid4()), EMAIL)
    worker.step(receipt["job_id"])
    state = store.snapshot(tenant, thread)
    draft = state["drafts"][0]
    assert state["status"] == "AWAITING_APPROVAL" and not state["deliveries"]
    assert draft["decision"]["total"] == 260000
    assert (
        "1,500" not in draft["body"]
        and "Where will the paid ads run, and for how long?" in draft["body"]
    )
    approval = store.review(tenant, thread, draft["id"], 1, "approve")
    worker.step(approval)
    state = store.snapshot(tenant, thread)
    assert state["status"] == "AWAITING_BRAND_REPLY"
    assert [row["body"] for row in state["deliveries"]] == [draft["body"]]


def test_new_version_and_inbound_block_old_approvals(store):
    tenant, thread = store.create_session()
    worker = Worker(store)
    receipt = store.ingest(tenant, thread, str(uuid4()), EMAIL)
    worker.step(receipt["job_id"])
    draft = store.snapshot(tenant, thread)["drafts"][0]
    approval = store.review(tenant, thread, draft["id"], 1, "approve")
    edited = store.review(tenant, thread, draft["id"], 1, "edit", Proposal(tone="formal"))
    worker.step(approval)
    assert not store.snapshot(tenant, thread)["deliveries"]
    approval = store.review(tenant, thread, edited, 2, "approve")
    followup = store.ingest(tenant, thread, str(uuid4()), "We can offer USD 2,400.")
    worker.step(approval)
    worker.step(followup["job_id"])
    after = store.snapshot(tenant, thread)
    assert not after["deliveries"]
    assert after["drafts"][-1]["decision"]["surcharge"] == 60000


def test_policy_update_blocks_approved_delivery(store):
    tenant, thread = store.create_session()
    worker = Worker(store)
    receipt = store.ingest(tenant, thread, str(uuid4()), EMAIL)
    worker.step(receipt["job_id"])
    draft = store.snapshot(tenant, thread)["drafts"][0]
    approval = store.review(tenant, thread, draft["id"], 1, "approve")
    store.update_policy(tenant, 1, Policy(exclusivity_pct=40))
    worker.step(approval)
    assert not store.snapshot(tenant, thread)["deliveries"]


def test_delivered_tone_edit_is_suggested_without_changing_policy(store):
    tenant, thread = store.create_session()
    worker = Worker(store)
    receipt = store.ingest(tenant, thread, str(uuid4()), EMAIL)
    worker.step(receipt["job_id"])
    draft = store.snapshot(tenant, thread)["drafts"][0]
    edited = store.review(tenant, thread, draft["id"], 1, "edit", Proposal(tone="formal"))
    approval = store.review(tenant, thread, edited, 2, "approve")
    worker.step(approval)
    profile = store.policy(tenant)
    assert profile["suggested_tone"] == "formal"
    assert profile["version"] == 1 and profile["data"]["tone"] == "warm"
    store.update_policy(tenant, 1, Policy(tone="formal"))
    assert store.policy(tenant)["suggested_tone"] is None


def test_simultaneous_approvals_share_one_delivery_key(store):
    from concurrent.futures import ThreadPoolExecutor

    tenant, thread = store.create_session()
    worker = Worker(store)
    receipt = store.ingest(tenant, thread, str(uuid4()), EMAIL)
    worker.step(receipt["job_id"])
    draft = store.snapshot(tenant, thread)["drafts"][0]

    def approve(_):
        return store.review(tenant, thread, draft["id"], 1, "approve")

    with ThreadPoolExecutor(max_workers=2) as pool:
        approvals = list(pool.map(approve, range(2)))
    assert approvals[0] == approvals[1]
    worker.step(approvals[0])
    assert len(store.snapshot(tenant, thread)["deliveries"]) == 1
