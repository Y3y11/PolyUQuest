"""Low-cardinality Prometheus snapshot over durable runtime stores."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from agent_rag.config import settings
from agent_rag.runs.admission import AgentRunAdmissionPolicy
from agent_rag.runs.store import AgentRunStore, agent_run_store
from agent_rag.telemetry.store import TelemetryStore, telemetry_store
from agent_rag.workers.status import WorkerStatusStore, worker_status_store

RUN_STATUSES = ("queued", "running", "retry", "completed", "failed", "cancelled")
WORKER_CAPABILITIES = ("agent-run", "index", "freshness")
WORKER_HEALTH = ("healthy", "unhealthy")
ADMISSION_OUTCOMES = (
    "accepted",
    "idempotent_replay",
    "rejected_active",
    "rejected_waiting",
    "rejected_budget",
)


class RuntimeMetricsUnavailableError(RuntimeError):
    """The first runtime metrics snapshot could not be constructed."""


@dataclass(frozen=True, slots=True)
class RuntimeMetricsSnapshot:
    created_monotonic: float
    run_stats: dict[str, int | float]
    limits: dict[str, int]
    workers: dict[tuple[str, str], int]
    telemetry: dict[str, Any]


class RuntimeMetricsSnapshotCache:
    """Single-flight TTL cache that can serve the last good snapshot on error."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        loader: Callable[[], RuntimeMetricsSnapshot],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= ttl_seconds <= 60:
            raise ValueError("metrics cache TTL must be between 1 and 60 seconds")
        self.ttl_seconds = float(ttl_seconds)
        self._loader = loader
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: RuntimeMetricsSnapshot | None = None
        self._refresh_success = 0
        self._refresh_error = 0

    def get(self) -> RuntimeMetricsSnapshot:
        now = self._clock()
        current = self._snapshot
        if current is not None and now - current.created_monotonic < self.ttl_seconds:
            return current
        with self._lock:
            now = self._clock()
            current = self._snapshot
            if current is not None and now - current.created_monotonic < self.ttl_seconds:
                return current
            try:
                loaded = self._loader()
            except Exception as exc:
                self._refresh_error += 1
                if current is not None:
                    return current
                raise RuntimeMetricsUnavailableError(
                    "runtime metrics snapshot is unavailable"
                ) from exc
            self._snapshot = loaded
            self._refresh_success += 1
            return loaded

    @property
    def refresh_counts(self) -> tuple[int, int]:
        return self._refresh_success, self._refresh_error

    def snapshot_age_seconds(self, snapshot: RuntimeMetricsSnapshot) -> float:
        return max(0.0, self._clock() - snapshot.created_monotonic)


def build_runtime_snapshot(
    *,
    run_store: AgentRunStore = agent_run_store,
    telemetry: TelemetryStore = telemetry_store,
    workers: WorkerStatusStore = worker_status_store,
    clock: Callable[[], float] = time.monotonic,
) -> RuntimeMetricsSnapshot:
    run_stats = run_store.stats()
    policy = AgentRunAdmissionPolicy.from_settings(settings)
    worker_counts = {
        (capability, health): 0
        for capability in WORKER_CAPABILITIES
        for health in WORKER_HEALTH
    }
    records = workers.list(
        limit=settings.runtime_metrics_worker_limit,
        max_age_seconds=settings.worker_heartbeat_max_age_seconds,
    )
    for record in records:
        health = "healthy" if record.healthy else "unhealthy"
        for capability in WORKER_CAPABILITIES:
            if capability in record.capabilities:
                worker_counts[(capability, health)] += 1
    telemetry_stats = telemetry.stats(
        run_type="agent_query",
        hours=settings.runtime_metrics_telemetry_window_hours,
    )
    return RuntimeMetricsSnapshot(
        created_monotonic=clock(),
        run_stats=run_stats,
        limits={"active": policy.max_active, "waiting": policy.max_waiting},
        workers=worker_counts,
        telemetry=telemetry_stats,
    )


