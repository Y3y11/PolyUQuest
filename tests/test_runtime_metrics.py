from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import ValidationError

from agent_rag.agent.schemas import AgentQueryRequest
from agent_rag.api.routes import metrics_router
from agent_rag.config import Settings
from agent_rag.metrics.runtime import (
    RuntimeMetricsService,
    RuntimeMetricsSnapshot,
    RuntimeMetricsSnapshotCache,
    RuntimeMetricsUnavailableError,
    build_runtime_snapshot,
)
from agent_rag.runs.admission import AgentRunAdmissionPolicy
from agent_rag.runs.store import AgentRunStore
from agent_rag.security.auth import ApiKeyAuthenticator
from agent_rag.security.credentials import hash_api_key
from agent_rag.telemetry.store import TelemetryStore
from agent_rag.workers.status import WorkerStatusStore


def _policy() -> AgentRunAdmissionPolicy:
    return AgentRunAdmissionPolicy(
        enabled=True,
        max_active=4,
        max_waiting=3,
        retry_after_seconds=1,
        warn_ratio=0.8,
        max_iterations=5,
        max_pages=10,
        max_seconds=120,
    )


def _request(query: str) -> AgentQueryRequest:
    return AgentQueryRequest(
        query=query,
        explore_web=False,
        persist_discoveries=False,
    )


def test_runtime_exposition_is_bounded_and_contains_no_business_content(
    tmp_path: Path,
) -> None:
    canary = "PRIVATE-QUESTION-CANARY-918273"
    run_store = AgentRunStore(tmp_path / "runs.sqlite3", admission_policy=_policy())
    run_store.create(_request(canary), "metrics-canary-000001")
    claimed = run_store.claim("private-worker-id", lease_seconds=30)
    assert claimed is not None
    telemetry = TelemetryStore(tmp_path / "telemetry.sqlite3")
    workers = WorkerStatusStore(tmp_path / "workers.sqlite3")
    workers.register(
        "private-worker-id",
        pid=123,
        capabilities=["agent-run", canary],
    )
    snapshot = build_runtime_snapshot(
        run_store=run_store,
        telemetry=telemetry,
        workers=workers,
    )
    cache = RuntimeMetricsSnapshotCache(ttl_seconds=5, loader=lambda: snapshot)
    body = RuntimeMetricsService(cache).render().decode()

    assert "polyuquest_agent_runs{status=\"running\"} 1.0" in body
    assert "polyuquest_agent_run_attempts_total{reason=\"all\"} 1.0" in body
    assert "polyuquest_worker_instances{capability=\"agent-run\",health=\"healthy\"}" in body
    assert canary not in body
    assert "private-worker-id" not in body
    assert "run-" not in body


def test_snapshot_cache_is_single_flight_and_serves_stale_on_error() -> None:
    calls = 0
    now = [10.0]
    should_fail = False

    def loader() -> RuntimeMetricsSnapshot:
        nonlocal calls
        calls += 1
        time.sleep(0.02)
        if should_fail:
            raise OSError("private path must not escape")
        return RuntimeMetricsSnapshot(
            created_monotonic=now[0],
            run_stats={},
            limits={"active": 1, "waiting": 1},
            workers={},
            telemetry={},
        )

    cache = RuntimeMetricsSnapshotCache(
        ttl_seconds=5,
        loader=loader,
        clock=lambda: now[0],
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(pool.map(lambda _: cache.get(), range(8)))
    assert calls == 1
    assert len({id(snapshot) for snapshot in snapshots}) == 1

    now[0] = 16.0
    should_fail = True
    assert cache.get() is snapshots[0]
    assert cache.refresh_counts == (1, 1)


def test_legacy_attempt_events_are_backfilled_once(tmp_path: Path) -> None:
    path = tmp_path / "legacy-runs.sqlite3"
    store = AgentRunStore(path, admission_policy=_policy())
    store.create(_request("legacy"), "metrics-legacy-000001")
    assert store.claim("legacy-worker", lease_seconds=30) is not None
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM agent_run_attempt_counters")
        connection.execute(
            "DELETE FROM agent_run_schema_metadata WHERE key='attempt_counters_v1'"
        )

    migrated = AgentRunStore(path, admission_policy=_policy())
    assert migrated.stats()["attempt_events_total"] == 1
    reopened = AgentRunStore(path, admission_policy=_policy())
    assert reopened.stats()["attempt_events_total"] == 1


def test_metrics_route_requires_operator_and_returns_prometheus_content_type() -> None:
    records = ",".join(
        (
            f"reader:reader:{hash_api_key('reader-secret')}",
            f"operator:operator:{hash_api_key('operator-secret')}",
            f"admin:admin:{hash_api_key('admin-secret')}",
        )
    )
    authenticator = ApiKeyAuthenticator("api_key", records)
    app = FastAPI()
    app.include_router(metrics_router.router, prefix="/api")
    with (
        patch("agent_rag.security.auth.security_authenticator", authenticator),
        patch.object(metrics_router.runtime_metrics_service, "render", return_value=b"safe 1\n"),
    ):
        client = TestClient(app)
        missing = client.get("/api/metrics")
        forbidden = client.get(
            "/api/metrics", headers={"X-API-Key": "reader-secret"}
        )
        allowed = client.get(
            "/api/metrics", headers={"X-API-Key": "operator-secret"}
        )

    assert missing.status_code == 401
    assert forbidden.status_code == 403
    assert allowed.status_code == 200
    assert allowed.text == "safe 1\n"
    assert allowed.headers["cache-control"] == "no-store"
    assert allowed.headers["content-type"] == CONTENT_TYPE_LATEST


def test_metrics_route_returns_safe_503_when_first_snapshot_fails() -> None:
    app = FastAPI()
    app.include_router(metrics_router.router, prefix="/api")
    with patch.object(
        metrics_router.runtime_metrics_service,
        "render",
        side_effect=RuntimeMetricsUnavailableError("private database path"),
    ):
        response = TestClient(app).get("/api/metrics")
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "runtime_metrics_unavailable"}
    assert "private database path" not in response.text


@pytest.mark.parametrize("ttl", [0.5, 61])
def test_metrics_cache_ttl_configuration_fails_fast(ttl: float) -> None:
    with pytest.raises(ValidationError, match="RUNTIME_METRICS_CACHE_TTL_SECONDS"):
        Settings(_env_file=None, runtime_metrics_cache_ttl_seconds=ttl)
