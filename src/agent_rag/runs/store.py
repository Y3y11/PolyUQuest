"""SQLite WAL store for durable Agent Runs and replayable events."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_rag.agent.schemas import AgentQueryRequest, AgentQueryResponse
from agent_rag.config import settings
from agent_rag.runs.models import (
    TERMINAL_AGENT_RUN_STATUSES,
    AgentRunEvent,
    AgentRunRecord,
    AgentRunStatus,
)

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_MAX_EVENT_BYTES = 1_048_576


class IdempotencyConflictError(ValueError):
    """The same idempotency key was reused for a different request."""


class AgentRunLeaseLostError(ValueError):
    """A stale worker no longer owns the Run lease."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


def _json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_EVENT_BYTES:
        raise ValueError("Agent Run event exceeds the 1 MiB payload limit")
    return encoded


def _request_payload(request: AgentQueryRequest) -> tuple[str, str]:
    value = request.model_dump(mode="json")
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AgentRunStore:
    def __init__(self, db_path: str | Path | None = None):
        path = Path(db_path or settings.agent_run_store_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._ensure_schema()
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            connection = sqlite3.connect(self.path, timeout=5.0)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS agent_runs (
                        run_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        request_fingerprint TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        result_json TEXT,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL,
                        available_at TEXT NOT NULL,
                        lease_until TEXT,
                        worker_id TEXT,
                        cancel_requested_at TEXT,
                        last_error_code TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_agent_runs_claim
                    ON agent_runs(status, available_at, lease_until, created_at);
                    CREATE INDEX IF NOT EXISTS idx_agent_runs_terminal
                    ON agent_runs(status, completed_at);

                    CREATE TABLE IF NOT EXISTS agent_run_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(run_id) REFERENCES agent_runs(run_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_agent_run_events_replay
                    ON agent_run_events(run_id, event_id);
                    """
                )
                connection.commit()
            finally:
                connection.close()
            self._initialized = True

    @staticmethod
    def validate_idempotency_key(value: str) -> str:
        selected = value.strip()
        if not _IDEMPOTENCY_KEY.fullmatch(selected):
            raise ValueError(
                "Idempotency-Key must be 16-128 URL-safe characters"
            )
        return selected

    @staticmethod
    def _append_event_tx(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        attempt: int,
        event_type: str,
        payload: dict[str, Any],
        created_at: str | None = None,
    ) -> AgentRunEvent:
        now = created_at or _iso()
        payload_json = _json(payload)
        cursor = connection.execute(
            """INSERT INTO agent_run_events(
                run_id, attempt, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)""",
            (run_id, attempt, event_type, payload_json, now),
        )
        return AgentRunEvent(
            event_id=int(cursor.lastrowid),
            run_id=run_id,
            attempt=attempt,
            event_type=event_type,
            payload_json=payload_json,
            created_at=now,
        )

    def create(
        self,
        request: AgentQueryRequest,
        idempotency_key: str,
        *,
        max_attempts: int | None = None,
    ) -> tuple[AgentRunRecord, bool]:
        key = self.validate_idempotency_key(idempotency_key)
        request_json, fingerprint = _request_payload(request)
        now = _iso()
        run = AgentRunRecord(
            run_id=f"run-{uuid.uuid4().hex}",
            idempotency_key=key,
            request_fingerprint=fingerprint,
            request_json=request_json,
            max_attempts=max_attempts or settings.agent_run_max_attempts,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM agent_runs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                current = self._from_run_row(existing)
                if current.request_fingerprint != fingerprint:
                    raise IdempotencyConflictError(
                        "Idempotency-Key was already used for another request"
                    )
                return current, False
            connection.execute(
                """INSERT INTO agent_runs(
                    run_id, idempotency_key, request_fingerprint, request_json,
                    result_json, status, attempts, max_attempts, available_at,
                    lease_until, worker_id, cancel_requested_at,
                    last_error_code, last_error, created_at, updated_at,
                    started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.run_id,
                    run.idempotency_key,
                    run.request_fingerprint,
                    run.request_json,
                    run.result_json,
                    run.status,
                    run.attempts,
                    run.max_attempts,
                    run.available_at,
                    run.lease_until,
                    run.worker_id,
                    run.cancel_requested_at,
                    run.last_error_code,
                    run.last_error,
                    run.created_at,
                    run.updated_at,
                    run.started_at,
                    run.completed_at,
                ),
            )
            self._append_event_tx(
                connection,
                run_id=run.run_id,
                attempt=0,
                event_type="run_queued",
                payload={"run_id": run.run_id, "status": "queued"},
                created_at=now,
            )
        return run, True

    def get(self, run_id: str) -> AgentRunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._from_run_row(row) if row is not None else None

    def list_events(
        self, run_id: str, *, after: int = 0, limit: int = 500
    ) -> list[AgentRunEvent]:
        if after < 0:
            raise ValueError("after must be non-negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM agent_run_events
                WHERE run_id=? AND event_id>? ORDER BY event_id ASC LIMIT ?""",
                (run_id, after, limit),
            ).fetchall()
        return [self._from_event_row(row) for row in rows]

    def last_event_id(self, run_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT max(event_id) AS event_id FROM agent_run_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return int(row["event_id"] or 0)

    def claim(self, worker_id: str, *, lease_seconds: int) -> AgentRunRecord | None:
        now = _utc_now()
        now_iso = _iso(now)
        lease_until = _iso(now + timedelta(seconds=lease_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exhausted = connection.execute(
                """SELECT * FROM agent_runs
                WHERE status='running' AND lease_until IS NOT NULL
                AND lease_until<=? AND attempts>=max_attempts""",
                (now_iso,),
            ).fetchall()
            for row in exhausted:
                connection.execute(
                    """UPDATE agent_runs SET status='failed', lease_until=NULL,
                    worker_id=NULL, last_error_code='lease_expired',
                    last_error='Run lease expired after the final attempt',
                    completed_at=?, updated_at=? WHERE run_id=? AND updated_at=?""",
                    (now_iso, now_iso, row["run_id"], row["updated_at"]),
                )
                self._append_event_tx(
                    connection,
                    run_id=row["run_id"],
                    attempt=row["attempts"],
                    event_type="error",
                    payload={"detail": "Agent Run failed after lease expiry"},
                    created_at=now_iso,
                )
            row = connection.execute(
                """SELECT * FROM agent_runs
                WHERE (
                    status IN ('queued', 'retry') AND available_at<=?
                ) OR (
                    status='running' AND lease_until IS NOT NULL
                    AND lease_until<=? AND attempts<max_attempts
                )
                ORDER BY available_at ASC, created_at ASC LIMIT 1""",
                (now_iso, now_iso),
            ).fetchone()
            if row is None:
                return None
            attempt = int(row["attempts"]) + 1
            cursor = connection.execute(
                """UPDATE agent_runs SET status='running', attempts=?, worker_id=?,
                lease_until=?, cancel_requested_at=NULL, last_error_code=NULL,
                last_error=NULL, started_at=coalesce(started_at, ?), updated_at=?
                WHERE run_id=? AND updated_at=?""",
                (
                    attempt,
                    worker_id,
                    lease_until,
                    now_iso,
                    now_iso,
                    row["run_id"],
                    row["updated_at"],
                ),
            )
            if cursor.rowcount != 1:
                return None
            self._append_event_tx(
                connection,
                run_id=row["run_id"],
                attempt=attempt,
                event_type="run_attempt_started",
                payload={"run_id": row["run_id"], "attempt": attempt},
                created_at=now_iso,
            )
            claimed = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (row["run_id"],)
            ).fetchone()
        return self._from_run_row(claimed)

    def heartbeat(
        self, run_id: str, worker_id: str, attempt: int, *, lease_seconds: int
    ) -> AgentRunRecord:
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE agent_runs SET lease_until=?, updated_at=?
                WHERE run_id=? AND status='running' AND worker_id=? AND attempts=?""",
                (
                    _iso(now + timedelta(seconds=lease_seconds)),
                    _iso(now),
                    run_id,
                    worker_id,
                    attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise AgentRunLeaseLostError("Agent Run lease is no longer owned")
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._from_run_row(row)

    def append_owned_event(
        self,
        run_id: str,
        worker_id: str,
        attempt: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> AgentRunEvent:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                """SELECT 1 FROM agent_runs WHERE run_id=? AND status='running'
                AND worker_id=? AND attempts=?""",
                (run_id, worker_id, attempt),
            ).fetchone()
            if owner is None:
                raise AgentRunLeaseLostError("Agent Run lease is no longer owned")
            return self._append_event_tx(
                connection,
                run_id=run_id,
                attempt=attempt,
                event_type=event_type,
                payload=payload,
            )

    def complete(
        self,
        run_id: str,
        worker_id: str,
        attempt: int,
        result: AgentQueryResponse,
        done_payload: dict[str, Any] | None = None,
    ) -> AgentRunRecord:
        now = _iso()
        payload = done_payload or result.model_dump(mode="json")
        result_json = result.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE agent_runs SET status='completed', result_json=?,
                lease_until=NULL, worker_id=NULL, last_error_code=NULL,
                last_error=NULL, completed_at=?, updated_at=?
                WHERE run_id=? AND status='running' AND worker_id=? AND attempts=?""",
                (result_json, now, now, run_id, worker_id, attempt),
            )
            if cursor.rowcount != 1:
                raise AgentRunLeaseLostError("Agent Run lease changed before completion")
            self._append_event_tx(
                connection,
                run_id=run_id,
                attempt=attempt,
                event_type="done",
                payload=payload,
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._from_run_row(row)

    def fail(
        self,
        run_id: str,
        worker_id: str,
        attempt: int,
        error: Exception,
        *,
        retry_base_seconds: float,
    ) -> AgentRunRecord:
        current = self.get(run_id)
        if (
            current is None
            or current.status != "running"
            or current.worker_id != worker_id
            or current.attempts != attempt
        ):
            raise AgentRunLeaseLostError(
                "Agent Run lease changed before failure recording"
            )
        terminal = attempt >= current.max_attempts
        now = _utc_now()
        delay = retry_base_seconds * (2 ** max(0, attempt - 1))
        status: AgentRunStatus = "failed" if terminal else "retry"
        available_at = _iso(now if terminal else now + timedelta(seconds=delay))
        # Run records are retrievable by a shared reader role in the current
        # single-tenant boundary. Do not persist provider exception text: it
        # can contain request fragments, upstream URLs, or credentials.
        detail = f"{error.__class__.__name__}: Agent Run attempt failed"
        event_type = "error" if terminal else "run_attempt_failed"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE agent_runs SET status=?, available_at=?, lease_until=NULL,
                worker_id=NULL, last_error_code=?, last_error=?, completed_at=?,
                updated_at=? WHERE run_id=? AND status='running'
                AND worker_id=? AND attempts=?""",
                (
                    status,
                    available_at,
                    error.__class__.__name__,
                    detail,
                    _iso(now) if terminal else None,
                    _iso(now),
                    run_id,
                    worker_id,
                    attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise AgentRunLeaseLostError(
                    "Agent Run lease changed before failure recording"
                )
            self._append_event_tx(
                connection,
                run_id=run_id,
                attempt=attempt,
                event_type=event_type,
                payload={
                    "detail": "Agent Run failed" if terminal else "Agent Run will retry",
                    "error_code": error.__class__.__name__,
                    "attempt": attempt,
                },
                created_at=_iso(now),
            )
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._from_run_row(row)

    def request_cancel(self, run_id: str) -> AgentRunRecord:
        now = _iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = self._from_run_row(row)
            if current.status in TERMINAL_AGENT_RUN_STATUSES:
                return current
            if current.status in {"queued", "retry"}:
                connection.execute(
                    """UPDATE agent_runs SET status='cancelled', completed_at=?,
                    cancel_requested_at=?, updated_at=? WHERE run_id=?""",
                    (now, now, now, run_id),
                )
                self._append_event_tx(
                    connection,
                    run_id=run_id,
                    attempt=current.attempts,
                    event_type="cancelled",
                    payload={"run_id": run_id, "status": "cancelled"},
                    created_at=now,
                )
            elif current.cancel_requested_at is None:
                connection.execute(
                    """UPDATE agent_runs SET cancel_requested_at=?, updated_at=?
                    WHERE run_id=? AND status='running'""",
                    (now, now, run_id),
                )
                self._append_event_tx(
                    connection,
                    run_id=run_id,
                    attempt=current.attempts,
                    event_type="cancel_requested",
                    payload={"run_id": run_id, "status": "cancel_requested"},
                    created_at=now,
                )
            updated = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._from_run_row(updated)

    def is_cancel_requested(self, run_id: str, worker_id: str, attempt: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT cancel_requested_at FROM agent_runs WHERE run_id=?
                AND status='running' AND worker_id=? AND attempts=?""",
                (run_id, worker_id, attempt),
            ).fetchone()
        if row is None:
            raise AgentRunLeaseLostError("Agent Run lease is no longer owned")
        return row["cancel_requested_at"] is not None

    def finish_cancelled(
        self, run_id: str, worker_id: str, attempt: int
    ) -> AgentRunRecord:
        now = _iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE agent_runs SET status='cancelled', lease_until=NULL,
                worker_id=NULL, completed_at=?, updated_at=? WHERE run_id=?
                AND status='running' AND worker_id=? AND attempts=?""",
                (now, now, run_id, worker_id, attempt),
            )
            if cursor.rowcount != 1:
                raise AgentRunLeaseLostError(
                    "Agent Run lease changed before cancellation"
                )
            self._append_event_tx(
                connection,
                run_id=run_id,
                attempt=attempt,
                event_type="cancelled",
                payload={"run_id": run_id, "status": "cancelled"},
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._from_run_row(row)

    def stats(self) -> dict[str, int | float]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, count(*) AS c FROM agent_runs GROUP BY status"
            ).fetchall()
            oldest = connection.execute(
                """SELECT min(created_at) AS oldest FROM agent_runs
                WHERE status IN ('queued', 'retry')"""
            ).fetchone()
        result: dict[str, int | float] = {
            status: 0
            for status in (
                "queued",
                "running",
                "retry",
                "completed",
                "failed",
                "cancelled",
            )
        }
        result.update({row["status"]: row["c"] for row in rows})
        oldest_age = 0.0
        if oldest and oldest["oldest"]:
            oldest_age = max(
                0.0,
                (_utc_now() - datetime.fromisoformat(oldest["oldest"])).total_seconds(),
            )
        result["oldest_waiting_seconds"] = round(oldest_age, 3)
        return result

    def purge_terminal(self, *, older_than_days: int) -> int:
        cutoff = _iso(_utc_now() - timedelta(days=older_than_days))
        with self._connect() as connection:
            cursor = connection.execute(
                """DELETE FROM agent_runs WHERE status IN ('completed','failed','cancelled')
                AND completed_at IS NOT NULL AND completed_at<?""",
                (cutoff,),
            )
        return cursor.rowcount

    @staticmethod
    def _from_run_row(row: sqlite3.Row) -> AgentRunRecord:
        return AgentRunRecord.model_validate(dict(row))

    @staticmethod
    def _from_event_row(row: sqlite3.Row) -> AgentRunEvent:
        return AgentRunEvent.model_validate(dict(row))


agent_run_store = AgentRunStore()
