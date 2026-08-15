"""Fail-open recorder and context propagation for concurrent LLM calls."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog

from agent_rag.config import agent_config, llm_config, observability_config
from agent_rag.telemetry.models import RunTelemetry, TelemetrySpan, utc_now
from agent_rag.telemetry.store import TelemetryStore, telemetry_store
from agent_rag.tracing import trace_runtime

logger = structlog.get_logger(__name__)
_current_run: ContextVar[str | None] = ContextVar("telemetry_run", default=None)
_current_recorder: ContextVar[Any | None] = ContextVar("telemetry_recorder", default=None)
_SECRET = re.compile(r"(?i)(sk-[a-z0-9_-]{8,}|bearer\s+[a-z0-9._-]+)")
_CONFIG_FINGERPRINT = hashlib.sha256(
    json.dumps(
        {
            "agent": agent_config,
            "llm": llm_config,
            "observability": observability_config,
        },
        sort_keys=True,
        default=str,
    ).encode()
).hexdigest()


def _safe_error(exc: BaseException | str) -> tuple[str, str]:
    category = exc.__class__.__name__ if isinstance(exc, BaseException) else "RuntimeError"
    message = _SECRET.sub("[REDACTED]", str(exc)).replace("\n", " ")[:500]
    return category, message


class TelemetryRecorder:
    def __init__(self, store: TelemetryStore = telemetry_store):
        self.store = store
        self.dropped_writes = 0

    @contextmanager
    def bind(self, run_id: str) -> Iterator[None]:
        run_token = _current_run.set(run_id)
        recorder_token = _current_recorder.set(self)
        try:
            yield
        finally:
            _current_recorder.reset(recorder_token)
            _current_run.reset(run_token)

    def start_run(
        self,
        run_id: str,
        run_type: str,
        *,
        root_run_id: str | None = None,
        parent_run_id: str | None = None,
        query: str = "",
        attributes: dict[str, Any] | None = None,
    ) -> None:
        normalized_query = " ".join(query.casefold().split())
        self._safe(
            self.store.start,
            RunTelemetry(
                run_id=run_id,
                run_type=run_type,
                root_run_id=root_run_id or run_id,
                parent_run_id=parent_run_id,
                query_hash=(
                    hashlib.sha256(normalized_query.encode()).hexdigest()
                    if normalized_query
                    else ""
                ),
                query_length=len(query),
                config_fingerprint=_CONFIG_FINGERPRINT,
                code_version=os.getenv("CODE_VERSION", "").strip(),
                attributes=attributes or {},
            ),
        )

    def finish_run(self, run_id: str, started: float, **updates: Any) -> None:
        updates.setdefault("completed_at", utc_now())
        updates.setdefault("duration_ms", int((time.perf_counter() - started) * 1000))
        self._safe(self.store.finish, run_id, **updates)

    def record_action(self, run_id: str, action: Any) -> None:
        if getattr(action, "status", "") == "started":
            return
        details = getattr(action, "details", {}) or {}
        allowed = {
            key: details[key]
            for key in (
                "iteration",
                "evidence",
                "frontier",
                "candidates",
                "route_mode",
                "route_source",
                "route_confidence",
                "relevant_blocks",
                "not_modified",
                "decision_action",
                "job_id",
                "patch_id",
                "job_status",
                "deduplicated",
                "webpages_written",
                "blocks_written",
                "links_written",
                "read_after_write_ok",
            )
            if key in details
        }
        error_category = ""
        if "error" in details:
            error_category, _ = _safe_error(str(details["error"]))
        self.record_span(
            run_id=run_id,
            stage=str(getattr(action, "action", "agent")),
            operation=str(getattr(action, "action", "agent")),
            status=(
                "failed"
                if getattr(action, "status", "") == "failed"
                else "skipped"
                if getattr(action, "status", "") == "skipped"
                else "succeeded"
            ),
            duration_ms=int(getattr(action, "duration_ms", 0) or 0),
            error_category=error_category,
            attributes=allowed,
        )

    def record_span(self, *, run_id: str | None = None, **values: Any) -> None:
        selected = run_id or _current_run.get()
        if not selected:
            return
        self._safe(
            self.store.append_span,
            TelemetrySpan(span_id=f"span-{uuid.uuid4().hex}", run_id=selected, **values),
        )
        attributes = values.get("attributes", {}) or {}
        mapped = {
            "iteration": attributes.get("iteration"),
            "evidence.count": attributes.get("evidence"),
            "frontier.count": attributes.get("frontier"),
            "candidate.count": attributes.get("candidates"),
            "route.mode": attributes.get("route_mode"),
            "route.source": attributes.get("route_source"),
            "route.confidence": attributes.get("route_confidence"),
            "relevant_block.count": attributes.get("relevant_blocks"),
            "webpage.count": attributes.get("webpages_written"),
            "block.count": attributes.get("blocks_written"),
            "link.count": attributes.get("links_written"),
            "cache.hit": values.get("cache_hit"),
            "token.input": values.get("logical_input_tokens"),
            "token.output": values.get("logical_output_tokens"),
        }
        trace_runtime.completed_span(
            "llm.chat" if values.get("operation") == "llm.chat" else "agent.stage",
            duration_ms=int(values.get("duration_ms", 0) or 0),
            status=str(values.get("status", "succeeded")),
            attributes={key: value for key, value in mapped.items() if value is not None},
            error_type=str(values.get("error_category", "")),
        )

    def record_llm(
        self,
        *,
        stage: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_hit: bool,
        duration_ms: int,
    ) -> None:
        self.record_span(
            stage=stage,
            operation="llm.chat",
            status="succeeded",
            duration_ms=duration_ms,
            logical_input_tokens=input_tokens,
            logical_output_tokens=output_tokens,
            billable_input_tokens=0 if cache_hit else input_tokens,
            billable_output_tokens=0 if cache_hit else output_tokens,
            llm_calls=1,
            cache_hit=cache_hit,
            provider=provider,
            model=model,
        )

    def _safe(self, callback, *args: Any, **kwargs: Any) -> Any:
        try:
            return callback(*args, **kwargs)
        except Exception as exc:
            self.dropped_writes += 1
            logger.warning(
                "telemetry_write_dropped",
                operation=getattr(callback, "__name__", "unknown"),
                error_category=exc.__class__.__name__,
            )
            return None


telemetry_recorder = TelemetryRecorder()


def record_current_llm_usage(**values: Any) -> None:
    recorder = _current_recorder.get() or telemetry_recorder
    recorder.record_llm(**values)
