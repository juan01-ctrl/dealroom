from app.evaluations import load_fixtures, run_suite
from app.models import ScriptedModel


def test_reference_suite_reports_twenty_real_results(store):
    result = run_suite(store, ScriptedModel(), load_fixtures())
    assert len(result["scenarios"]) == 28
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


def test_evaluation_surfaces_failure_instead_of_prefilled_success(store):
    fixture = load_fixtures()[0] | {"expected": {"action": "acceptance"}}
    result = run_suite(store, ScriptedModel(), [fixture])
    scenario = result["scenarios"][0]
    assert not scenario["passed"]
    assert scenario["expected"]["action"] == "acceptance"
    assert scenario["actual"]["action"] == "counteroffer"
    assert scenario["failures"]
