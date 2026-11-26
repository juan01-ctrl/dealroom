import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

DATASET = Path(__file__).resolve().parents[1] / "evals/fixtures/held-out-v2.json"


def make_review(dataset_path=DATASET):
    dataset = dataset_path.read_bytes()
    return {
        "rubric_version": "negotiation-review-v1",
        "dataset_sha256": hashlib.sha256(dataset).hexdigest(),
        "candidate": {
            "revision": "candidate-1",
            "workflow": "workflow-1",
            "prompt": "prompt-1",
            "retrieval": "retrieval-1",
            "model": "provider/model-1",
        },
        "blind_review_completed": True,
        "turns": [
            {
                "case_id": turn["case_id"],
                "turn": turn["turn"],
                "usability": "as_written",
                "tone": "appropriate",
                "checks": dict.fromkeys(
                    (
                        "amounts",
                        "terms",
                        "scope",
                        "approval",
                        "floor_privacy",
                        "commitment",
                        "provenance",
                    ),
                    True,
                ),
                "first_failure": None,
            }
            for turn in json.loads(dataset)["turns"]
        ],
    }


def evaluate(tmp_path, review, baseline=None, dataset=DATASET):
    review_file = tmp_path / "review.json"
    review_file.write_text(json.dumps(review))
    args = [sys.executable, "-m", "app.human_evaluation", str(dataset), str(review_file)]
    if baseline is not None:
        baseline_file = tmp_path / "baseline.json"
        baseline_file.write_text(json.dumps(baseline))
        args.extend(["--baseline", str(baseline_file)])
    return subprocess.run(args, capture_output=True, text=True, check=False)


def test_held_out_manifest_tracks_the_supplied_messages_and_expected_outcomes():
    source = DATASET.parents[3] / "docs/live-negotiation-review.md"
    marker = b"## Messages\n"
    outcomes_marker = b"\n## Expected outcomes\n"
    messages, outcomes = source.read_bytes().split(marker, 1)[1].split(outcomes_marker, 1)
    contents = marker + messages + outcomes_marker + outcomes.split(b"\n## ", 1)[0]
    assert json.loads(DATASET.read_bytes())["source_sha256"] == hashlib.sha256(contents).hexdigest()


def test_full_held_out_review_reports_sample_sizes_and_small_slices(tmp_path):
    run = evaluate(tmp_path, make_review())
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout)
    assert report["overall"]["cases"] == 10
    assert report["overall"]["turns"] == 13
    assert report["overall"]["usable_as_written"] == 13
    assert report["overall"]["status"] == "pass"
    assert report["slices"]["locale"]["es"]["turns"] == 1
    assert report["slices"]["locale"]["es"]["status"] == "insufficient_evidence"


def test_critical_failure_blocks_even_in_a_one_turn_slice(tmp_path):
    review = make_review()
    spanish = next(row for row in review["turns"] if row["case_id"] == "H02")
    spanish.update(usability="material_edit", first_failure="terms")
    spanish["checks"]["terms"] = False
    run = evaluate(tmp_path, review)
    assert run.returncode == 1, run.stderr
    report = json.loads(run.stdout)
    assert report["overall"]["critical_turns"] == 1
    assert report["slices"]["locale"]["es"]["status"] == "fail"
    assert report["first_failures"] == {"terms": 1}


def test_usable_gate_uses_the_full_turn_denominator(tmp_path):
    review = make_review()
    review["turns"][0].update(usability="minor_edit", first_failure="usability")
    assert evaluate(tmp_path, review).returncode == 0
    review["turns"][1].update(usability="minor_edit", first_failure="usability")
    run = evaluate(tmp_path, review)
    assert run.returncode == 1, run.stderr
    assert json.loads(run.stdout)["overall"]["usable_as_written"] == 11


def test_a_sufficient_slice_can_fail_even_when_the_overall_rate_passes(tmp_path):
    manifest = json.loads(DATASET.read_bytes())
    manifest["dataset_version"] = "pilot-v1"
    manifest["kind"] = "pilot"
    manifest["turns"] = [
        {
            "case_id": f"P{index}",
            "turn": 1,
            "locale": "en" if index < 10 else "es",
            "rights_ambiguity": "clear",
            "brief_availability": "none",
        }
        for index in range(20)
    ]
    dataset = tmp_path / "pilot.json"
    dataset.write_text(json.dumps(manifest))
    review = make_review(dataset)
    for row in review["turns"][:2]:
        row.update(usability="missing_draft", tone="no_draft", first_failure="generation")
    run = evaluate(tmp_path, review, dataset=dataset)
    assert run.returncode == 1
    report = json.loads(run.stdout)
    assert report["overall"]["status"] == "pass"
    assert report["slices"]["locale"]["en"]["status"] == "fail"
    assert report["slices"]["locale"]["es"]["status"] == "pass"


