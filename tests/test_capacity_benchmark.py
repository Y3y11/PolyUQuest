from __future__ import annotations

import json

import pytest

from agent_rag.runs.benchmark import REPORT_SCHEMA, run_capacity_benchmark


def test_capacity_benchmark_proves_rejection_release_and_recovery() -> None:
    report = run_capacity_benchmark(
        requests=10,
        concurrency=8,
        max_active=5,
        max_waiting=3,
        max_p95_ms=5000,
    )

    assert report["schema"] == REPORT_SCHEMA
    assert report["status"] == "passed"
    assert report["phases"]["active_rejection"]["rejected_active"] == 10
    assert report["phases"]["recovery"]["accepted"] == 3
    assert all(report["assertions"].values())
    serialized = json.dumps(report)
    assert "capacity benchmark synthetic request" not in serialized
    assert "benchmark-" not in serialized
    assert "sqlite3" not in serialized


def test_capacity_benchmark_rejects_non_exercising_configuration() -> None:
    with pytest.raises(ValueError, match="exceed max_waiting"):
        run_capacity_benchmark(requests=3, max_active=5, max_waiting=3)
