import logging
import os
import time
from contextlib import nullcontext
from functools import partial
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import trace, tracing_context

from .campaign_briefs import CampaignBriefs
from .domain import Decision, Offer, Policy, Proposal, decide, merge
from .negotiation import select_negotiation_move
from .observability import observe
from .operations import execution_identity
from .retrieval import collect_evidence, links
from .sender import current_body

logger = logging.getLogger(__name__)


class State(TypedDict, total=False):
    email: str
    policy: dict
    previous: dict
    last_quote: dict | None
    unprocessed: list
    context_ids: list
    started_at: float
    retrieval: list
    retrieval_ms: int
    usage: dict
    model_calls: int
    terms: dict
    decision: dict
    proposal: dict


def add_usage(usage, additional):
    return {key: usage.get(key, 0) + additional.get(key, 0) for key in ("input_tokens", "output_tokens")}


def email_bodies(state: State):
    return [*(item["body"] for item in state["unprocessed"]), state["email"]]


def build_graph(model, saver, inputs=None, *, brief_reader=nullcontext):
    def source(state):
        return state if inputs is None else inputs

    def route(state: State):
        return (
            "retrieve"
            if any(links(current_body(email)) for email in email_bodies(source(state)))
            else "extract"
        )

    def retrieve(state: State):
        start = time.perf_counter()
        with observe("briefs.retrieve", as_type="retriever") as span:
            with brief_reader() as lookup:
                evidence = [
                    collect_evidence(current_body(email), lookup)
                    for email in email_bodies(source(state))
                ]
            span.update(
                output={
                    "documents": sum(len(item["sources"]) for item in evidence),
                    "denied_links": sum(item["denied_links"] for item in evidence),
                    "conflicting_fields": sum(len(item["conflicting_fields"]) for item in evidence),
                }
            )
        return {"retrieval": evidence, "retrieval_ms": round((time.perf_counter() - start) * 1000)}

    def extract(state: State):
        current = source(state)
        terms = Offer.model_validate(current["previous"])
        evidence = state.get("retrieval", [])
        if evidence and evidence[-1]["terms"]:
            terms = merge(terms, Offer.model_validate(evidence[-1]["terms"]))
        live = model.mode == "live"
        generation = {"model": os.getenv("MODEL_NAME")} if live else {}
        with observe("terms.extract", as_type="generation" if live else "chain", **generation) as span:
            patch, usage = model.extract(current["email"], terms.model_dump())
            if live and usage:
                span.update(
                    usage_details={
                        field: usage[key]
                        for key, field in (("input_tokens", "input"), ("output_tokens", "output"))
                        if key in usage
                    }
                )
        return {
            "terms": merge(terms, patch).model_dump(),
            "usage": add_usage({}, usage),
            "model_calls": int(model.mode == "live"),
        }

    def negotiate(decision: Decision, state: State):
        prior = source(state).get("last_quote") or {}
        with observe("negotiation.select_move", as_type="tool"):
            return Decision.model_validate(
                select_negotiation_move.invoke(
                    {
                        "decision": decision,
                        "offer": Offer.model_validate(state["terms"]),
                        "current_email": source(state)["email"],
                        "previous_decision": prior.get("decision"),
                        "previous_offer": prior.get("terms"),
                    }
                )
            )

    def price(state: State):
        with observe("policy.evaluate", as_type="guardrail") as span:
            result = evaluate_policy(state)
            span.update(
                output={"action": result["decision"]["action"], "move": result["decision"].get("move")}
            )
            return result

    def evaluate_policy(state: State):
        decision = decide(
            Offer.model_validate(state["terms"]), Policy.model_validate(source(state)["policy"])
        )
        if state.get("retrieval"):
            current = state["retrieval"][-1]
            if current["denied_links"] or current["conflicting_fields"]:
                question = (
                    "a shareable campaign brief"
                    if current["denied_links"]
                    else "the conflicting brief terms"
                )
                if decision.action == "acceptance":
                    decision = Decision(
                        action="clarification", checks=["Linked terms need confirmation."]
                    )
                if decision.action != "rejection":
                    decision.questions.append(question)
        return {"decision": negotiate(decision, state).model_dump()}

    def compose(state: State):
        with observe("reply.compose", as_type="chain"):
            return compose_proposal(state)

    def compose_proposal(state: State):
        decision = Decision.model_validate(state["decision"])
        if not decision.move:
            decision = negotiate(decision, state)
        return {
            "decision": decision.model_dump(),
            "proposal": Proposal(tone=source(state)["policy"]["tone"]).model_dump(),
        }

    builder = StateGraph(State)
    builder.add_node("retrieve", retrieve)
    builder.add_node("extract", extract)
    builder.add_node("price", price)
    builder.add_node("compose", compose)
    builder.add_conditional_edges(START, route, {"retrieve": "retrieve", "extract": "extract"})
    builder.add_edge("retrieve", "extract")
    builder.add_edge("extract", "price")
    builder.add_edge("price", "compose")
    builder.add_edge("compose", END)
    return builder.compile(checkpointer=saver)


class SimulatedFailure(Exception):
    pass


