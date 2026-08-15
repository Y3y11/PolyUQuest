from __future__ import annotations

from datetime import UTC, datetime

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind

from agent_rag.agent.schemas import AgentQueryRequest
from agent_rag.indexing.outbox import IndexOutbox
from agent_rag.runs.store import AgentRunStore
from agent_rag.tools.schemas import GraphPatch
from agent_rag.tracing.runtime import (
    TraceRuntime,
    trace_id_from_traceparent,
    validate_traceparent,
)


def _request() -> AgentQueryRequest:
    return AgentQueryRequest(
        query="privacy-canary-secret-question",
        explore_web=False,
        persist_discoveries=False,
    )


def _patch() -> GraphPatch:
    now = datetime.now(UTC).isoformat()
    return GraphPatch(
        patch_id="patch-private",
        observation_id="observation-private",
        run_id="run-private",
        source_url="https://secret.example/private",
        content_hash="hash-private",
        created_at=now,
        updated_at=now,
    )


def test_trace_context_survives_both_durable_queues(tmp_path) -> None:
    exporter = InMemorySpanExporter()
    runtime = TraceRuntime(
        mode="propagate",
        service_name="contract-test",
        sample_ratio=1.0,
        exporter=exporter,
    )
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    outbox = IndexOutbox(tmp_path / "outbox.sqlite3")

    with runtime.span("agent.run.submit", kind=SpanKind.PRODUCER) as submitted:
        run, _ = store.create(
            _request(),
            "trace-contract-00000001",
            traceparent=submitted.traceparent,
        )
    restored = AgentRunStore(store.path).get(run.run_id)
    assert restored is not None
    assert restored.trace_id == submitted.trace_id

    with runtime.span(
        "agent.run.execute",
        traceparent=restored.traceparent,
        kind=SpanKind.CONSUMER,
        attributes={"attempt": 1, "query": "must-be-dropped"},
    ):
        runtime.completed_span(
            "agent.stage",
            duration_ms=2,
            status="succeeded",
            attributes={"evidence.count": 2, "url": "must-be-dropped"},
        )
        job, _ = outbox.enqueue(
            _patch(), traceparent=runtime.current_traceparent()
        )

    restored_job = IndexOutbox(outbox.path).get(job.job_id)
    assert restored_job is not None
    with runtime.span(
        "knowledge.index.execute",
        traceparent=restored_job.traceparent,
        kind=SpanKind.CONSUMER,
        attributes={"attempt": 1},
    ):
        pass
    runtime.shutdown()

    spans = exporter.get_finished_spans()
    assert {span.context.trace_id for span in spans} == {int(submitted.trace_id, 16)}
    by_name = {span.name: span for span in spans}
    assert by_name["agent.run.execute"].kind is SpanKind.CONSUMER
    assert by_name["knowledge.index.execute"].kind is SpanKind.CONSUMER
    assert by_name["agent.run.execute"].parent.span_id == by_name[
        "agent.run.submit"
    ].context.span_id
    assert by_name["knowledge.index.execute"].parent.span_id == by_name[
        "agent.run.execute"
    ].context.span_id
    serialized = repr([(span.name, dict(span.attributes)) for span in spans])
    for forbidden in (
        "privacy-canary",
        "secret.example",
        "must-be-dropped",
        "patch-private",
        "run-private",
    ):
        assert forbidden not in serialized


def test_traceparent_validation_and_disabled_mode() -> None:
    valid = "00-11111111111111111111111111111111-2222222222222222-01"
    assert validate_traceparent(valid.upper()) == valid
    assert trace_id_from_traceparent(valid) == "1" * 32
    assert validate_traceparent("00-" + "0" * 32 + "-" + "2" * 16 + "-01") == ""
    assert validate_traceparent("not-a-trace") == ""

    runtime = TraceRuntime(mode="disabled")
    with runtime.span("agent.run.submit", traceparent=valid) as active:
        assert active.traceparent == ""
        assert active.trace_id is None
        assert runtime.current_traceparent() == ""