@pytest.mark.parametrize(
    "invalid", ["missing", "duplicate", "digest", "payload", "blind", "blind_integer", "first_failure"]
)
def test_incomplete_or_invalid_review_fails_validation(tmp_path, invalid):
    review = make_review()
    if invalid == "missing":
        review["turns"].pop()
    elif invalid == "duplicate":
        review["turns"].append(review["turns"][0])
    elif invalid == "digest":
        review["dataset_sha256"] = "b" * 64
    elif invalid == "payload":
        review["turns"][0]["body"] = "commercial text"
    elif invalid == "blind":
        review["blind_review_completed"] = False
    elif invalid == "blind_integer":
        review["blind_review_completed"] = 1
    else:
        review["turns"][0]["checks"]["floor_privacy"] = False
    run = evaluate(tmp_path, review)
    assert run.returncode == 2, run.stderr
    assert run.stdout == ""
    assert "commercial text" not in run.stderr


def test_comparison_flags_regressions_even_in_an_insufficient_slice(tmp_path):
    baseline = make_review()
    candidate = make_review()
    candidate["candidate"]["prompt"] = "prompt-2"
    spanish = next(row for row in candidate["turns"] if row["case_id"] == "H02")
    spanish.update(usability="minor_edit", tone="needs_edit", first_failure="tone")
    run = evaluate(tmp_path, candidate, baseline)
    assert run.returncode == 1
    report = json.loads(run.stdout)
    assert report["overall"]["status"] == "pass"
    assert report["comparison"]["status"] == "review_required"
    assert report["comparison"]["slices"]["locale"]["es"] == {
        "usability_worse": 1,
        "usability_better": 0,
        "tone_lost": 1,
        "tone_gained": 0,
    }


def test_comparison_flags_a_minor_edit_becoming_material_even_if_usable_rate_matches(tmp_path):
    baseline = make_review()
    candidate = make_review()
    baseline["turns"][0].update(usability="minor_edit", first_failure="usability")
    candidate["turns"][0].update(usability="material_edit", first_failure="usability")
    run = evaluate(tmp_path, candidate, baseline)
    assert run.returncode == 1
    assert json.loads(run.stdout)["comparison"]["overall"]["usability_worse"] == 1


def test_pilot_metrics_report_coverage_and_use_paired_cost_samples(tmp_path):
    review = make_review()
    review["turns"][0]["pilot"] = {
        "disposition": "sent_as_written",
        "reviewable_ms": 10,
        "estimated_cost_usd_micros": 120,
    }
    review["turns"][1]["pilot"] = {
        "disposition": "discarded",
        "reviewable_ms": 30,
        "estimated_cost_usd_micros": 40,
    }
    review["turns"][2]["pilot"] = {"disposition": "sent_as_written"}
    run = evaluate(tmp_path, review)
    assert run.returncode == 0, run.stderr
    metrics = json.loads(run.stdout)["pilot_metrics"]
    assert metrics["dispositions"] == {"sent_as_written": 2, "discarded": 1}
    assert metrics["disposition_turns"] == 3
    assert metrics["reviewable_turns"] == 2
    assert metrics["median_reviewable_ms"] == 20
    assert metrics["cost_turns"] == 2
    assert metrics["cost_sample_usable_as_written"] == 2
    assert metrics["estimated_cost_per_usable_draft_usd_micros"] == 80
    assert metrics["cost_sample_sent_as_written"] == 1
    assert metrics["estimated_cost_per_sent_as_written_usd_micros"] == 160


def test_duplicate_check_field_cannot_override_a_failure(tmp_path):
    review_file = tmp_path / "review.json"
    review_file.write_text(
        json.dumps(make_review()).replace('"approval": true', '"approval": false, "approval": true', 1)
    )
    run = subprocess.run(
        [sys.executable, "-m", "app.human_evaluation", str(DATASET), str(review_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 2
    assert run.stdout == ""
