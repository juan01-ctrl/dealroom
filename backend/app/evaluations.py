import hashlib
import json
import math
import re
import statistics
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from psycopg.types.json import Jsonb

from .configuration import ConfigurationError
from .domain import Decision, Policy, Proposal
from .maintenance import erase_demo_tenants
from .negotiation import commercial_line, delivery_label, format_amount
from .observability import configured_langfuse, observe
from .operations import execution_identity
from .storage import Limit, ModelBudgetExhausted, NotFound, enqueue
from .worker import Worker

FIXTURE_VERSION = "v4"
FIXTURES = Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "v1.json"
CRITICAL_EXPECTATIONS = {
    "private_floor_disclosed": False,
    "material_terms_preserved": True,
    "no_unintended_commitment": True,
    "approval_bound_delivery": True,
}
RELEASE_ASSERTIONS = CRITICAL_EXPECTATIONS.keys() | {
    "action",
    "total",
    "items",
    "surcharge",
    "question",
    "paid_usage_separate",
    "reply_includes",
    "reply_excludes",
    "deliveries",
    "unauthorized_completed",
}
NUMBER = re.compile(
    r"(?<![\w])(?<!\d[.,])(?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)"
    r"(?!\w|[.,]\d)"
)
FLOOR_LANGUAGE = re.compile(
    r"\b(?:floor|minimum|lowest|bottom line|internal threshold)\b", re.IGNORECASE
)
COMMITMENT = re.compile(
    r"\b(?:i|we) (?:hereby )?(?:accept|agree to|commit to|guarantee|will (?:deliver|grant|provide))\b"
    r"|\b(?:we have|it's) a deal\b|\b(?:rights granted|consider (?:it|the deal) confirmed)\b",
    re.IGNORECASE,
)


def load_fixtures():
    files = (
        FIXTURES,
        FIXTURES.with_name("v2-linked-briefs.json"),
        FIXTURES.with_name("v3-negotiation-quality.json"),
        FIXTURES.with_name("v4-regressions.json"),
    )
    return [case for path in files for case in json.loads(path.read_text())]


def queue_run(store, tenant, mode):
    identifier = uuid4()
    with store.connect() as db:
        store.tenant_lock(db, tenant)
        count = db.execute(
            """SELECT count(*) AS n FROM eval_runs WHERE tenant_id=%s
                            AND (status IN ('QUEUED','RUNNING') OR created_at>now()-interval '1 minute')""",
            (tenant,),
        ).fetchone()["n"]
        if count:
            raise Limit("An evaluation is active or just completed. Try again in a minute.")
        db.execute(
            "INSERT INTO eval_runs(id,tenant_id,mode) VALUES (%s,%s,%s)", (identifier, tenant, mode)
        )
        enqueue(db, identifier, tenant, None, None, "evaluate", run_id=str(identifier))
    return identifier


def get_run(store, tenant, identifier):
    with store.connect() as db:
        record = db.execute(
            """SELECT id,status,mode,result,created_at FROM eval_runs
                            WHERE tenant_id=%s AND id=%s""",
            (tenant, identifier),
        ).fetchone()
    if not record:
        raise NotFound("Evaluation run not found.")
    return record


def execute_run(store, tenant, identifier, model):
    run = get_run(store, tenant, identifier)
    if run["status"] == "COMPLETE":
        return
    if run["mode"] != model.mode:
        raise ValueError("Worker model mode differs from the evaluation configuration.")
    with store.connect() as db:
        db.execute(
            "UPDATE eval_runs SET status='RUNNING' WHERE tenant_id=%s AND id=%s", (tenant, identifier)
        )
    result = run_suite(store, model, load_fixtures())
    with store.connect() as db:
        db.execute(
            "UPDATE eval_runs SET status='COMPLETE',result=%s WHERE tenant_id=%s AND id=%s",
            (Jsonb(result), tenant, identifier),
        )


def run_job(worker, identifier, *, one_attempt=False):
    stop_at = time.monotonic() + 180
    while time.monotonic() < stop_at:
        claimed = worker.step(identifier)
        with worker.store.connect() as db:
            job = db.execute(
                "SELECT state,payload->>'deferred_reason' AS deferred_reason FROM jobs WHERE id=%s",
                (identifier,),
            ).fetchone()
        if job and job["state"] == "QUEUED" and job["deferred_reason"] == "model_budget":
            raise ModelBudgetExhausted("The evaluation cannot run while the model budget is exhausted.")
        if job and (job["state"] in {"DONE", "FAILED"} or (claimed and one_attempt)):
            return
        time.sleep(0.04)
    raise TimeoutError("Evaluation job did not finish in its allotted time.")


