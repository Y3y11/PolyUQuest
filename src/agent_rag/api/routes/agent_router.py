"""HTTP and SSE endpoints for the bounded query-driven retrieval agent."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry.trace import SpanKind

from agent_rag.agent.schemas import (
    AgentQueryRequest,
    AgentQueryResponse,
    AgentRunSnapshot,
    AgentRunSubmission,
)
from agent_rag.config import settings
from agent_rag.runs.admission import (
    AgentRunAdmissionPolicy,
    AgentRunAdmissionRejectedError,
    AgentRunBudgetRejectedError,
)
from agent_rag.runs.models import AgentRunEvent, AgentRunRecord
from agent_rag.runs.store import (
    IdempotencyConflictError,
    agent_run_store,
)
from agent_rag.runtime import build_query_agent
from agent_rag.security.auth import require_role
from agent_rag.security.end_user import EndUser
from agent_rag.security.models import EndUserIdentity, Role
from agent_rag.tracing import trace_runtime
from agent_rag.workers.status import worker_status_store

logger = structlog.get_logger(__name__)
router = APIRouter(dependencies=[Depends(require_role(Role.reader))])
_RUN_ID = re.compile(r"^run-[a-f0-9]{32}$")


def _sse_event(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stored_sse_event(event: AgentRunEvent) -> str:
    return (
        f"id: {event.event_id}\n"
        f"event: {event.event_type}\n"
        f"data: {event.payload_json}\n\n"
    )


def _require_run_id(run_id: str) -> str:
    if not _RUN_ID.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="Agent Run not found")
    return run_id


async def _get_run(run_id: str, identity: EndUserIdentity) -> AgentRunRecord:
    selected = _require_run_id(run_id)
    run = await asyncio.to_thread(
        agent_run_store.get_for_owner,
        selected,
        tenant_id=identity.tenant_id,
        owner_subject=identity.subject,
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Agent Run not found")
    return run


async def _snapshot(
    run: AgentRunRecord, identity: EndUserIdentity
) -> AgentRunSnapshot:
    last_event_id = await asyncio.to_thread(
        agent_run_store.last_event_id_for_owner,
        run.run_id,
        tenant_id=identity.tenant_id,
        owner_subject=identity.subject,
    )
    return AgentRunSnapshot(
        run_id=run.run_id,
        query=run.request.query,
        status=run.status,
        attempts=run.attempts,
        max_attempts=run.max_attempts,
        cancel_requested=run.cancel_requested_at is not None,
        last_event_id=last_event_id,
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        error_code=run.last_error_code,
        error="Agent Run failed" if run.status == "failed" else None,
        result=run.result,
        trace_id=run.trace_id,
    )


@router.post(
    "/agent/runs",
    response_model=AgentRunSubmission,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_agent_run(
    request: AgentQueryRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    traceparent: str | None = Header(default=None, alias="traceparent"),
    *,
    identity: EndUser,
) -> AgentRunSubmission:
    _enforce_persistence_gate(request)
    try:
        with trace_runtime.span(
            "agent.run.submit",
            traceparent=traceparent,
            kind=SpanKind.PRODUCER,
        ) as active_trace:
            run, created = await asyncio.to_thread(
                agent_run_store.create,
                request,
                idempotency_key,
                traceparent=active_trace.traceparent,
                tenant_id=identity.tenant_id,
                owner_subject=identity.subject,
            )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentRunAdmissionRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.detail(),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except AgentRunBudgetRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.detail(),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    base = f"/api/agent/runs/{run.run_id}"
    return AgentRunSubmission(
        run_id=run.run_id,
        status=run.status,
        created=created,
        status_url=base,
        events_url=f"{base}/events",
        cancel_url=f"{base}/cancel",
        trace_id=run.trace_id,
    )


@router.get(
    "/agent/runs/stats",
    dependencies=[Depends(require_role(Role.operator))],
)
async def get_agent_run_stats() -> dict[str, int | float]:
    return await asyncio.to_thread(agent_run_store.stats)


@router.get(
    "/agent/runs/health",
    dependencies=[Depends(require_role(Role.operator))],
    response_model=None,
)
async def get_agent_run_health(request: Request) -> dict[str, Any] | JSONResponse:
    stats = await asyncio.to_thread(agent_run_store.stats)
    in_process = getattr(request.app.state, "agent_run_worker", None)
    in_process_available = bool(getattr(in_process, "is_running", False))
    worker_records = await asyncio.to_thread(
        worker_status_store.list,
        limit=100,
        max_age_seconds=settings.worker_heartbeat_max_age_seconds,
    )
    agent_workers = [
        worker for worker in worker_records if "agent-run" in worker.capabilities
    ]
    healthy_standalone = next(
        (worker for worker in agent_workers if worker.healthy),
        None,
    )
    observed_standalone = healthy_standalone or next(iter(agent_workers), None)
    standalone_available = healthy_standalone is not None
    worker_available = in_process_available or standalone_available
    oldest_waiting = float(stats["oldest_waiting_seconds"])
    admission = AgentRunAdmissionPolicy.from_settings(settings)
    active = int(stats["active"])
    waiting = int(stats["waiting"])
    active_utilization = active / admission.max_active
    waiting_utilization = waiting / admission.max_waiting
    reasons: list[str] = []
    health_status = "ok"
    if not worker_available:
        health_status = "critical"
        reasons.append("no_healthy_agent_run_worker")
    if oldest_waiting >= settings.agent_run_queue_critical_seconds:
        health_status = "critical"
        reasons.append("queue_wait_critical")
    elif oldest_waiting >= settings.agent_run_queue_warn_seconds:
        if health_status == "ok":
            health_status = "degraded"
        reasons.append("queue_wait_warning")
    if admission.enabled:
        if active >= admission.max_active:
            health_status = "critical"
            reasons.append("active_capacity_full")
        elif active_utilization >= admission.warn_ratio:
            if health_status == "ok":
                health_status = "degraded"
            reasons.append("active_capacity_warning")
        if waiting >= admission.max_waiting:
            health_status = "critical"
            reasons.append("waiting_capacity_full")
        elif waiting_utilization >= admission.warn_ratio:
            if health_status == "ok":
                health_status = "degraded"
            reasons.append("waiting_capacity_warning")
    payload: dict[str, Any] = {
        "status": health_status,
        "worker_available": worker_available,
        "worker_instance_id": (
            observed_standalone.instance_id
            if observed_standalone is not None
            else None
        ),
        "worker_source": (
            "in_process"
            if in_process_available
            else "standalone"
            if standalone_available
            else None
        ),
        "reasons": reasons,
        "queue_warn_seconds": settings.agent_run_queue_warn_seconds,
        "queue_critical_seconds": settings.agent_run_queue_critical_seconds,
        "admission": {
            "enabled": admission.enabled,
            "max_active": admission.max_active,
            "max_waiting": admission.max_waiting,
            "warn_ratio": admission.warn_ratio,
            "active_utilization": round(active_utilization, 4),
            "waiting_utilization": round(waiting_utilization, 4),
        },
        "stats": stats,
    }
    if health_status == "critical":
        return JSONResponse(status_code=503, content=payload)
    return payload


@router.get("/agent/runs/{run_id}", response_model=AgentRunSnapshot)
async def get_agent_run(run_id: str, identity: EndUser) -> AgentRunSnapshot:
    return await _snapshot(await _get_run(run_id, identity), identity)


async def _replay_run_events(
    request: Request,
    run_id: str,
    after: int,
    identity: EndUserIdentity,
) -> AsyncIterator[str]:
    cursor = after
    last_keepalive = time.monotonic()
    while True:
        if await request.is_disconnected():
            return
        events = await asyncio.to_thread(
            agent_run_store.list_events_for_owner,
            run_id,
            tenant_id=identity.tenant_id,
            owner_subject=identity.subject,
            after=cursor,
            limit=500,
        )
        for event in events:
            cursor = event.event_id
            yield _stored_sse_event(event)
        run = await asyncio.to_thread(
            agent_run_store.get_for_owner,
            run_id,
            tenant_id=identity.tenant_id,
            owner_subject=identity.subject,
        )
        if run is None:
            return
        if run.is_terminal and cursor >= await asyncio.to_thread(
            agent_run_store.last_event_id_for_owner,
            run_id,
            tenant_id=identity.tenant_id,
            owner_subject=identity.subject,
        ):
            return
        now = time.monotonic()
        if now - last_keepalive >= settings.agent_run_sse_keepalive_seconds:
            yield ": keepalive\n\n"
            last_keepalive = now
        await asyncio.sleep(settings.agent_run_event_poll_seconds)


@router.get("/agent/runs/{run_id}/events")
async def stream_agent_run_events(
    request: Request,
    run_id: str,
    identity: EndUser,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    run = await _get_run(run_id, identity)
    cursor = after
    if last_event_id is not None:
        if not last_event_id.isdigit():
            raise HTTPException(status_code=422, detail="Last-Event-ID must be numeric")
        cursor = max(cursor, int(last_event_id))
    return StreamingResponse(
        _replay_run_events(request, run.run_id, cursor, identity),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/agent/runs/{run_id}/cancel", response_model=AgentRunSnapshot)
async def cancel_agent_run(run_id: str, identity: EndUser) -> AgentRunSnapshot:
    run = await _get_run(run_id, identity)
    updated = await asyncio.to_thread(
        agent_run_store.request_cancel_for_owner,
        run.run_id,
        tenant_id=identity.tenant_id,
        owner_subject=identity.subject,
    )
    return await _snapshot(updated, identity)


@router.post("/agent/query", response_model=AgentQueryResponse)
async def handle_agent_query(request: AgentQueryRequest) -> AgentQueryResponse:
    _enforce_persistence_gate(request)
    return await build_query_agent().run(request)


def _enforce_persistence_gate(request: AgentQueryRequest) -> None:
    if request.persist_discoveries and not settings.agent_allow_persistence:
        raise HTTPException(
            status_code=403,
            detail=(
                "Durable Agent graph updates are disabled. Set "
                "AGENT_ALLOW_PERSISTENCE=true only in an authorized environment."
            ),
        )


async def _stream_agent_query(request: AgentQueryRequest) -> AsyncIterator[str]:
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    async def emit(event: str, data: dict[str, Any]) -> None:
        await queue.put((event, data))

    async def execute() -> None:
        try:
            await build_query_agent().run(request, emit=emit)
        except Exception as exc:  # final safety net for stream clients
            logger.exception("agent_stream_failed", error=str(exc))
            await queue.put(("error", {"detail": str(exc)}))
        finally:
            await queue.put(None)

    task = asyncio.create_task(execute())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            event, data = item
            yield _sse_event(event, data)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@router.post("/agent/query/stream")
async def handle_agent_query_stream(request: AgentQueryRequest) -> StreamingResponse:
    _enforce_persistence_gate(request)
    return StreamingResponse(
        _stream_agent_query(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
