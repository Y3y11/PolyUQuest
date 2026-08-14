"""Operational API for cross-process Worker heartbeat visibility."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_rag.config import settings
from agent_rag.security.auth import require_role
from agent_rag.security.models import Role
from agent_rag.workers.status import WorkerStatus, worker_status_store

router = APIRouter(dependencies=[Depends(require_role(Role.operator))])


@router.get("/workers/status", response_model=list[WorkerStatus])
def list_worker_status(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[WorkerStatus]:
    return worker_status_store.list(
        limit=limit,
        max_age_seconds=settings.worker_heartbeat_max_age_seconds,
    )


@router.get("/workers/health", response_model=WorkerStatus)
def get_worker_health() -> WorkerStatus:
    latest = worker_status_store.latest(
        max_age_seconds=settings.worker_heartbeat_max_age_seconds
    )
    if latest is None:
        raise HTTPException(status_code=503, detail="No Worker heartbeat is registered")
    if not latest.healthy:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Latest Worker heartbeat is stale or stopped",
                "instance_id": latest.instance_id,
                "state": latest.state,
                "heartbeat_age_seconds": latest.heartbeat_age_seconds,
            },
        )
    return latest