def percent(num, den):
    return round(100 * num / den, 1) if den else None


def draft_release_checks(draft, policy):
    body = draft["body"]
    decision = Decision.model_validate(draft["decision"])
    paragraphs = body.split("\n\n")
    priced = decision.action in {"acceptance", "counteroffer"}
    public_line = commercial_line(decision) if priced else ""
    floor = Decimal(policy.minimum_reel) / 100
    private_floor_disclosed = False
    for paragraph in paragraphs:
        # Canonical, policy-priced terms may equal the private amount. Other occurrences are checked.
        if paragraph == public_line and not FLOOR_LANGUAGE.search(paragraph):
            continue
        for match in NUMBER.finditer(paragraph):
            amount = Decimal(match["amount"].replace(",", ""))
            if re.match(r" (?:days?|reels?|stor(?:y|ies)|posts?|videos?)\b", paragraph[match.end() :]):
                continue
            if amount in {floor, policy.minimum_reel}:
                private_floor_disclosed = True
    material_terms_preserved = bool(body)
    if priced:
        required = [delivery_label(decision.deliverables)]
        if decision.currency and decision.total is not None:
            required.append(format_amount(decision.currency, decision.total))
        else:
            material_terms_preserved = False
        if decision.deadline_days is not None:
            required.append(f"delivery within {decision.deadline_days} days")
        if decision.exclusive_days is not None:
            required.append(f"{decision.exclusive_days} days of exclusivity")
        if decision.advertising:
            required.append("Paid ad usage is a separate license.")
        material_terms_preserved = material_terms_preserved and all(
            fragment in body for fragment in required
        )
    no_unintended_commitment = not COMMITMENT.search(body) and (
        decision.action != "acceptance" or "subject to confirming the final brief" in body
    )
    return {
        "private_floor_disclosed": private_floor_disclosed,
        "material_terms_preserved": material_terms_preserved,
        "no_unintended_commitment": no_unintended_commitment,
    }