class PredecessorRecoveryRequired(Exception):
    pass


def telemetry(result, model):
    usage = result["usage"]
    rates = [os.getenv("PRICE_INPUT_USD_PER_MILLION"), os.getenv("PRICE_OUTPUT_USD_PER_MILLION")]
    price_as_of = os.getenv("PRICE_AS_OF")
    cost = None
    if model.mode == "live" and all(rates) and price_as_of:
        cost = round(
            sum(
                usage[key] * float(rate)
                for key, rate in zip(("input_tokens", "output_tokens"), rates, strict=True)
            )
            / 1_000_000,
            8,
        )
    evidence = result.get("retrieval", [])
    sources = {
        (source["document_id"], source["revision"]): source
        for item in evidence
        for source in item["sources"]
    }
    current_evidence = evidence[-1] if evidence else {}
    retrieval = {
        "sources": list(sources.values()),
        "field_sources": {
            field: locations
            for field, locations in current_evidence.get("field_sources", {}).items()
            if result["terms"].get(field) == current_evidence["terms"][field]
        },
        "latency_ms": result.get("retrieval_ms", 0),
        "denied_links": sum(item["denied_links"] for item in evidence),
        "attempted_links": sum(item["attempted_links"] for item in evidence),
        "conflicting_fields": sorted(
            {field for item in evidence for field in item["conflicting_fields"]}
        ),
    }
    reply = result["decision"]
    tools = ["extract_terms", "evaluate_policy", "render_draft"]
    if reply.get("move"):
        tools.insert(2, "select_negotiation_move")
    if evidence:
        tools.insert(0, "read_campaign_brief")
    return {
        "model_mode": model.mode,
        "model_name": os.getenv("MODEL_NAME") if model.mode == "live" else "scripted",
        "latency_ms": round((time.time() - result["started_at"]) * 1000),
        "usage": usage,
        "model_calls": result.get("model_calls"),
        "cost_usd": cost if model.mode == "live" else 0,
        "price_as_of": price_as_of,
        "context_ids": result["context_ids"],
        "tools": tools,
        "negotiation_move": reply.get("move"),
        "sender_evidence": reply.get("sender_evidence"),
        "retrieval": retrieval,
    }


def run_analysis(store, saver, job, model, *, trace_id=None):
    inputs = store.analysis_input(job)
    if inputs is None:
        return
    if inputs["unprocessed"]:
        raise PredecessorRecoveryRequired("Resolve earlier turns before retrying this one.")
    brief_reader = partial(CampaignBriefs(store).reader, job["tenant_id"], job["thread_id"])
    graph = build_graph(model, saver, inputs, brief_reader=brief_reader)
    config = {"configurable": {"thread_id": str(job["id"])}}
    snapshot = graph.get_state(config)
    identifiers = {"negotiation_id": str(job["thread_id"]), "turn_id": str(job["id"])}
    try:
        with tracing_context(enabled=False):
            if not snapshot.values:
                initial = {"context_ids": inputs["context_ids"], "started_at": time.time()}
                state = graph.invoke(initial, config, interrupt_before=["compose"], durability="sync")
            elif snapshot.next and snapshot.next != ("compose",):
                state = graph.invoke(None, config, interrupt_before=["compose"], durability="sync")
            else:
                state = snapshot.values
            if "proposal" not in state:
                if store.checkpoint_fault(job):
                    raise SimulatedFailure("checkpoint")
                state = graph.invoke(None, config, durability="sync")
        metadata = telemetry(state, model)
        metadata["execution"] = job["payload"].get("execution") or execution_identity(model.mode)
        if trace_id:
            metadata["langfuse_trace_id"] = trace_id
        if job["payload"].get("queued_build"):
            metadata["queued_build"] = job["payload"]["queued_build"]
        store.publish(
            job,
            Offer.model_validate(state["terms"]),
            Decision.model_validate(state["decision"]),
            Proposal.model_validate(state["proposal"]),
            metadata,
        )
    except Exception as exc:
        report_trace(model, identifiers, {"failure": type(exc).__name__})
        raise
    report_trace(
        model,
        identifiers,
        {
            "latency_ms": metadata["latency_ms"],
            "usage": metadata["usage"],
            "model_calls": metadata["model_calls"],
            "tools": metadata["tools"],
            "negotiation_move": metadata["negotiation_move"],
            "sender_evidence": metadata["sender_evidence"],
            "retrieval": {
                key: value
                for key, value in metadata["retrieval"].items()
                if key not in {"sources", "field_sources"}
            },
        },
    )


def report_trace(model, identifiers, outputs):
    # Build a span after analysis, with no provider errors or graph payload in scope.
    if os.getenv("ENABLE_LANGSMITH") != "1":
        return
    try:
        with (
            tracing_context(enabled=True),
            trace(
                "negotiation.analysis",
                inputs={"mode": model.mode},
                metadata=identifiers,
            ) as span,
        ):
            span.end(outputs=outputs)
    except Exception as exc:  # noqa: BLE001 - telemetry must not change job outcomes
        logger.warning("Trace export unavailable (%s)", type(exc).__name__)
