import pytest

from app.domain import Offer, Policy, Proposal, decide, merge
from app.models import ScriptedModel
from app.negotiation import plan_reply, render_reply
from app.sender import identify_sender


@pytest.mark.parametrize(
    ("header", "body", "name", "evidence"),
    [
        ("Jordan Lee <jordan.lee@northstar.example>", "Hi Maya,\n\nBest,\nJordan", "Jordan", "mailbox"),
        (
            "Jordan Lee <partnerships@northstar.example>",
            "Hi Maya,\n\nThanks, Jordan Lee",
            "Jordan",
            "signature",
        ),
        ("jordan.lee@northstar.example", "Best,\nJordan Lee", "Jordan", "signature"),
        ("Maya Rivers <brand@northstar.example>", "Hi Maya,\n\n> Best,\n> Maya", None, "unverified"),
        ("Jordan Lee <jordan@northstar.example>", "Hi Maya,\n\nThanks, Morgan", None, "ambiguous"),
        (
            "Jordan Lee <jordan@northstar.example>\nFrom: Morgan <morgan@northstar.example>",
            "Best,\nJordan",
            None,
            "ambiguous",
        ),
    ],
)
def test_sender_greeting_needs_matching_evidence(header, body, name, evidence):
    actual = identify_sender(f"From: {header}\nSubject: Offer\n\n{body}")
    assert (actual.first_name, actual.evidence) == (name, evidence)


def test_reference_reply_has_an_anchor_and_one_targeted_question():
    offer = Offer(
        deliverables={"reel": 1},
        currency="USD",
        amount=120000,
        exclusive=True,
        exclusive_days=30,
        advertising=True,
        usage_scope="paid social",
        usage_days=45,
    )
    plan = plan_reply(
        decide(offer, Policy()),
        offer,
        identify_sender("From: Jordan Lee <jordan@northstar.example>\n\nBest,\nJordan"),
    )
    reply = render_reply(plan, Proposal())
    assert reply.startswith("Hi Jordan,")
    assert "one reel" in reply and "30 days of exclusivity" in reply
    assert "USD 2,600.00" in reply and "45-day paid social" in reply
    assert "1,500" not in reply and reply.count("?") == 1
    assert len(reply.split()) <= 90


def test_separate_brand_budget_is_preserved_and_anchor_uses_only_sent_quote():
    offer = Offer(
        deliverables={"reel": 1},
        currency="USD",
        amount=280000,
        advertising=True,
        usage_scope="paid social",
        usage_days=30,
        usage_included=False,
    )
    sender = identify_sender("A bare message.")
    prior = plan_reply(decide(offer, Policy()), offer, sender)
    assert prior.total == 280000
    revised = offer.model_copy(update={"amount": 230000})
    next_reply = plan_reply(decide(revised, Policy()), revised, sender, prior=(prior, offer))
    assert next_reply.move == "hold_quote"
    assert "USD 2,800.00" in render_reply(next_reply, Proposal())
    changed = revised.model_copy(update={"usage_days": 10})
    next_reply = plan_reply(decide(changed, Policy()), changed, sender, prior=(prior, offer))
    assert next_reply.move == "reprice_scope"
    assert "USD 2,300.00" in render_reply(next_reply, Proposal())


def test_optional_followup_does_not_remove_blocking_question_or_add_filler():
    offer = Offer(deliverables={"reel": 1}, amount=120000)
    decision = plan_reply(decide(offer, Policy()), offer, identify_sender("Hello"))
    reply = render_reply(decision, Proposal(invite_followup=False))
    assert "Which currency is the budget in?" in reply
    assert "?" in reply and "Happy to discuss" not in reply


def test_known_foreign_currency_requests_a_budget_in_the_quote_currency():
    offer = Offer(deliverables={"reel": 1}, amount=240000, currency="EUR")
    decision = plan_reply(decide(offer, Policy()), offer, identify_sender("Hello"))
    reply = render_reply(decision, Proposal())
    assert decision.action == "clarification"
    assert decision.questions == ["budget in quote currency"]
    assert decision.total is None
    assert "I quote in USD. Could you share the budget in USD?" in reply
    assert "2,400" not in reply


def test_conditional_scope_trade_avoids_unilateral_discount():
    offer = Offer(
        deliverables={"reel": 1},
        currency="USD",
        amount=120000,
        exclusive=True,
        exclusive_days=30,
    )
    decision = plan_reply(decide(offer, Policy()), offer, identify_sender("Hello"))
    reply = render_reply(decision, Proposal())
    assert "USD 2,600.00" in reply
    assert "If the budget is fixed, would a package without exclusivity work?" in reply
    assert reply.count("USD") == 1


def test_zero_fee_exclusivity_and_delivery_window_are_confirmed():
    patch, _ = ScriptedModel().extract(
        "USD 2,400 for one reel within 14 days with 30 days of category exclusivity.", {}
    )
    offer = merge(Offer(), patch)
    decision = plan_reply(decide(offer, Policy(exclusivity_pct=0)), offer, identify_sender(""))
    draft = render_reply(decision, Proposal())

    assert decision.action == "acceptance"
    assert decision.surcharge == 0
    assert "USD 2,400.00" in draft
    assert "30 days of exclusivity" in draft
    assert "within 14 days" in draft


def test_unmapped_bundle_obligations_block_a_quote_until_explicitly_withdrawn():
    model = ScriptedModel()
    patch, _ = model.extract("USD 2,400 for one reel, plus whitelisting and raw footage.", {})
    offer = merge(Offer(), patch)
    higher_budget, _ = model.extract("We can go up to USD 3,000.", offer.model_dump())
    offer = merge(offer, higher_budget)
    decision = plan_reply(decide(offer, Policy()), offer, identify_sender(""))
    draft = render_reply(decision, Proposal(invite_followup=False))

    assert offer.deliverables == {"reel": 1}
    assert {item.kind for item in offer.unmapped_obligations} == {"whitelisting", "raw_assets"}
    assert decision.action == "clarification"
    assert decision.total is None and "USD" not in draft
    assert "whitelisting" in draft and "raw assets" in draft

    withdrawal, _ = model.extract("No whitelisting or raw footage is needed.", offer.model_dump())
    offer = merge(offer, withdrawal)
    assert not offer.unmapped_obligations
    assert decide(offer, Policy()).action == "acceptance"


def test_withdrawing_one_contract_term_keeps_other_terms_under_review():
    model = ScriptedModel()
    patch, _ = model.extract("USD 2,400 for one reel, with indemnification and copyright transfer.", {})
    offer = merge(Offer(), patch)
    followup, _ = model.extract("No copyright transfer.", offer.model_dump())
    offer = merge(offer, followup)

    assert [item.detail for item in offer.unmapped_obligations] == ["indemnification"]
    assert decide(offer, Policy()).action == "clarification"


def test_an_unsent_quote_does_not_anchor_the_next_turn(store):
    from uuid import uuid4

    from app.worker import Worker

    tenant, thread = store.create_session()
    worker = Worker(store)
    first = store.ingest(
        tenant,
        thread,
        str(uuid4()),
        "USD 2,800 for one reel. We need 30 days of paid social ads. Paid usage is outside that budget.",
    )
    worker.step(first["job_id"])
    assert store.snapshot(tenant, thread)["drafts"][-1]["decision"]["total"] == 280000
    second = store.ingest(tenant, thread, str(uuid4()), "Can you work with USD 2,300?")
    worker.step(second["job_id"])
    draft = store.snapshot(tenant, thread)["drafts"][-1]
    assert draft["decision"]["move"] == "open_quote"
    assert draft["decision"]["total"] == 230000