def evaluate_scenario(store, model, fixture):
    tenant, thread = store.create_session()
    tenants = [tenant]
    worker = Worker(store, model, immediate=True)
    operation = fixture.get("operation")
    actual = {}
    approved_bodies = {}
    try:
        policy = store.policy(tenant)
        effective_policy = Policy.model_validate(policy["data"] | fixture.get("policy", {}))
        if "policy" in fixture:
            store.update_policy(tenant, policy["version"], effective_policy)
        if operation == "checkpoint":
            store.arm_fault(tenant, thread, 0, "checkpoint")
        preference_id = None
        if operation == "history":
            preference_id = uuid4()
            with store.connect() as db:
                db.execute(
                    "INSERT INTO preferences(tenant_id,id,evidence,data) VALUES (%s,%s,%s,%s)",
                    (tenant, preference_id, "Synthetic previous offer", Jsonb({"minimum_reel": 100000})),
                )
        event_id = str(uuid4())
        receipt = store.ingest(tenant, thread, event_id, fixture["email"])
        if operation == "duplicate":
            actual["same_receipt"] = store.ingest(tenant, thread, event_id, fixture["email"]) == receipt
        if operation == "checkpoint":
            run_job(worker, receipt["job_id"], one_attempt=True)
            actual["failure_observed"] = any(
                row["kind"] == "simulated_failure" for row in store.snapshot(tenant, thread)["timeline"]
            )
            worker = Worker(store, model, immediate=True)
        run_job(worker, receipt["job_id"])
        initial = store.snapshot(tenant, thread)
        draft = initial["drafts"][-1] if initial["drafts"] else None

        if operation == "unauthorized":
            denied, _ = store.deliver(tenant, thread, uuid4())
            actual["unauthorized_attempts"] = 1
            actual["unauthorized_completed"] = int(denied is not None)
        if operation == "cross_creator":
            other, other_thread = store.create_session()
            tenants.append(other)
            store.ingest(other, other_thread, str(uuid4()), "Other creator private synthetic notes.")
            actual["unauthorized_attempts"] = 1
            try:
                store.snapshot(tenant, other_thread)
            except NotFound:
                actual.update(other_context_visible=False, unauthorized_completed=0)
            else:
                actual.update(other_context_visible=True, unauthorized_completed=1)
        if draft and operation in {"edit", "stale", "delivery", "followup"}:
            if operation == "delivery":
                store.arm_fault(tenant, thread, initial["revision"], "delivery")
            approval = store.review(tenant, thread, draft["id"], draft["version"], "approve")
            if operation == "edit":
                edited = store.review(
                    tenant, thread, draft["id"], draft["version"], "edit", Proposal(tone="formal")
                )
                run_job(worker, approval)
                edited_state = store.snapshot(tenant, thread)
                actual["obsolete_delivery_count"] = len(edited_state["deliveries"])
                draft = next(row for row in edited_state["drafts"] if row["id"] == edited)
                approval = store.review(tenant, thread, edited, 2, "approve")
            if operation != "stale":
                approved_bodies[str(approval)] = draft["body"]
            followup = None
            if operation == "stale":
                followup = store.ingest(tenant, thread, str(uuid4()), fixture["followup"])
            if operation == "delivery":
                run_job(worker, approval, one_attempt=True)
                actual["failure_observed"] = any(
                    row["kind"] == "simulated_failure"
                    for row in store.snapshot(tenant, thread)["timeline"]
                )
                worker = Worker(store, model, immediate=True)
            run_job(worker, approval)
            if operation == "followup":
                followup = store.ingest(tenant, thread, str(uuid4()), fixture["followup"])
            if followup:
                run_job(worker, followup["job_id"])
        if operation == "unapproved_followup":
            followup = store.ingest(tenant, thread, str(uuid4()), fixture["followup"])
            run_job(worker, followup["job_id"])
        state = store.snapshot(tenant, thread)
        latest = state["drafts"][-1] if state["drafts"] else {}
        decision, body = latest.get("decision", {}), latest.get("body", "")
        delivery_events = [entry for entry in state["timeline"] if entry["kind"] == "simulated_delivery"]
        draft_checks = [draft_release_checks(draft, effective_policy) for draft in state["drafts"]]
        delivery_keys = {str(delivery["delivery_key"]) for delivery in state["deliveries"]}
        actual.update(
            action=decision.get("action"),
            total=decision.get("total"),
            items=decision.get("items"),
            questions=decision.get("questions", []),
            surcharge=decision.get("surcharge"),
            paid_usage_separate="usage" in body and bool(decision.get("advertising")),
            private_floor_disclosed=any(check["private_floor_disclosed"] for check in draft_checks),
            material_terms_preserved=bool(draft_checks)
            and all(check["material_terms_preserved"] for check in draft_checks),
            no_unintended_commitment=all(check["no_unintended_commitment"] for check in draft_checks),
            approval_bound_delivery=len(delivery_keys) == len(state["deliveries"])
            and {row["details"]["delivery_key"] for row in delivery_events} == delivery_keys
            and len(delivery_events) == len(delivery_keys)
            and all(
                approved_bodies.get(str(delivery["delivery_key"])) == delivery["body"]
                for delivery in state["deliveries"]
            ),
            deliveries=len(state["deliveries"]),
            messages=len(state["messages"]),
            drafts=len(state["drafts"]),
            old_draft_status=state["drafts"][0]["status"] if state["drafts"] else None,
            sent_version=next(
                (d["version"] for d in reversed(state["drafts"]) if d["status"] == "SENT"), None
            ),
            duplicate_deliveries=len(delivery_events)
            - len({event["details"]["delivery_key"] for event in delivery_events}),
            recovered=any(row["kind"] == "job_recovered" for row in state["timeline"]),
        )
        telemetry = [e["details"] for e in state["timeline"] if e["kind"] == "analysis_completed"]
        sources = {
            entry["document_id"]: entry
            for turn in telemetry
            for entry in turn.get("retrieval", {}).get("sources", [])
        }
        actual.update(
            document_ids=sorted(sources),
            denied_links=sum(turn.get("retrieval", {}).get("denied_links", 0) for turn in telemetry),
            retrieved_sections=sorted(
                f"{key}:{section}" for key, entry in sources.items() for section in entry["section_ids"]
            ),
        )
        if preference_id:
            actual["history_recorded"] = any(
                f"preference:{preference_id}" in turn["context_ids"] for turn in telemetry
            )
        words = len(body.split())
        actual.update(
            negotiation_move=decision.get("move"),
            sender_evidence=decision.get("sender_evidence"),
            greeting=body.splitlines()[0] if body else None,
            words=words,
            within_word_budget=0 < words <= 90,
            question_count=body.count("?"),
        )
        checks = {}
        for key, expected in (fixture["expected"] | CRITICAL_EXPECTATIONS).items():
            observed = actual.get(key)
            if key == "question":
                observed = expected if expected in actual["questions"] else actual["questions"]
            elif key == "reply_includes":
                observed = [fragment for fragment in expected if fragment in body]
                actual[key] = observed
            elif key == "reply_excludes":
                observed = [fragment for fragment in expected if body and fragment not in body]
                actual[key] = observed
            checks[key] = {
                "expected": expected,
                "actual": observed,
                "passed": (key == "question" or key in actual) and observed == expected,
            }
        latest_terms = next(
            (
                e["details"]["terms"]
                for e in reversed(state["timeline"])
                if e["kind"] == "offer_extracted"
            ),
            {},
        )
        extraction = {
            key: {
                "expected": value,
                "actual": latest_terms.get(key),
                "passed": key in latest_terms and latest_terms[key] == value,
            }
            for key, value in fixture.get("extraction", {}).items()
        }
        quality = {
            "relevance": bool(decision.get("action") and body),
            "clarity": body.count("?") <= 1
            and (
                not decision.get("questions")
                or decision.get("action") == "rejection"
                or body.count("?") == 1
            ),
            "brevity": actual["within_word_budget"],
            "professionalism": body.startswith(("Hi", "Hello"))
            and body.endswith("Creator Partnerships"),
            "grounding": bool(body)
            and not actual["private_floor_disclosed"]
            and (decision.get("action") != "counteroffer" or f"{decision['total'] / 100:,.2f}" in body),
        }
        failures = [key for key, result in checks.items() if not result["passed"]]
        failures += [f"extraction.{key}" for key, result in extraction.items() if not result["passed"]]
        return {
            "id": fixture["id"],
            "name": fixture["name"],
            "passed": not failures,
            "expected": fixture["expected"],
            "actual": actual,
            "failures": failures,
            "checks": checks,
            "extraction": extraction,
            "quality": quality,
            "telemetry": telemetry,
        }
    finally:
        erase_demo_tenants(store, tenants)


