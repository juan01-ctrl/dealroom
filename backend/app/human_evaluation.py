"""Validate and summarize a completed blind negotiation review offline."""

import argparse
import json
import sys
from collections import Counter
from hashlib import sha256
from pathlib import Path
from statistics import median
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MIN_CASES = 10
MIN_USABLE_PERCENT = 90
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Check = Literal["amounts", "terms", "scope", "approval", "floor_privacy", "commitment", "provenance"]
FirstFailure = Check | Literal["generation", "usability", "tone"]
Usability = Literal["as_written", "minor_edit", "material_edit", "missing_draft"]
USABILITY_SEVERITY: dict[Usability, int] = {
    "as_written": 0,
    "minor_edit": 1,
    "material_edit": 2,
    "missing_draft": 3,
}
type TurnId = tuple[str, int]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TurnKey(Record):
    case_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")]
    turn: Annotated[int, Field(ge=1)]

    @property
    def key(self) -> TurnId:
        return self.case_id, self.turn


class DatasetTurn(TurnKey):
    locale: Annotated[str, Field(pattern=r"^(?:[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*|und)$", max_length=35)]
    rights_ambiguity: Literal["clear", "ambiguous"]
    brief_availability: Literal["none", "available", "unavailable"]


class Dataset(Record):
    manifest_version: Literal["negotiation-dataset-v1"]
    dataset_version: Identifier
    kind: Literal["held_out", "pilot"]
    source_sha256: Digest
    turns: Annotated[list[DatasetTurn], Field(min_length=1)]


class Execution(Record):
    revision: Identifier
    workflow: Identifier
    prompt: Identifier
    retrieval: Identifier
    model: Identifier


class ObjectiveChecks(Record):
    amounts: bool
    terms: bool
    scope: bool
    approval: bool
    floor_privacy: bool
    commitment: bool
    provenance: bool


class PilotSignal(Record):
    disposition: Literal["sent_as_written", "minor_edit", "material_edit", "discarded"] | None = None
    reviewable_ms: Annotated[int, Field(ge=0)] | None = None
    estimated_cost_usd_micros: Annotated[int, Field(ge=0)] | None = None


class ReviewedTurn(TurnKey):
    usability: Usability
    tone: Literal["appropriate", "needs_edit", "no_draft"]
    checks: ObjectiveChecks
    first_failure: FirstFailure | None
    pilot: PilotSignal | None = None

    @property
    def failed_checks(self) -> set[str]:
        return {check for check, passed in self.checks.model_dump().items() if not passed}

    @model_validator(mode="after")
    def validate_labels(self):
        if (self.usability == "missing_draft") != (self.tone == "no_draft"):
            raise ValueError("missing drafts require the no_draft tone label")
        failures = self.failed_checks
        if self.usability == "as_written" and (failures or self.tone != "appropriate"):
            raise ValueError("as_written requires passing checks and appropriate tone")
        if self.usability == "missing_draft":
            failures.add("generation")
        elif self.usability != "as_written":
            failures.add("usability")
        if self.tone == "needs_edit":
            failures.add("tone")
        if (failures and self.first_failure not in failures) or (not failures and self.first_failure):
            raise ValueError("first_failure must identify an observed failure")
        if (
            self.usability == "missing_draft"
            and self.pilot
            and (self.pilot.reviewable_ms is not None or self.pilot.disposition is not None)
        ):
            raise ValueError("missing drafts cannot have a reviewable time or disposition")
        return self


class Review(Record):
    rubric_version: Literal["negotiation-review-v1"]
    dataset_sha256: Digest
    candidate: Execution
    blind_review_completed: bool
    turns: list[ReviewedTurn]

    @field_validator("blind_review_completed")
    @classmethod
    def verify_blind_attestation(cls, value: bool) -> bool:
        if not value:
            raise ValueError("blind review must be completed")
        return value


def index_turns[T: TurnKey](turns: list[T], source: str) -> dict[TurnId, T]:
    indexed = {turn.key: turn for turn in turns}
    if len(indexed) != len(turns):
        raise ValueError(f"{source} contains duplicate case and turn keys")
    return indexed


def review_turns(
    review: Review, dataset: dict[TurnId, DatasetTurn], digest: str, source: str
) -> dict[TurnId, ReviewedTurn]:
    if review.dataset_sha256 != digest:
        raise ValueError(f"{source} dataset SHA-256 does not match the supplied manifest")
    turns = index_turns(review.turns, source)
    if turns.keys() != dataset.keys():
        raise ValueError(f"{source} must contain exactly the supplied manifest's turns")
    return turns


def summarize_group(rows: list[ReviewedTurn]) -> dict:
    total = len(rows)
    cases = len({row.case_id for row in rows})
    usable = sum(row.usability == "as_written" for row in rows)
    critical = sum(bool(row.failed_checks) for row in rows)
    if critical:
        status = "fail"
    elif cases < MIN_CASES:
        status = "insufficient_evidence"
    else:
        status = "pass" if 100 * usable >= MIN_USABLE_PERCENT * total else "fail"
    return {
        "status": status,
        "cases": cases,
        "turns": total,
        "usable_as_written": usable,
        "usable_percent": round(100 * usable / total, 2),
        "tone_appropriate": sum(row.tone == "appropriate" for row in rows),
        "critical_turns": critical,
        "failed_checks": dict(Counter(check for row in rows for check in sorted(row.failed_checks))),
    }


