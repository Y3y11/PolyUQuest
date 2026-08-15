"""Deterministic benchmark for the Agent Run admission control plane."""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_rag.agent.schemas import AgentQueryRequest
from agent_rag.runs.admission import (
    AgentRunAdmissionPolicy,
    AgentRunAdmissionRejectedError,
)
from agent_rag.runs.models import AgentRunRecord
from agent_rag.runs.store import AgentRunStore

REPORT_SCHEMA = "polyuquest.capacity-benchmark.v1"


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    outcome: str
    latency_ms: float
    run_id: str | None = None


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _latency_summary(results: list[SubmissionResult], wall_seconds: float) -> dict[str, float]:
    values = [result.latency_ms for result in results]
    return {
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "p99_ms": _percentile(values, 0.99),
        "throughput_ops_per_second": round(len(results) / max(wall_seconds, 1e-9), 3),
    }


def _request() -> AgentQueryRequest:
    return AgentQueryRequest(
        query="capacity benchmark synthetic request",
        explore_web=False,
        persist_discoveries=False,
    )


def _submit(
    store: AgentRunStore,
    policy: AgentRunAdmissionPolicy,
    sequence: int,
) -> SubmissionResult:
    started = time.perf_counter()
    try:
        run, _ = store.create(
            _request(),
            f"benchmark-{sequence:010d}",
            admission_policy=policy,
        )
    except AgentRunAdmissionRejectedError as exc:
        outcome = f"rejected_{exc.reason.removesuffix('_limit')}"
        return SubmissionResult(
            outcome=outcome,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
    return SubmissionResult(
        outcome="accepted",
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
        run_id=run.run_id,
    )


def _concurrent_submit(
    store: AgentRunStore,
    policy: AgentRunAdmissionPolicy,
    *,
    count: int,
    concurrency: int,
    sequence_start: int,
) -> tuple[list[SubmissionResult], float]:
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(
            pool.map(
                lambda sequence: _submit(store, policy, sequence),
                range(sequence_start, sequence_start + count),
            )
        )
    return results, time.perf_counter() - started


def _count(results: list[SubmissionResult]) -> dict[str, int]:
    counts = {"accepted": 0, "rejected_active": 0, "rejected_waiting": 0}
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
    return counts


def run_capacity_benchmark(
    *,
    requests: int = 32,
    concurrency: int = 16,
    max_active: int = 12,
    max_waiting: int = 8,
    max_p95_ms: float = 250.0,
) -> dict[str, Any]:
    if requests <= max_waiting:
        raise ValueError("requests must exceed max_waiting to exercise rejection")
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    policy = AgentRunAdmissionPolicy(
        enabled=True,
        max_active=max_active,
        max_waiting=max_waiting,
        retry_after_seconds=1,
        warn_ratio=0.8,
        max_iterations=5,
        max_pages=10,
        max_seconds=120,
    )
    sequence = 0
    all_results: list[SubmissionResult] = []
    claimed: list[AgentRunRecord] = []
    assertions: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="polyuquest-capacity-") as directory:
        store = AgentRunStore(Path(directory) / "benchmark.sqlite3", admission_policy=policy)

        burst, burst_wall = _concurrent_submit(
            store,
            policy,
            count=requests,
            concurrency=concurrency,
            sequence_start=sequence,
        )
        sequence += requests
        all_results.extend(burst)
        burst_counts = _count(burst)
        burst_stats = store.stats()
        for _ in range(burst_counts["accepted"]):
            worker_id = f"benchmark-worker-{len(claimed)}"
            claimed_run = store.claim(worker_id, lease_seconds=300)
            if claimed_run is None:
                raise RuntimeError("burst acceptance could not be claimed")
            claimed.append(claimed_run)

        while int(store.stats()["active"]) < max_active:
            remaining = max_active - int(store.stats()["active"])
            batch_size = min(max_waiting, remaining)
            queued, wall = _concurrent_submit(
                store,
                policy,
                count=batch_size,
                concurrency=min(concurrency, batch_size),
                sequence_start=sequence,
            )
            sequence += batch_size
            all_results.extend(queued)
            burst_wall += wall
            for _ in range(batch_size):
                worker_id = f"benchmark-worker-{len(claimed)}"
                claimed_run = store.claim(
                    worker_id,
                    lease_seconds=300,
                )
                if claimed_run is None:
                    raise RuntimeError("capacity fill could not claim an accepted Run")
                claimed.append(claimed_run)

        active_probes, reject_wall = _concurrent_submit(
            store,
            policy,
            count=requests,
            concurrency=concurrency,
            sequence_start=sequence,
        )
        sequence += requests
        all_results.extend(active_probes)
        active_probe_counts = _count(active_probes)
        full_stats = store.stats()

        release_started = time.perf_counter()
        for run in claimed:
            store.request_cancel(run.run_id)
            store.finish_cancelled(run.run_id, str(run.worker_id), run.attempts)
        release_wall = time.perf_counter() - release_started
        released_stats = store.stats()

        recovery, recovery_wall = _concurrent_submit(
            store,
            policy,
            count=max_waiting,
            concurrency=min(concurrency, max_waiting),
            sequence_start=sequence,
        )
        all_results.extend(recovery)
        recovery_counts = _count(recovery)
        for result in recovery:
            if result.run_id is not None:
                store.request_cancel(result.run_id)
        final_stats = store.stats()

        assertions = {
            "burst_respected_waiting_limit": (
                int(burst_stats["waiting"]) <= max_waiting
                and burst_counts["accepted"] <= max_waiting
            ),
            "active_capacity_reached_exactly": int(full_stats["active"]) == max_active,
            "active_probe_fully_rejected": (
                active_probe_counts["rejected_active"] == requests
            ),
            "release_cleared_capacity": (
                int(released_stats["active"]) == 0
                and int(released_stats["waiting"]) == 0
            ),
            "recovery_restored_acceptance": recovery_counts["accepted"] == max_waiting,
            "cleanup_cleared_capacity": (
                int(final_stats["active"]) == 0 and int(final_stats["waiting"]) == 0
            ),
        }

        total_wall = burst_wall + reject_wall + release_wall + recovery_wall
        latency = _latency_summary(all_results, total_wall)
        assertions["p95_within_budget"] = latency["p95_ms"] <= max_p95_ms
        assertions["persisted_acceptance_matches"] = int(
            final_stats["admission_accepted_total"]
        ) == sum(result.outcome == "accepted" for result in all_results)
        assertions["persisted_rejections_match"] = (
            int(final_stats["admission_rejected_active_total"])
            == sum(result.outcome == "rejected_active" for result in all_results)
            and int(final_stats["admission_rejected_waiting_total"])
            == sum(result.outcome == "rejected_waiting" for result in all_results)
        )

    return {
        "schema": REPORT_SCHEMA,
        "status": "passed" if all(assertions.values()) else "failed",
        "scope": "sqlite_agent_run_admission_control_plane",
        "config": {
            "requests": requests,
            "concurrency": concurrency,
            "max_active": max_active,
            "max_waiting": max_waiting,
            "max_p95_ms": max_p95_ms,
        },
        "phases": {
            "burst": {**burst_counts, **_latency_summary(burst, burst_wall)},
            "active_rejection": {
                **active_probe_counts,
                **_latency_summary(active_probes, reject_wall),
            },
            "release": {
                "released": len(claimed),
                "duration_ms": round(release_wall * 1000, 3),
            },
            "recovery": {
                **recovery_counts,
                **_latency_summary(recovery, recovery_wall),
            },
        },
        "overall": latency,
        "store_counters": {
            key: int(final_stats[key])
            for key in (
                "admission_accepted_total",
                "admission_rejected_active_total",
                "admission_rejected_waiting_total",
                "attempt_events_total",
            )
        },
        "assertions": assertions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark the local Agent Run admission control plane"
    )
    parser.add_argument("--requests", type=int, default=32)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--max-active", type=int, default=12)
    parser.add_argument("--max-waiting", type=int, default=8)
    parser.add_argument("--max-p95-ms", type=float, default=250.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_capacity_benchmark(
        requests=args.requests,
        concurrency=args.concurrency,
        max_active=args.max_active,
        max_waiting=args.max_waiting,
        max_p95_ms=args.max_p95_ms,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
