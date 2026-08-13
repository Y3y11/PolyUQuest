"""Operational API for asynchronous indexing jobs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from agent_rag.indexing.outbox import IndexJob, IndexJobStatus, index_outbox

router = APIRouter()


@router.get("/indexing/jobs", response_model=list[IndexJob])
def list_index_jobs(
    status: IndexJobStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[IndexJob]:
    return index_outbox.list(status=status, limit=limit)


@router.get("/indexing/jobs/{job_id}", response_model=IndexJob)
def get_index_job(job_id: str) -> IndexJob:
    job = index_outbox.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Index job not found")
    return job


@router.get("/indexing/stats")
def get_indexing_stats() -> dict[str, int | float]:
    return index_outbox.stats()


@router.post("/indexing/jobs/{job_id}/retry", response_model=IndexJob)
def retry_index_job(job_id: str) -> IndexJob:
    if index_outbox.get(job_id) is None:
        raise HTTPException(status_code=404, detail="Index job not found")
    try:
        return index_outbox.retry(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
