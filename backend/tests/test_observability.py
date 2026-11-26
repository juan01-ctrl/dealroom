import json
from uuid import uuid4

import pytest
from langfuse import Langfuse
from langfuse.span_filter import is_langfuse_span
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.configuration import ConfigurationError
from app.evaluations import load_fixtures, run_suite
from app.models import ScriptedModel
from app.observability import langfuse_config, observe, use_langfuse
from app.worker import Worker


@pytest.fixture
def captured_langfuse(monkeypatch):
    exporter = InMemorySpanExporter()
    scores = []
    # The SDK serializes real OTel spans to an in-memory exporter, with the score sink intercepted.
    client = Langfuse(
        public_key=f"pk-lf-{uuid4()}",
        secret_key="sk-lf-test-only",
        base_url="http://127.0.0.1:1",
        tracer_provider=TracerProvider(),
        span_exporter=exporter,
        should_export_span=is_langfuse_span,
    )
    monkeypatch.setattr(client, "create_score", lambda **score: scores.append(score))
    try:
        yield client, exporter, scores
    finally:
        client.shutdown()


def test_langfuse_is_opt_in_and_validates_transport():
    assert langfuse_config({"LANGFUSE_PUBLIC_KEY": "test"}) is None
    with pytest.raises(ConfigurationError, match="LANGFUSE_SECRET_KEY"):
        langfuse_config({"ENABLE_LANGFUSE": "1"})
    env = {"ENABLE_LANGFUSE": "1", "LANGFUSE_PUBLIC_KEY": "test", "LANGFUSE_SECRET_KEY": "test"}
    with pytest.raises(ConfigurationError, match="HTTPS"):
        langfuse_config(env | {"APP_ENV": "production", "LANGFUSE_BASE_URL": "http://127.0.0.1:1"})
    assert langfuse_config(env | {"LANGFUSE_BASE_URL": "http://127.0.0.1:1"})["environment"] == "local"


def test_worker_emits_nested_sanitized_traces_and_persists_correlation(
    store, captured_langfuse, monkeypatch
):
    client, exporter, _ = captured_langfuse
    monkeypatch.setenv("MODEL_MODE", "live")
    monkeypatch.setenv("MODEL_NAME", "test-model")

    class MeteredModel(ScriptedModel):
        mode = "live"

        def extract(self, email, prior):
            offer, _ = super().extract(email, prior)
            return offer, {"input_tokens": 42, "output_tokens": 17}

    tenant, thread = store.create_session()
    receipt = store.ingest(
        tenant,
        thread,
        "test-otel-" + str(uuid4()),
        "Private marker 4813. USD 2,400 for one reel. https://briefs.dealroom.example/northstar-summer-launch",
    )
    with use_langfuse(client):
        assert Worker(store, model=MeteredModel()).step(receipt["job_id"])
    client.flush()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    root = spans["dealroom.job.analyze"]
    trace_id = f"{root.context.trace_id:032x}"
    for name in (
        "briefs.retrieve",
        "terms.extract",
        "policy.evaluate",
        "negotiation.select_move",
        "reply.compose",
    ):
        assert spans[f"dealroom.{name}"].context.trace_id == root.context.trace_id
    generation = spans["dealroom.terms.extract"]
    assert generation.parent.span_id == root.context.span_id
    assert '"input": 42' in str(generation.attributes)
    assert '"output": 17' in str(generation.attributes)
    snapshot = store.snapshot(tenant, thread)
    analysis = next(e for e in snapshot["timeline"] if e["kind"] == "analysis_completed")
    assert analysis["details"]["langfuse_trace_id"] == trace_id
    exported = json.dumps([span.to_json() for span in spans.values()])
    assert "Private marker 4813" not in exported
    assert "briefs.dealroom.example" not in exported
    assert "2,400" not in exported
    assert str(receipt["job_id"]) in exported


def test_eval_scores_record_the_assertion_on_the_correct_case_span(store, captured_langfuse):
    client, exporter, scores = captured_langfuse
    fixture = load_fixtures()[0] | {"expected": {"action": "acceptance"}}
    with use_langfuse(client):
        result = run_suite(store, ScriptedModel(), [fixture])
    client.flush()
    assert not result["scenarios"][0]["passed"]
    case = next(s for s in exporter.get_finished_spans() if s.name == "dealroom.eval.case")
    by_name = {score["name"]: score for score in scores}
    assert by_name["scenario_pass"]["value"] == 0
    assert by_name["assertion.action"]["value"] == 0
    assert by_name["assertion.action"]["observation_id"] == f"{case.context.span_id:016x}"
    assert by_name["assertion.action"]["trace_id"] == f"{case.context.trace_id:032x}"


def test_trace_errors_never_export_provider_exception_messages(captured_langfuse):
    client, exporter, _ = captured_langfuse
    with (
        use_langfuse(client),
        pytest.raises(RuntimeError, match="private exception marker"),
        observe("error"),
    ):
        raise RuntimeError("private exception marker")
    client.flush()
    span = exporter.get_finished_spans()[0].to_json()
    assert "RuntimeError" in span
    assert "private exception marker" not in span


def test_unavailable_tracer_does_not_change_job_result(store):
    class UnavailableClient:
        def start_as_current_observation(self, **_):
            raise RuntimeError("unavailable tracer")

    tenant, thread = store.create_session()
    receipt = store.ingest(tenant, thread, str(uuid4()), "USD 2,400 for one reel.")
    with use_langfuse(UnavailableClient()):
        assert Worker(store, model=ScriptedModel()).step(receipt["job_id"])
    assert store.snapshot(tenant, thread)["status"] == "AWAITING_APPROVAL"


def test_checkpoint_retry_has_its_own_trace_and_reuses_extraction(store, captured_langfuse):
    client, exporter, _ = captured_langfuse
    tenant, thread = store.create_session()
    store.arm_fault(tenant, thread, 0, "checkpoint")
    receipt = store.ingest(tenant, thread, str(uuid4()), "USD 2,400 for one reel.")
    with use_langfuse(client):
        assert Worker(store, model=ScriptedModel(), immediate=True).step(receipt["job_id"])
        assert Worker(store, model=ScriptedModel(), immediate=True).step(receipt["job_id"])
    client.flush()
    spans = exporter.get_finished_spans()
    attempts = [span for span in spans if span.name == "dealroom.job.analyze"]
    assert len({span.context.trace_id for span in attempts}) == 2
    assert all(str(receipt["job_id"]) in str(span.attributes) for span in attempts)
    assert sum(span.name == "dealroom.terms.extract" for span in spans) == 1
    assert any("SimulatedFailure" in str(span.attributes) for span in attempts)
    assert store.snapshot(tenant, thread)["status"] == "AWAITING_APPROVAL"
