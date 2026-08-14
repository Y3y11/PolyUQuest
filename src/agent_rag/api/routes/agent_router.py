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
from fastapi.responses import StreamingResponse

from agent_rag.agent.schemas import (
    AgentQueryRequest,
    AgentQueryResponse,
    AgentRunSnapshot,
    AgentRunSubmission,
)
from agent_rag.config import settings
from agent_rag.runs.models import AgentRunEvent, AgentRunRecord
from agent_rag.runs.store import (
    IdempotencyConflictError,
    agent_run_store,
)
from agent_rag.runtime import build_query_agent
from agent_rag.security.auth import require_role
from agent_rag.security.models import Role

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


async def _get_run(run_id: str) -> AgentRunRecord:
    selected = _require_run_id(run_id)
    run = await asyncio.to_thread(agent_run_store.get, selected)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent Run not found")
    return run


async def _snapshot(run: AgentRunRecord) -> AgentRunSnapshot:
    last_event_id = await asyncio.to_thread(
        agent_run_store.last_event_id, run.run_id
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
    )


@router.post(
    "/agent/runs",
    response_model=AgentRunSubmission,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_agent_run(
    request: AgentQueryRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> AgentRunSubmission:
    _enforce_persistence_gate(request)
    try:
        run, created = await asyncio.to_thread(
            agent_run_store.create, request, idempotency_key
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    )


@router.get("/agent/runs/stats")
async def get_agent_run_stats() -> dict[str, int | float]:
    return await asyncio.to_thread(agent_run_store.stats)


@router.get("/agent/runs/{run_id}", response_model=AgentRunSnapshot)
async def get_agent_run(run_id: str) -> AgentRunSnapshot:
    return await _snapshot(await _get_run(run_id))


async def _replay_run_events(
    request: Request,
    run_id: str,
    after: int,
) -> AsyncIterator[str]:
    cursor = after
    last_keepalive = time.monotonic()
    while True:
        if await request.is_disconnected():
            return
        events = await asyncio.to_thread(
            agent_run_store.list_events, run_id, after=cursor, limit=500
        )
        for event in events:
            cursor = event.event_id
            yield _stored_sse_event(event)
        run = await asyncio.to_thread(agent_run_store.get, run_id)
        if run is None:
            return
        if run.is_terminal and cursor >= await asyncio.to_thread(
            agent_run_store.last_event_id, run_id
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
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    run = await _get_run(run_id)
    cursor = after
    if last_event_id is not None:
        if not last_event_id.isdigit():
            raise HTTPException(status_code=422, detail="Last-Event-ID must be numeric")
        cursor = max(cursor, int(last_event_id))
    return StreamingResponse(
        _replay_run_events(request, run.run_id, cursor),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/agent/runs/{run_id}/cancel", response_model=AgentRunSnapshot)
async def cancel_agent_run(run_id: str) -> AgentRunSnapshot:
    run = await _get_run(run_id)
    updated = await asyncio.to_thread(agent_run_store.request_cancel, run.run_id)
    return await _snapshot(updated)


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
