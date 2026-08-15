"""Protected Prometheus runtime metrics endpoint."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST

from agent_rag.config import settings
from agent_rag.metrics.runtime import (
    RuntimeMetricsUnavailableError,
    runtime_metrics_service,
)
from agent_rag.security.auth import require_role
from agent_rag.security.models import Role

router = APIRouter(dependencies=[Depends(require_role(Role.operator))])


@router.get("/metrics", response_class=Response)
async def get_runtime_metrics() -> Response:
    if not settings.runtime_metrics_enabled:
        raise HTTPException(status_code=404, detail={"code": "runtime_metrics_disabled"})
    try:
        body = await asyncio.to_thread(runtime_metrics_service.render)
    except RuntimeMetricsUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "runtime_metrics_unavailable"},
        ) from exc
    return Response(
        content=body,
        media_type=CONTENT_TYPE_LATEST,
        headers={"Cache-Control": "no-store"},
    )
