"""Real OTLP Collector -> Tempo privacy and query contract gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

from agent_rag.tracing.backend import TempoTraceClient

_CANARIES = (
    "resource-canary-private",
    "query-canary-private",
    "url-canary-private",
    "status-canary-private",
    "event-canary-private",
    "dynamic-canary-private",
)


async def _wait_ready(url: str, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    delay = 0.1
    async with httpx.AsyncClient(timeout=2, follow_redirects=False) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return
            except httpx.RequestError:
                pass
            await asyncio.sleep(delay)
            delay = min(1.0, delay * 1.5)
    raise TimeoutError("Tracing dependency did not become ready")


def _emit_canary(otlp_endpoint: str) -> str:
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint, timeout=5)
    provider = TracerProvider(
        resource=Resource.create({
            "service.name": "polyuquest-e2e",
            "secret.resource": _CANARIES[0],
        })
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("private-scope-canary")
    with tracer.start_as_current_span(
        "agent.run.submit",
        kind=SpanKind.PRODUCER,
        attributes={"attempt": 1, "query": _CANARIES[1]},
    ) as root:
        trace_id = trace.format_trace_id(root.get_span_context().trace_id)
        with tracer.start_as_current_span(
            f"GET /private/{_CANARIES[5]}",
            kind=SpanKind.CLIENT,
            attributes={
                "evidence.count": 2,
                "url.full": f"https://private.example/{_CANARIES[2]}",
            },
        ) as unsafe:
            unsafe.set_status(Status(StatusCode.ERROR, _CANARIES[3]))
            try:
                raise RuntimeError(_CANARIES[4])
            except RuntimeError as exc:
                unsafe.record_exception(exc)
        with tracer.start_as_current_span(
            "knowledge.index.execute",
            kind=SpanKind.CONSUMER,
            attributes={"attempt": 1, "block.count": 3},
        ):
            pass
    if not provider.force_flush(timeout_millis=10_000):
        raise RuntimeError("OTLP force flush failed")
    provider.shutdown()
    return trace_id


async def _query_raw(tempo_url: str, trace_id: str) -> dict[str, Any] | None:
    async with httpx.AsyncClient(timeout=3, follow_redirects=False) as client:
        response = await client.get(
            f"{tempo_url.rstrip('/')}/api/v2/traces/{trace_id}",
            headers={"Accept": "application/json"},
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("Tempo returned a non-object trace")
    return value


async def run_gate(
    *,
    otlp_endpoint: str,
    tempo_url: str,
    collector_health_url: str,
    output: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    await asyncio.gather(
        _wait_ready(f"{tempo_url.rstrip('/')}/ready", timeout_seconds=timeout_seconds),
        _wait_ready(collector_health_url, timeout_seconds=timeout_seconds),
    )
    trace_id = await asyncio.to_thread(_emit_canary, otlp_endpoint)
    deadline = time.monotonic() + timeout_seconds
    raw: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        raw = await _query_raw(tempo_url, trace_id)
        if raw is not None:
            break
        await asyncio.sleep(0.25)
    if raw is None:
        raise TimeoutError("Tempo did not make the trace queryable")

    raw_text = json.dumps(raw, ensure_ascii=False)
    raw_privacy = {canary: canary not in raw_text for canary in _CANARIES}
    if not all(raw_privacy.values()):
        raise AssertionError("Collector privacy processor leaked a canary")

    client = TempoTraceClient(
        enabled=True,
        base_url=tempo_url,
        timeout_seconds=3,
        max_response_bytes=2_097_152,
        max_spans=50,
    )
    waterfall = await client.get(trace_id)
    names = [span.name for span in waterfall.spans]
    expected_names = {
        "agent.run.submit",
        "runtime.unknown",
        "knowledge.index.execute",
    }
    checks = {
        "three_spans_returned": waterfall.returned_span_count == 3,
        "names_normalized": expected_names <= set(names),
        "service_allowlisted": waterfall.services == ["polyuquest-e2e"],
        "parent_relationships_present": sum(
            bool(span.parent_span_id) for span in waterfall.spans
        )
        == 2,
        "raw_privacy": all(raw_privacy.values()),
        "safe_view_privacy": all(
            canary not in waterfall.model_dump_json() for canary in _CANARIES
        ),
    }
    if not all(checks.values()):
        raise AssertionError(f"Tracing E2E contract failed: {checks}")
    report = {
        "schema_version": 1,
        "status": "passed",
        "trace_id": trace_id,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "services": waterfall.services,
        "span_names": names,
        "span_count": waterfall.span_count,
        "duration_ms": waterfall.duration_ms,
        "checks": checks,
        "privacy_canary_count": len(_CANARIES),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--otlp-endpoint", default="http://127.0.0.1:14318/v1/traces"
    )
    parser.add_argument("--tempo-url", default="http://127.0.0.1:13200")
    parser.add_argument(
        "--collector-health-url", default="http://127.0.0.1:13133/"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/runtime/tracing-e2e/report.json"),
    )
    parser.add_argument("--timeout-seconds", type=float, default=60)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = asyncio.run(
        run_gate(
            otlp_endpoint=args.otlp_endpoint,
            tempo_url=args.tempo_url,
            collector_health_url=args.collector_health_url,
            output=args.output,
            timeout_seconds=args.timeout_seconds,
        )
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
