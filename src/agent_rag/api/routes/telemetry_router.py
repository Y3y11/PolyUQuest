"""Privacy-safe operational telemetry endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_rag.config import observability_config
from agent_rag.security.auth import require_role
from agent_rag.security.models import Role
from agent_rag.telemetry.models import RunTelemetry, RunTelemetryDetail
from agent_rag.telemetry.store import telemetry_store

router = APIRouter(dependencies=[Depends(require_role(Role.operator))])


@router.get("/telemetry/runs", response_model=list[RunTelemetry])
def list_runs(
    run_type: str = "",
    status: str = "",
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[RunTelemetry]:
    return telemetry_store.list(run_type=run_type, status=status, limit=limit)


@router.get("/telemetry/runs/{run_id}", response_model=RunTelemetryDetail)
def get_run(run_id: str) -> RunTelemetryDetail:
    detail = telemetry_store.get(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Telemetry run not found")
    return detail


@router.get("/telemetry/stats")
def get_stats(run_type: str = "agent_query", hours: int = Query(24, ge=1, le=2160)):
    return telemetry_store.stats(run_type=run_type, hours=hours)


@router.get("/telemetry/slo")
def get_slo(run_type: str = "agent_query", hours: int | None = None) -> dict[str, Any]:
    slo_config = observability_config.get("slo", {})
    target = slo_config.get(run_type)
    if not isinstance(target, dict):
        raise HTTPException(status_code=400, detail="No SLO configured for run type")
    selected_hours = hours or int(slo_config.get("window_hours", 24))
    stats = telemetry_store.stats(run_type=run_type, hours=selected_hours)
    min_samples = int(slo_config.get("min_samples", 20))
    checks: dict[str, bool | None] = {
        "success_rate": (
            stats["success_rate"] >= float(target["success_rate_min"])
            if stats["success_rate"] is not None
            else None
        ),
        "p95_duration_ms": (
            stats["p95_duration_ms"] <= int(target["p95_duration_ms_max"])
            if stats["p95_duration_ms"] is not None
            else None
        ),
    }
    if "avg_billable_tokens_max" in target:
        checks["avg_billable_tokens"] = (
            stats["avg_billable_tokens"] <= float(target["avg_billable_tokens_max"])
            if stats["avg_billable_tokens"] is not None
            else None
        )
    enough_data = stats["terminal_runs"] >= min_samples
    return {
        "run_type": run_type,
        "window_hours": selected_hours,
        "status": (
            "insufficient_data"
            if not enough_data
            else "pass"
            if all(value is True for value in checks.values())
            else "fail"
        ),
        "min_samples": min_samples,
        "checks": checks,
        "targets": target,
        "stats": stats,
    }
