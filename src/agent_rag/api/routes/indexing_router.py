"""Operational API for asynchronous indexing jobs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_rag.indexing.outbox import IndexJob, IndexJobStatus, index_outbox
from agent_rag.knowledge import FactVersion, fact_version_store
from agent_rag.knowledge.store import FactStatus
from agent_rag.quality import PageQualityDecision, page_quality_store
from agent_rag.quality.gate import QualityAction
from agent_rag.reconciliation import ReconciliationRun, ReconciliationRunDetail
from agent_rag.reconciliation.service import reconciliation_service
from agent_rag.security.auth import require_role
from agent_rag.security.models import Role
from agent_rag.versioning import PageVersion, page_version_store
from agent_rag.versioning.store import VersionStatus

router = APIRouter(dependencies=[Depends(require_role(Role.operator))])


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


@router.post(
    "/indexing/jobs/{job_id}/retry",
    response_model=IndexJob,
    dependencies=[Depends(require_role(Role.admin))],
)
def retry_index_job(job_id: str) -> IndexJob:
    if index_outbox.get(job_id) is None:
        raise HTTPException(status_code=404, detail="Index job not found")
    try:
        return index_outbox.retry(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/indexing/quality/decisions", response_model=list[PageQualityDecision])
def list_quality_decisions(
    action: QualityAction | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[PageQualityDecision]:
    return page_quality_store.list(action=action, limit=limit)


@router.get(
    "/indexing/quality/decisions/{decision_id}", response_model=PageQualityDecision
)
def get_quality_decision(decision_id: str) -> PageQualityDecision:
    decision = page_quality_store.get(decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="Page quality decision not found")
    return decision


@router.get("/indexing/quality/stats")
def get_quality_stats() -> dict[str, int | float]:
    return page_quality_store.stats()


@router.get("/indexing/versions", response_model=list[PageVersion])
def list_page_versions(
    source_url: str | None = None,
    status: VersionStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[PageVersion]:
    return page_version_store.list(
        source_url=source_url, status=status, limit=limit
    )


@router.get("/indexing/version-stats")
def get_page_version_stats() -> dict[str, int | float]:
    return page_version_store.stats()


@router.get("/indexing/facts", response_model=list[FactVersion])
def list_fact_versions(
    status: FactStatus | None = None,
    source_url: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[FactVersion]:
    return fact_version_store.list(
        status=status, source_url=source_url, limit=limit
    )


@router.get("/indexing/knowledge-stats")
def get_knowledge_stats() -> dict[str, int]:
    return fact_version_store.stats()


@router.post(
    "/indexing/reconciliation/runs", response_model=ReconciliationRunDetail
)
def create_reconciliation_run() -> ReconciliationRunDetail:
    return reconciliation_service.scan()


@router.get(
    "/indexing/reconciliation/runs", response_model=list[ReconciliationRun]
)
def list_reconciliation_runs(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ReconciliationRun]:
    return reconciliation_service.store.list(limit=limit)


@router.get("/indexing/reconciliation/stats")
def get_reconciliation_stats() -> dict[str, int | float | str | None]:
    return reconciliation_service.store.stats()


@router.get(
    "/indexing/reconciliation/runs/{run_id}",
    response_model=ReconciliationRunDetail,
)
def get_reconciliation_run(run_id: str) -> ReconciliationRunDetail:
    detail = reconciliation_service.store.get_detail(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Reconciliation run not found")
    return detail


@router.post(
    "/indexing/reconciliation/runs/{run_id}/execute",
    response_model=ReconciliationRunDetail,
    dependencies=[Depends(require_role(Role.admin))],
)
def execute_reconciliation_run(
    run_id: str,
    confirm: Annotated[bool, Query()] = False,
) -> ReconciliationRunDetail:
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Reconciliation execution requires confirm=true",
        )
    try:
        return reconciliation_service.execute(run_id, confirmed=True)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="Reconciliation run not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/indexing/facts/{fact_key}/history", response_model=list[FactVersion]
)
def get_fact_history(fact_key: str) -> list[FactVersion]:
    history = fact_version_store.history(fact_key)
    if not history:
        raise HTTPException(status_code=404, detail="Fact history not found")
    return history


@router.get("/indexing/versions/{version_id}", response_model=PageVersion)
def get_page_version(version_id: str) -> PageVersion:
    version = page_version_store.get(version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Page version not found")
    return version