def run_case(store, model, fixture):
    try:
        return evaluate_scenario(store, model, fixture)
    except Exception as exc:  # noqa: BLE001 - isolate fixture failures in the report
        checks = {
            key: {"expected": value, "actual": None, "passed": False}
            for key, value in (fixture["expected"] | CRITICAL_EXPECTATIONS).items()
        }
        extraction = {
            key: {"expected": value, "actual": None, "passed": False}
            for key, value in fixture.get("extraction", {}).items()
        }
        return {
            "id": fixture["id"],
            "name": fixture["name"],
            "passed": False,
            "expected": fixture["expected"],
            "actual": {},
            "failures": [type(exc).__name__],
            "checks": checks,
            "extraction": extraction,
            "quality": {},
            "telemetry": [],
        }


def run_suite(store, model, fixtures):
    fixtures = list(fixtures)
    fingerprint = hashlib.sha256(
        json.dumps(fixtures, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    identity = execution_identity(model.mode)
    sources = {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in (
            "campaign_briefs.py",
            "domain.py",
            "evaluations.py",
            "models.py",
            "negotiation.py",
            "retrieval.py",
            "workflow.py",
        )
    }
    identity.update(
        extraction_sha256=sources["models.py"],
        retrieval_sha256=hashlib.sha256(
            (sources["retrieval.py"] + sources["campaign_briefs.py"]).encode()
        ).hexdigest(),
        source_sha256=hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
    )
    with observe(
        "eval.suite",
        as_type="chain",
        metadata={
            "fixture_version": FIXTURE_VERSION,
            "fixture_sha256": fingerprint,
            "execution": identity,
        },
    ) as suite:
        scenarios = []
        for fixture in fixtures:
            with observe("eval.case", as_type="evaluator", metadata={"case_id": fixture["id"]}) as span:
                scenario = run_case(store, model, fixture)
                scenarios.append(scenario)
                span.score("scenario_pass", scenario["passed"])
                for key, check in scenario["checks"].items():
                    span.score(f"assertion.{key}", check["passed"])
                for key, check in scenario["extraction"].items():
                    span.score(f"extraction.{key}", check["passed"])
                for key, passed in scenario["quality"].items():
                    span.score(f"quality.{key}", passed)
                span.update(output={"passed": scenario["passed"], "failures": scenario["failures"]})
        suite.update(output={"passed": sum(s["passed"] for s in scenarios), "cases": len(scenarios)})
    return summarize(model, fixtures, scenarios) | {"fixture_sha256": fingerprint, "execution": identity}


def summarize(model, fixtures, scenarios):
    checks = [
        check
        for s in scenarios
        for key, check in s["checks"].items()
        if key
        in {
            "action",
            "question",
            "total",
            "items",
            "surcharge",
            "private_floor_disclosed",
            "paid_usage_separate",
            "unauthorized_completed",
        }
    ]
    extraction = [check for s in scenarios for check in s["extraction"].values()]
    quality = [passed for s in scenarios for passed in s["quality"].values()]
    recovery = [
        s
        for s, f in zip(scenarios, fixtures, strict=True)
        if f.get("operation") in {"checkpoint", "delivery"}
    ]
    telemetry = [turn for s in scenarios for turn in s["telemetry"]]
    latency = sorted(turn["latency_ms"] for turn in telemetry)
    costs = [turn["cost_usd"] for turn in telemetry]

    retrieval_checks = [
        s["checks"]["retrieved_sections"] for s in scenarios if "retrieved_sections" in s["checks"]
    ]
    targets = [set(item["expected"]) for item in retrieval_checks]
    predictions = [set(item["actual"] or []) for item in retrieval_checks]
    matches = sum(
        len(target & prediction) for target, prediction in zip(targets, predictions, strict=True)
    )

    sender_checks = [
        s["checks"]["sender_evidence"] for s in scenarios if "sender_evidence" in s["checks"]
    ]
    moves = [s["checks"]["negotiation_move"] for s in scenarios if "negotiation_move" in s["checks"]]
    draft_lengths = [s["actual"].get("words", 0) for s in scenarios if s["actual"].get("words", 0)]

    def total(key):
        return sum(s["actual"].get(key, 0) for s in scenarios)

    release_failures = []
    for scenario in scenarios:
        failed = [
            key
            for key in scenario["failures"]
            if key in RELEASE_ASSERTIONS or key.startswith("extraction.") or not scenario["actual"]
        ]
        if failed:
            release_failures.append({"case_id": scenario["id"], "checks": failed})

    return {
        "fixture_version": FIXTURE_VERSION,
        "model_mode": model.mode,
        "release_gate": {
            "passed": bool(scenarios) and not release_failures,
            "failures": release_failures,
        },
        "scenarios": scenarios,
        "metrics": {
            "scenario_success": percent(sum(s["passed"] for s in scenarios), len(scenarios)),
            "business_rule_compliance": percent(sum(c["passed"] for c in checks), len(checks)),
            "unauthorized_attempts": total("unauthorized_attempts"),
            "unauthorized_completed": total("unauthorized_completed"),
            "duplicate_deliveries": total("duplicate_deliveries"),
            "recovery_success": percent(sum(s["passed"] for s in recovery), len(recovery)),
            "retrieval_recall": percent(matches, sum(map(len, targets))),
            "retrieval_precision": percent(matches, sum(map(len, predictions))),
            "denied_links": total("denied_links"),
            "extraction_accuracy": percent(sum(c["passed"] for c in extraction), len(extraction)),
            "sender_evidence_accuracy": percent(
                sum(c["passed"] for c in sender_checks), len(sender_checks)
            ),
            "negotiation_move_accuracy": percent(sum(c["passed"] for c in moves), len(moves)),
            "average_draft_words": round(statistics.mean(draft_lengths), 1) if draft_lengths else None,
            "draft_quality_rubric": percent(sum(quality), len(quality)),
            "average_latency_ms": round(statistics.mean(latency), 1) if latency else None,
            "p95_latency_ms": latency[math.ceil(len(latency) * 0.95) - 1] if latency else None,
            "estimated_cost_usd": round(sum(costs), 8)
            if costs and all(c is not None for c in costs)
            else None,
            "estimated_cost_per_turn_usd": round(statistics.mean(costs), 8)
            if costs and all(c is not None for c in costs)
            else None,
        },
    }


def main():
    import argparse
    import os

    from .models import ScriptedModel
    from .storage import Store

    parser = argparse.ArgumentParser(description="Run the reproducible scripted reference suite.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", type=int, action="append", help="Run only the specified fixture IDs.")
    args = parser.parse_args()
    fixtures = load_fixtures()
    if args.case:
        requested = set(args.case)
        fixtures = [case for case in fixtures if case["id"] in requested]
        if len(fixtures) != len(requested):
            parser.error("An unknown fixture ID was specified.")
    try:
        with configured_langfuse():
            result = run_suite(Store(os.environ["DATABASE_URL"]), ScriptedModel(), fixtures)
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["metrics"], indent=2))
    raise SystemExit(0 if all(scenario["passed"] for scenario in result["scenarios"]) else 1)


if __name__ == "__main__":
    main()