class _RuntimeCollector:
    def __init__(
        self,
        snapshot: RuntimeMetricsSnapshot,
        *,
        snapshot_age_seconds: float,
        refresh_counts: tuple[int, int],
    ) -> None:
        self.snapshot = snapshot
        self.snapshot_age_seconds = snapshot_age_seconds
        self.refresh_counts = refresh_counts

    def collect(self):  # noqa: ANN201 - prometheus_client collector protocol
        stats = self.snapshot.run_stats
        runs = GaugeMetricFamily(
            "polyuquest_agent_runs",
            "Current durable Agent Runs by fixed status.",
            labels=["status"],
        )
        for status in RUN_STATUSES:
            runs.add_metric([status], float(stats.get(status, 0)))
        yield runs

        for name, key, help_text in (
            ("polyuquest_agent_run_active", "active", "Current active Agent Runs."),
            ("polyuquest_agent_run_waiting", "waiting", "Current waiting Agent Runs."),
        ):
            metric = GaugeMetricFamily(name, help_text)
            metric.add_metric([], float(stats.get(key, 0)))
            yield metric

        oldest = GaugeMetricFamily(
            "polyuquest_agent_run_oldest_seconds",
            "Age of the oldest Agent Run in a fixed state class.",
            labels=["state"],
        )
        oldest.add_metric(["waiting"], float(stats.get("oldest_waiting_seconds", 0)))
        oldest.add_metric(["running"], float(stats.get("oldest_running_seconds", 0)))
        yield oldest

        limits = GaugeMetricFamily(
            "polyuquest_agent_run_admission_limit",
            "Configured admission limit by fixed capacity kind.",
            labels=["kind"],
        )
        utilization = GaugeMetricFamily(
            "polyuquest_agent_run_admission_utilization_ratio",
            "Current admission capacity utilization ratio.",
            labels=["kind"],
        )
        for kind, stat_key in (("active", "active"), ("waiting", "waiting")):
            limit = self.snapshot.limits[kind]
            limits.add_metric([kind], float(limit))
            utilization.add_metric([kind], float(stats.get(stat_key, 0)) / limit)
        yield limits
        yield utilization

        admission = CounterMetricFamily(
            "polyuquest_agent_run_admission",
            "Cumulative Agent Run admission decisions.",
            labels=["outcome"],
        )
        admission_keys = {
            "accepted": "admission_accepted_total",
            "idempotent_replay": "admission_idempotent_replays_total",
            "rejected_active": "admission_rejected_active_total",
            "rejected_waiting": "admission_rejected_waiting_total",
            "rejected_budget": "admission_rejected_budget_total",
        }
        for outcome in ADMISSION_OUTCOMES:
            admission.add_metric([outcome], float(stats.get(admission_keys[outcome], 0)))
        yield admission

        attempts = CounterMetricFamily(
            "polyuquest_agent_run_attempts",
            "Cumulative Agent Run attempts by fixed reason class.",
            labels=["reason"],
        )
        attempts.add_metric(["all"], float(stats.get("attempt_events_total", 0)))
        attempts.add_metric(
            ["application_retry"], float(stats.get("application_retries", 0))
        )
        attempts.add_metric(["lease_reclaim"], float(stats.get("lease_reclaims", 0)))
        yield attempts

        worker_instances = GaugeMetricFamily(
            "polyuquest_worker_instances",
            "Worker instances grouped by bounded capability and health.",
            labels=["capability", "health"],
        )
        for capability in WORKER_CAPABILITIES:
            for health in WORKER_HEALTH:
                worker_instances.add_metric(
                    [capability, health],
                    float(self.snapshot.workers[(capability, health)]),
                )
        yield worker_instances

        telemetry_runs = GaugeMetricFamily(
            "polyuquest_telemetry_runs",
            "Agent telemetry runs in the configured observation window.",
            labels=["status"],
        )
        telemetry = self.snapshot.telemetry
        telemetry_runs.add_metric(
            ["running"], float(telemetry["runs"] - telemetry["terminal_runs"])
        )
        telemetry_runs.add_metric(["completed"], float(telemetry["completed"]))
        telemetry_runs.add_metric(["error"], float(telemetry["errors"]))
        yield telemetry_runs

        duration = GaugeMetricFamily(
            "polyuquest_telemetry_duration_milliseconds",
            "Agent query duration quantiles in the configured observation window.",
            labels=["quantile"],
        )
        for quantile in ("p50", "p95", "p99"):
            value = telemetry.get(f"{quantile}_duration_ms")
            duration.add_metric([quantile], float(value or 0))
        yield duration

        age = GaugeMetricFamily(
            "polyuquest_metrics_snapshot_age_seconds",
            "Age of the currently served runtime metrics snapshot.",
        )
        age.add_metric([], self.snapshot_age_seconds)
        yield age

        refresh = CounterMetricFamily(
            "polyuquest_metrics_snapshot_refresh",
            "Runtime metrics snapshot refresh outcomes.",
            labels=["outcome"],
        )
        refresh.add_metric(["success"], float(self.refresh_counts[0]))
        refresh.add_metric(["error"], float(self.refresh_counts[1]))
        yield refresh


class RuntimeMetricsService:
    def __init__(self, cache: RuntimeMetricsSnapshotCache) -> None:
        self.cache = cache

    def render(self) -> bytes:
        snapshot = self.cache.get()
        registry = CollectorRegistry(auto_describe=False)
        registry.register(
            _RuntimeCollector(
                snapshot,
                snapshot_age_seconds=self.cache.snapshot_age_seconds(snapshot),
                refresh_counts=self.cache.refresh_counts,
            )
        )
        return generate_latest(registry)


runtime_metrics_cache = RuntimeMetricsSnapshotCache(
    ttl_seconds=settings.runtime_metrics_cache_ttl_seconds,
    loader=build_runtime_snapshot,
)
runtime_metrics_service = RuntimeMetricsService(runtime_metrics_cache)
