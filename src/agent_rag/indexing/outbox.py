"""SQLite outbox for durable, lease-based graph indexing jobs."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from agent_rag.config import settings
from agent_rag.tools._ranking import normalize_url
from agent_rag.tools.schemas import GraphPatch

IndexJobStatus = Literal[
    "pending", "running", "retry", "succeeded", "dead_letter"
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


class IndexJob(BaseModel):
    job_id: str
    dedupe_key: str
    patch_id: str
    run_id: str
    source_url: str
    content_hash: str
    status: IndexJobStatus = "pending"
    attempts: int = 0
    total_attempts: int = 0
    manual_retries: int = 0
    max_attempts: int = 5
    available_at: str
    lease_until: str | None = None
    worker_id: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None


class IndexOutbox:
    def __init__(self, db_path: str | Path | None = None):
        path = Path(db_path or settings.agent_ledger_path)
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
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS index_jobs (
                        job_id TEXT PRIMARY KEY,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        patch_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        total_attempts INTEGER NOT NULL DEFAULT 0,
                        manual_retries INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL,
                        available_at TEXT NOT NULL,
                        lease_until TEXT,
                        worker_id TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_index_jobs_claim
                    ON index_jobs(status, available_at, lease_until, created_at);
                    CREATE INDEX IF NOT EXISTS idx_index_jobs_patch
                    ON index_jobs(patch_id);
                    """
                )
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(index_jobs)")
                }
                if "total_attempts" not in columns:
                    connection.execute(
                        "ALTER TABLE index_jobs ADD COLUMN total_attempts "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                if "manual_retries" not in columns:
                    connection.execute(
                        "ALTER TABLE index_jobs ADD COLUMN manual_retries "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                connection.commit()
            finally:
                connection.close()
            self._initialized = True

    @staticmethod
    def dedupe_key(source_url: str, content_hash: str) -> str:
        canonical = normalize_url(source_url)
        return hashlib.sha256(
            f"{canonical}\n{content_hash}".encode()
        ).hexdigest()

    def enqueue(
        self, patch: GraphPatch, *, max_attempts: int | None = None
    ) -> tuple[IndexJob, bool]:
        now = _iso()
        key = self.dedupe_key(patch.source_url, patch.content_hash)
        job = IndexJob(
            job_id=f"job-{uuid.uuid4().hex}",
            dedupe_key=key,
            patch_id=patch.patch_id,
            run_id=patch.run_id,
            source_url=normalize_url(patch.source_url),
            content_hash=patch.content_hash,
            max_attempts=max_attempts or settings.index_job_max_attempts,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO index_jobs(
                    job_id, dedupe_key, patch_id, run_id, source_url,
                        content_hash, status, attempts, total_attempts,
                        manual_retries, max_attempts, available_at,
                    lease_until, worker_id, last_error, created_at, updated_at,
                    started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.dedupe_key,
                    job.patch_id,
                    job.run_id,
                    job.source_url,
                    job.content_hash,
                    job.status,
                    job.attempts,
                    job.total_attempts,
                    job.manual_retries,
                    job.max_attempts,
                    job.available_at,
                    job.lease_until,
                    job.worker_id,
                    job.last_error,
                    job.created_at,
                    job.updated_at,
                    job.started_at,
                    job.completed_at,
                ),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT * FROM index_jobs WHERE dedupe_key = ?", (key,)
            ).fetchone()
        return self._from_row(row), created

    def get_by_snapshot(self, source_url: str, content_hash: str) -> IndexJob | None:
        key = self.dedupe_key(source_url, content_hash)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM index_jobs WHERE dedupe_key = ?", (key,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def get(self, job_id: str) -> IndexJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM index_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list(
        self, *, status: IndexJobStatus | None = None, limit: int = 50
    ) -> list[IndexJob]:
        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM index_jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM index_jobs WHERE status = ?
                    ORDER BY created_at DESC LIMIT ?""",
                    (status, limit),
                ).fetchall()
        return [self._from_row(row) for row in rows]

    def claim(self, worker_id: str, *, lease_seconds: int = 120) -> IndexJob | None:
        now = _utc_now()
        now_iso = _iso(now)
        lease_until = _iso(now + timedelta(seconds=lease_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM index_jobs
                WHERE (
                    status IN ('pending', 'retry') AND available_at <= ?
                ) OR (
                    status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?
                )
                ORDER BY available_at ASC, created_at ASC
                LIMIT 1
                """,
                (now_iso, now_iso),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE index_jobs
                SET status='running', attempts=attempts + 1,
                    total_attempts=total_attempts + 1,
                    worker_id=?, lease_until=?, updated_at=?,
                    started_at=coalesce(started_at, ?)
                WHERE job_id=? AND updated_at=?
                """,
                (
                    worker_id,
                    lease_until,
                    now_iso,
                    now_iso,
                    row["job_id"],
                    row["updated_at"],
                ),
            )
            if cursor.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM index_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
        return self._from_row(claimed)

    def succeed(self, job_id: str, worker_id: str) -> IndexJob:
        now = _iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE index_jobs SET status='succeeded', completed_at=?,
                lease_until=NULL, worker_id=NULL, last_error=NULL, updated_at=?
                WHERE job_id=? AND status='running' AND worker_id=?""",
                (now, now, job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Index job is not leased by this worker")
            row = connection.execute(
                "SELECT * FROM index_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._from_row(row)

    def heartbeat(
        self, job_id: str, worker_id: str, *, lease_seconds: int = 120
    ) -> IndexJob:
        """Extend an active lease using compare-and-set ownership semantics."""
        now = _utc_now()
        lease_until = _iso(now + timedelta(seconds=lease_seconds))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE index_jobs SET lease_until=?, updated_at=?
                WHERE job_id=? AND status='running' AND worker_id=?
                """,
                (lease_until, _iso(now), job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Index job lease is no longer owned by this worker")
            row = connection.execute(
                "SELECT * FROM index_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._from_row(row)

    def fail(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        *,
        retry_base_seconds: float = 2.0,
        retry_max_seconds: float = 300.0,
    ) -> IndexJob:
        current = self.get(job_id)
        if current is None:
            raise KeyError(job_id)
        if current.status != "running" or current.worker_id != worker_id:
            raise ValueError("Index job is not leased by this worker")
        terminal = current.attempts >= current.max_attempts
        delay = min(
            retry_max_seconds,
            retry_base_seconds * (2 ** max(0, current.attempts - 1)),
        )
        now = _utc_now()
        available_at = _iso(now if terminal else now + timedelta(seconds=delay))
        status: IndexJobStatus = "dead_letter" if terminal else "retry"
        completed_at = _iso(now) if terminal else None
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE index_jobs SET status=?, available_at=?, lease_until=NULL,
                worker_id=NULL, last_error=?, updated_at=?, completed_at=?
                WHERE job_id=? AND status='running' AND worker_id=?""",
                (
                    status,
                    available_at,
                    error[:4000],
                    _iso(now),
                    completed_at,
                    job_id,
                    worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Index job lease changed before failure was recorded")
            row = connection.execute(
                "SELECT * FROM index_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._from_row(row)

    def retry(self, job_id: str) -> IndexJob:
        now = _iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE index_jobs SET status='pending', attempts=0,
                manual_retries=manual_retries + 1, available_at=?,
                lease_until=NULL, worker_id=NULL, last_error=NULL,
                completed_at=NULL, updated_at=?
                WHERE job_id=? AND status IN ('dead_letter', 'retry')""",
                (now, now, job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only retry or dead-letter jobs can be retried")
            row = connection.execute(
                "SELECT * FROM index_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._from_row(row)

    def stats(self) -> dict[str, int | float]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, count(*) AS c FROM index_jobs GROUP BY status"
            ).fetchall()
            oldest = connection.execute(
                """SELECT min(created_at) AS oldest FROM index_jobs
                WHERE status IN ('pending', 'retry')"""
            ).fetchone()
        counts = {status: 0 for status in (
            "pending", "running", "retry", "succeeded", "dead_letter"
        )}
        counts.update({row["status"]: row["c"] for row in rows})
        oldest_age = 0.0
        if oldest and oldest["oldest"]:
            created = datetime.fromisoformat(oldest["oldest"])
            oldest_age = max(0.0, (_utc_now() - created).total_seconds())
        return {**counts, "oldest_waiting_seconds": round(oldest_age, 3)}

    def purge_completed(self, *, older_than_days: int = 30) -> int:
        """Delete old terminal jobs; active and dead-letter jobs remain auditable."""
        cutoff = _iso(_utc_now() - timedelta(days=older_than_days))
        with self._connect() as connection:
            cursor = connection.execute(
                """DELETE FROM index_jobs
                WHERE status='succeeded' AND completed_at IS NOT NULL
                AND completed_at < ?""",
                (cutoff,),
            )
        return cursor.rowcount

    @staticmethod
    def _from_row(row: sqlite3.Row) -> IndexJob:
        return IndexJob.model_validate(dict(row))


index_outbox = IndexOutbox()