def paired_changes(rows: list[ReviewedTurn], baseline: dict[TurnId, ReviewedTurn]) -> dict[str, int]:
    changes = dict.fromkeys(("usability_worse", "usability_better", "tone_lost", "tone_gained"), 0)
    for row in rows:
        prior = baseline[row.key]
        rating = USABILITY_SEVERITY[row.usability]
        prior_rating = USABILITY_SEVERITY[prior.usability]
        changes["usability_worse"] += rating > prior_rating
        changes["usability_better"] += rating < prior_rating
        was_appropriate = prior.tone == "appropriate"
        is_appropriate = row.tone == "appropriate"
        changes["tone_lost"] += was_appropriate and not is_appropriate
        changes["tone_gained"] += is_appropriate and not was_appropriate
    return changes


def summarize_pilot(rows: list[ReviewedTurn]) -> dict | None:
    signals = [row.pilot for row in rows if row.pilot is not None]
    if not signals:
        return None
    dispositions = Counter(signal.disposition for signal in signals if signal.disposition is not None)
    durations = [signal.reviewable_ms for signal in signals if signal.reviewable_ms is not None]
    cost_rows = [
        row for row in rows if row.pilot is not None and row.pilot.estimated_cost_usd_micros is not None
    ]
    total_cost = sum(row.pilot.estimated_cost_usd_micros for row in cost_rows)
    usable = sum(row.usability == "as_written" for row in cost_rows)
    sent = sum(row.pilot.disposition == "sent_as_written" for row in cost_rows)
    return {
        "disposition_turns": dispositions.total(),
        "dispositions": dict(dispositions),
        "reviewable_turns": len(durations),
        "median_reviewable_ms": median(durations) if durations else None,
        "cost_turns": len(cost_rows),
        "cost_sample_usable_as_written": usable,
        "estimated_cost_per_usable_draft_usd_micros": round(total_cost / usable, 2) if usable else None,
        "cost_sample_sent_as_written": sent,
        "estimated_cost_per_sent_as_written_usd_micros": round(total_cost / sent, 2) if sent else None,
    }


def summarize(dataset: Dataset, review: Review, digest: str, baseline: Review | None = None) -> dict:
    manifest = index_turns(dataset.turns, "dataset")
    turns = review_turns(review, manifest, digest, "review")
    thread_lengths = Counter(row.case_id for row in manifest.values())
    groups: dict[str, dict[str, list[ReviewedTurn]]] = {
        axis: {} for axis in ("locale", "thread_length", "rights_ambiguity", "brief_availability")
    }
    for key, row in manifest.items():
        length = "single" if thread_lengths[row.case_id] == 1 else "multi"
        for axis, value in (
            ("locale", row.locale),
            ("thread_length", length),
            ("rights_ambiguity", row.rights_ambiguity),
            ("brief_availability", row.brief_availability),
        ):
            groups[axis].setdefault(value, []).append(turns[key])

    rows = list(turns.values())
    overall = summarize_group(rows)
    slices = {
        axis: {name: summarize_group(group) for name, group in groups_by_axis.items()}
        for axis, groups_by_axis in groups.items()
    }
    comparison = None
    if baseline is not None:
        prior = review_turns(baseline, manifest, digest, "baseline")
        changes = paired_changes(rows, prior)
        comparison = {
            "status": "review_required"
            if changes["usability_worse"] or changes["tone_lost"]
            else "no_regression",
            "baseline": baseline.candidate.model_dump(),
            "overall": changes,
            "slices": {
                axis: {name: paired_changes(group, prior) for name, group in groups_by_axis.items()}
                for axis, groups_by_axis in groups.items()
            },
        }

    status = overall["status"]
    if any(result["status"] == "fail" for group in slices.values() for result in group.values()):
        status = "fail"
    if status == "pass" and comparison and comparison["status"] == "review_required":
        status = "review_required"
    return {
        "status": status,
        "rubric_version": review.rubric_version,
        "dataset": {
            "version": dataset.dataset_version,
            "kind": dataset.kind,
            "sha256": digest,
            "source_sha256": dataset.source_sha256,
        },
        "candidate": review.candidate.model_dump(),
        "gate": {"minimum_cases": MIN_CASES, "minimum_usable_percent": MIN_USABLE_PERCENT},
        "overall": overall,
        "slices": slices,
        "first_failures": dict(Counter(row.first_failure for row in rows if row.first_failure)),
        "comparison": comparison,
        "pilot_metrics": summarize_pilot(rows),
    }


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("JSON objects must not have duplicate fields")
    return result


def validate_file[T: Record](model: type[T], contents: bytes) -> T:
    return model.model_validate(json.loads(contents, object_pairs_hook=unique_object))


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("dataset", type=Path)
    args.add_argument("review", type=Path)
    args.add_argument("--baseline", type=Path)
    inputs = args.parse_args()
    source = "dataset"
    try:
        contents = inputs.dataset.read_bytes()
        dataset = validate_file(Dataset, contents)
        source = "review"
        review = validate_file(Review, inputs.review.read_bytes())
        source = "baseline"
        baseline = validate_file(Review, inputs.baseline.read_bytes()) if inputs.baseline else None
        report = summarize(dataset, review, sha256(contents).hexdigest(), baseline)
    except OSError:
        print(f"Could not read {source} file", file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(f"Invalid {source}: {exc.error_count()} schema validation error(s)", file=sys.stderr)
        return 2
    except (UnicodeError, json.JSONDecodeError):
        print(f"Invalid JSON in {source} file", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Invalid input: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
