"""SQLite store for request, worker, and LLM telemetry."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_rag.config import settings
from agent_rag.telemetry.models import RunTelemetry, RunTelemetryDetail, TelemetrySpan


class TelemetryStore:
    def __init__(self, db_path: str | Path | None = None):
        path = Path(db_path or settings.agent_ledger_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Telemetry is fail-open: a contended ledger must not hold the answer
        # path for the five-second timeout used by business-critical ledgers.
        connection = sqlite3.connect(self.path, timeout=0.25)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=250")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS telemetry_runs (
                    run_id TEXT PRIMARY KEY,
                    run_type TEXT NOT NULL,
                    root_run_id TEXT NOT NULL,
                    parent_run_id TEXT,
                    status TEXT NOT NULL,
                    response_status TEXT NOT NULL,
                    route_mode TEXT NOT NULL,
                    stop_reason TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    query_length INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    duration_ms INTEGER NOT NULL,
                    logical_input_tokens INTEGER NOT NULL,
                    logical_output_tokens INTEGER NOT NULL,
                    billable_input_tokens INTEGER NOT NULL,
                    billable_output_tokens INTEGER NOT NULL,
                    llm_calls INTEGER NOT NULL,
                    cache_hits INTEGER NOT NULL,
                    evidence_count INTEGER NOT NULL,
                    pages_fetched INTEGER NOT NULL,
                    fetch_failures INTEGER NOT NULL,
                    indexing_jobs_queued INTEGER NOT NULL,
                    error_category TEXT NOT NULL,
                    error TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    code_version TEXT NOT NULL,
                    attributes_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_telemetry_runs_started
                ON telemetry_runs(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_telemetry_runs_root
                ON telemetry_runs(root_run_id, started_at);
                CREATE TABLE IF NOT EXISTS telemetry_spans (
                    span_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    logical_input_tokens INTEGER NOT NULL,
                    logical_output_tokens INTEGER NOT NULL,
                    billable_input_tokens INTEGER NOT NULL,
                    billable_output_tokens INTEGER NOT NULL,
                    llm_calls INTEGER NOT NULL,
                    cache_hit INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    error_category TEXT NOT NULL,
                    attributes_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES telemetry_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_telemetry_spans_run
                ON telemetry_spans(run_id, started_at);
                """
            )

    def start(self, run: RunTelemetry) -> RunTelemetry:
        values = run.model_dump()
        attributes = json.dumps(values.pop("attributes"), ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO telemetry_runs({','.join(values)},attributes_json) "
                f"VALUES ({','.join('?' for _ in range(len(values) + 1))})",
                (*values.values(), attributes),
            )
        return run

    def finish(self, run_id: str, **updates: Any) -> RunTelemetry:
        allowed = {
            "status",
            "response_status",
            "route_mode",
            "stop_reason",
            "completed_at",
            "duration_ms",
            "evidence_count",
            "pages_fetched",
            "fetch_failures",
            "indexing_jobs_queued",
            "error_category",
            "error",
            "attributes",
        }
        payload = {key: value for key, value in updates.items() if key in allowed}
        if "attributes" in payload:
            payload["attributes_json"] = json.dumps(payload.pop("attributes"), ensure_ascii=False)
        if payload:
            assignments = ",".join(f"{key}=?" for key in payload)
            with self._connect() as connection:
                connection.execute(
                    f"UPDATE telemetry_runs SET {assignments} WHERE run_id=?",
                    (*payload.values(), run_id),
                )
        detail = self.get(run_id)
        if detail is None:
            raise KeyError(run_id)
        return detail.run

    def append_span(self, span: TelemetrySpan) -> TelemetrySpan:
        values = span.model_dump()
        attributes = json.dumps(values.pop("attributes"), ensure_ascii=False)
        values["cache_hit"] = int(values["cache_hit"])
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO telemetry_spans({','.join(values)},attributes_json) "
                f"VALUES ({','.join('?' for _ in range(len(values) + 1))})",
                (*values.values(), attributes),
            )
            connection.execute(
                """UPDATE telemetry_runs SET
                    logical_input_tokens=logical_input_tokens+?,
                    logical_output_tokens=logical_output_tokens+?,
                    billable_input_tokens=billable_input_tokens+?,
                    billable_output_tokens=billable_output_tokens+?,
                    llm_calls=llm_calls+?, cache_hits=cache_hits+?
                   WHERE run_id=?""",
                (
                    span.logical_input_tokens,
                    span.logical_output_tokens,
                    span.billable_input_tokens,
                    span.billable_output_tokens,
                    span.llm_calls,
                    int(span.cache_hit),
                    span.run_id,
                ),
            )
        return span

    def get(self, run_id: str) -> RunTelemetryDetail | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM telemetry_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            spans = connection.execute(
                "SELECT * FROM telemetry_spans WHERE run_id=? ORDER BY started_at, span_id",
                (run_id,),
            ).fetchall()
        return RunTelemetryDetail(
            run=self._run_from_row(row),
            spans=[self._span_from_row(item) for item in spans],
        )

    def list(self, *, run_type: str = "", status: str = "", limit: int = 100) -> list[RunTelemetry]:
        where: list[str] = []
        params: list[Any] = []
        if run_type:
            where.append("run_type=?")
            params.append(run_type)
        if status:
            where.append("status=?")
            params.append(status)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM telemetry_runs{clause} ORDER BY started_at DESC LIMIT ?",
                (*params, min(max(limit, 1), 1000)),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def stats(self, *, run_type: str = "agent_query", hours: int = 24) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM telemetry_runs WHERE run_type=? AND started_at>=?",
                (run_type, since),
            ).fetchall()
        terminal = [row for row in rows if row["status"] != "running"]
        durations = [int(row["duration_ms"]) for row in terminal]
        completed = sum(row["status"] == "completed" for row in terminal)
        errors = sum(row["status"] == "error" for row in terminal)
        return {
            "run_type": run_type,
            "window_hours": hours,
            "runs": len(rows),
            "terminal_runs": len(terminal),
            "completed": completed,
            "errors": errors,
            "success_rate": (round(completed / len(terminal), 4) if terminal else None),
            "p50_duration_ms": self._percentile(durations, 0.50),
            "p95_duration_ms": self._percentile(durations, 0.95),
            "p99_duration_ms": self._percentile(durations, 0.99),
            "min_duration_ms": min(durations) if durations else None,
            "max_duration_ms": max(durations) if durations else None,
            "avg_duration_ms": (round(sum(durations) / len(durations), 2) if durations else None),
            "avg_billable_tokens": (
                round(
                    sum(
                        row["billable_input_tokens"] + row["billable_output_tokens"]
                        for row in terminal
                    )
                    / len(terminal),
                    2,
                )
                if terminal
                else None
            ),
            "cache_hit_rate": (
                round(
                    sum(row["cache_hits"] for row in terminal)
                    / max(sum(row["llm_calls"] for row in terminal), 1),
                    4,
                )
                if terminal
                else None
            ),
        }

    @staticmethod
    def _percentile(values: list[int], quantile: float) -> int | None:
        if not values:
            return None
        ordered = sorted(values)
        return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunTelemetry:
        data = dict(row)
        data["attributes"] = json.loads(data.pop("attributes_json") or "{}")
        return RunTelemetry.model_validate(data)

    @staticmethod
    def _span_from_row(row: sqlite3.Row) -> TelemetrySpan:
        data = dict(row)
        data["attributes"] = json.loads(data.pop("attributes_json") or "{}")
        data["cache_hit"] = bool(data["cache_hit"])
        return TelemetrySpan.model_validate(data)


telemetry_store = TelemetryStore()
