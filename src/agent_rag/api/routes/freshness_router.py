"""Operational API for adaptive page lifecycle scheduling."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from agent_rag.freshness import (
    LifecycleStatus,
    PageLifecycleTarget,
    page_lifecycle_store,
)

router = APIRouter()


@router.get("/freshness/targets", response_model=list[PageLifecycleTarget])
def list_freshness_targets(
    status: LifecycleStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[PageLifecycleTarget]:
    return page_lifecycle_store.list(status=status, limit=limit)


@router.get("/freshness/target", response_model=PageLifecycleTarget)
def get_freshness_target(url: Annotated[str, Query(min_length=1)]) -> PageLifecycleTarget:
    target = page_lifecycle_store.get(url)
    if target is None:
        raise HTTPException(status_code=404, detail="Lifecycle target not found")
    return target


@router.get("/freshness/stats")
def get_freshness_stats() -> dict[str, int | float]:
    return page_lifecycle_store.stats()


@router.post("/freshness/refresh-now", response_model=PageLifecycleTarget)
def refresh_target_now(
    url: Annotated[str, Query(min_length=1)],
) -> PageLifecycleTarget:
    try:
        return page_lifecycle_store.refresh_now(url)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Lifecycle target not found") from exc


@router.post("/freshness/pause", response_model=PageLifecycleTarget)
def pause_freshness_target(
    url: Annotated[str, Query(min_length=1)],
) -> PageLifecycleTarget:
    try:
        return page_lifecycle_store.pause(url)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Lifecycle target not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/freshness/resume", response_model=PageLifecycleTarget)
def resume_freshness_target(
    url: Annotated[str, Query(min_length=1)],
) -> PageLifecycleTarget:
    try:
        return page_lifecycle_store.resume(url)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Lifecycle target not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
