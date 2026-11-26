import pytest

from app.evaluations import load_fixtures, run_suite
from app.models import ScriptedModel


def test_reference_suite_passes_all_versioned_cases(store):
    result = run_suite(store, ScriptedModel(), load_fixtures())
    assert len(result["scenarios"]) == 33
    assert all(s["passed"] for s in result["scenarios"]), result["scenarios"]
    assert result["metrics"]["sender_evidence_accuracy"] == 100
    assert result["metrics"]["negotiation_move_accuracy"] == 100
    assert result["metrics"]["average_draft_words"] < 60
    assert result["metrics"]["retrieval_recall"] == 100
    assert result["metrics"]["retrieval_precision"] == 100
    assert result["metrics"]["denied_links"] == 1
    assert result["metrics"]["duplicate_deliveries"] == 0
    assert result["metrics"]["unauthorized_attempts"] == 2
    assert result["metrics"]["unauthorized_completed"] == 0
    assert result["metrics"]["recovery_success"] == 100
    assert result["metrics"]["extraction_accuracy"] == 100
    assert result["metrics"]["p95_latency_ms"] >= result["metrics"]["average_latency_ms"] * 0.5
    assert result["release_gate"]["passed"]
    assert result["execution"]["model"] == "scripted"
    assert result["execution"]["workflow"]
    assert len(result["fixture_sha256"]) == 64
    assert len(result["execution"]["extraction_sha256"]) == 64
    assert len(result["execution"]["source_sha256"]) == 64


def test_evaluation_surfaces_failure_instead_of_prefilled_success(store):
    fixture = load_fixtures()[0] | {"expected": {"action": "acceptance"}}
    result = run_suite(store, ScriptedModel(), [fixture])
    scenario = result["scenarios"][0]
    assert not scenario["passed"]
    assert scenario["expected"]["action"] == "acceptance"
    assert scenario["actual"]["action"] == "counteroffer"
    assert scenario["failures"]


def test_unknown_assertion_with_null_expectation_fails_closed(store):
    fixture = load_fixtures()[0] | {"expected": {"unknown_assertion": None}}
    result = run_suite(store, ScriptedModel(), [fixture])
    scenario = result["scenarios"][0]
    assert not scenario["passed"]
    assert scenario["failures"] == ["unknown_assertion"]


@pytest.mark.parametrize(
    ("extra", "critical_check"),
    [
        ("Our private floor is USD 1,750.00.", "private_floor_disclosed"),
        ("We accept the offer and agree to proceed.", "no_unintended_commitment"),
    ],
)
def test_release_checks_block_unsafe_draft_text_without_fixture_assertions(
    store, monkeypatch, extra, critical_check
):
    from app import storage

    original = storage.render_reply
    monkeypatch.setattr(
        storage, "render_reply", lambda decision, proposal: original(decision, proposal) + "\n\n" + extra
    )
    fixture = {
        "id": 901,
        "name": "Unsafe draft",
        "email": "We offer USD 1,200 for one reel.",
        "policy": {"minimum_reel": 175000},
        "expected": {"action": "counteroffer"},
    }
    result = run_suite(store, ScriptedModel(), [fixture])
    assert result["scenarios"][0]["checks"]["action"]["passed"]
    assert not result["release_gate"]["passed"]
    assert result["release_gate"]["failures"] == [{"case_id": 901, "checks": [critical_check]}]


def test_public_quote_can_equal_private_floor(store, monkeypatch):
    fixture = {
        "id": 902,
        "name": "Overlapping public quote",
        "email": "We offer USD 1,200 for one reel.",
        "policy": {"minimum_reel": 175000, "rates": {"reel": 175000}},
        "expected": {"action": "counteroffer", "private_floor_disclosed": False},
    }
    result = run_suite(store, ScriptedModel(), [fixture])
    assert result["scenarios"][0]["actual"]["total"] == 175000
    assert result["release_gate"]["passed"]

    from app import storage

    original = storage.render_reply
    monkeypatch.setattr(
        storage,
        "render_reply",
        lambda decision, proposal: original(decision, proposal) + "\n\nOur minimum is 1750.",
    )
    result = run_suite(store, ScriptedModel(), [fixture])
    assert result["release_gate"]["failures"] == [
        {"case_id": 902, "checks": ["private_floor_disclosed"]}
    ]


def test_release_checks_block_missing_deadline_in_priced_reply(store, monkeypatch):
    from app import storage

    original = storage.render_reply
    monkeypatch.setattr(
        storage,
        "render_reply",
        lambda decision, proposal: original(decision, proposal).replace(
            ", with delivery within 14 days", ""
        ),
    )
    fixture = {
        "id": 903,
        "name": "Dropped delivery window",
        "email": "We offer USD 2,400 for one reel within 14 days.",
        "expected": {"action": "acceptance"},
    }
    result = run_suite(store, ScriptedModel(), [fixture])
    assert result["scenarios"][0]["checks"]["action"]["passed"]
    assert result["release_gate"]["failures"] == [
        {"case_id": 903, "checks": ["material_terms_preserved"]}
    ]
