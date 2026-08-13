"""HTTP and SSE endpoints for the bounded query-driven retrieval agent."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from agent_rag.agent.orchestrator import QueryDrivenAgent
from agent_rag.agent.schemas import AgentQueryRequest, AgentQueryResponse
from agent_rag.config import settings

logger = structlog.get_logger(__name__)
router = APIRouter()


def _sse_event(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/agent/query", response_model=AgentQueryResponse)
async def handle_agent_query(request: AgentQueryRequest) -> AgentQueryResponse:
    _enforce_persistence_gate(request)
    return await QueryDrivenAgent().run(request)


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
            await QueryDrivenAgent().run(request, emit=emit)
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
