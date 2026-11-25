from app.domain import Offer, Policy, decide, merge


def test_reference_counter_and_confidential_floor():
    offer = Offer(
        brand="Northstar",
        deliverables={"reel": 1},
        amount=120000,
        currency="USD",
        exclusive=True,
        advertising=True,
    )
    result = decide(offer, Policy())
    assert result.action == "counteroffer"
    assert (result.base, result.surcharge, result.total) == (200000, 60000, 260000)
    assert {"usage scope", "usage duration"} <= set(result.questions)
    assert 150000 not in result.model_dump().values()


def test_omission_withdrawal_and_currency():
    prior = Offer(deliverables={"reel": 1}, amount=120000, currency="USD", exclusive=True)
    next_offer = merge(prior, Offer(amount=210000, exclusive=False))
    assert next_offer.currency == "USD" and next_offer.exclusive is False
    assert next_offer.deliverables == {"reel": 1}
    next_offer = merge(next_offer, Offer(currency=None))
    assert decide(next_offer, Policy()).action == "clarification"


def test_exclusivity_rounding_and_multiple_deliverables():
    policy = Policy(rates={"reel": 200005, "story": 50000})
    terms = Offer(
        currency="USD",
        deliverables={"reel": 1, "story": 2},
        exclusive=True,
        exclusive_days=30,
        amount=120000,
    )
    decision = decide(terms, policy)
    assert decision.items == {"reel": 200005, "story": 100000}
    assert decision.surcharge == 90002


def test_accepted_inclusive_offer_uses_negotiated_base():
    terms = Offer(
        currency="USD", deliverables={"reel": 1}, exclusive=True, exclusive_days=30, amount=390000
    )
    result = decide(terms, Policy())
    assert result.action == "acceptance"
    assert (result.items, result.base, result.surcharge, result.total) == (
        {"reel": 300000},
        300000,
        90000,
        390000,
    )


def test_changed_item_keeps_other_deliverables_and_null_clears_exclusivity():
    prior = Offer(deliverables={"reel": 1, "story": 1}, exclusive=True, exclusive_days=180)
    terms = merge(prior, Offer(deliverables={"story": 2}, exclusive=None))
    assert terms.deliverables == {"reel": 1, "story": 2}
    assert terms.exclusive_days is None
