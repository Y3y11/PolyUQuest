"""Durable lifecycle targets with lease-based refresh scheduling."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from agent_rag.config import settings
from agent_rag.freshness.policy import FreshnessPolicy
from agent_rag.tools._ranking import normalize_url

LifecycleStatus = Literal[
    "active", "checking", "indexing", "retry", "quarantined", "paused"
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


class PageLifecycleTarget(BaseModel):
    source_url: str
    content_hash: str
    status: LifecycleStatus = "active"
    policy_version: str
    current_ttl_hours: float
    quality_score: float = 0.0
    business_priority: float = 1.0
    indexed_at: str
    last_checked_at: str | None = None
    last_validated_at: str | None = None
    last_changed_at: str | None = None
    next_check_at: str
    change_count: int = 0
    unchanged_count: int = 0
    access_count: int = 0
    last_accessed_at: str | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    lease_until: str | None = None
    worker_id: str | None = None
    pending_job_id: str | None = None
    pending_content_hash: str | None = None
    created_at: str
    updated_at: str


class PageLifecycleStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        policy: FreshnessPolicy | None = None,
    ):
        path = Path(db_path or settings.agent_ledger_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.policy = policy or FreshnessPolicy.from_config()
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
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
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS page_lifecycle_targets (
                    source_url TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    current_ttl_hours REAL NOT NULL,
                    quality_score REAL NOT NULL DEFAULT 0,
                    business_priority REAL NOT NULL DEFAULT 1,
                    indexed_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    last_validated_at TEXT,
                    last_changed_at TEXT,
                    next_check_at TEXT NOT NULL,
                    change_count INTEGER NOT NULL DEFAULT 0,
                    unchanged_count INTEGER NOT NULL DEFAULT 0,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    last_accessed_at TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    lease_until TEXT,
                    worker_id TEXT,
                    pending_job_id TEXT,
                    pending_content_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_lifecycle_claim
                ON page_lifecycle_targets(status, next_check_at, lease_until);
                """
            )

    def register_indexed(
        self,
        source_url: str,
        content_hash: str,
        *,
        quality_score: float = 0.0,
        business_priority: float = 1.0,
        validated_at: datetime | None = None,
    ) -> PageLifecycleTarget:
        now = validated_at or _utc_now()
        now_iso = _iso(now)
        url = normalize_url(source_url)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM page_lifecycle_targets WHERE source_url=?", (url,)
            ).fetchone()
            if row is None:
                ttl = self.policy.initial_ttl(quality_score, business_priority)
                next_check = _iso(now + timedelta(hours=ttl))
                connection.execute(
                    """
                    INSERT INTO page_lifecycle_targets(
                        source_url, content_hash, status, policy_version,
                        current_ttl_hours, quality_score, business_priority,
                        indexed_at, last_checked_at, last_validated_at,
                        last_changed_at, next_check_at, change_count,
                        unchanged_count, access_count, last_accessed_at,
                        consecutive_failures, last_error, lease_until, worker_id,
                        pending_job_id, pending_content_hash, created_at, updated_at
                    ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, NULL, ?, ?, ?,
                              0, 0, 0, NULL, 0, NULL, NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        url,
                        content_hash,
                        self.policy.policy_version,
                        ttl,
                        quality_score,
                        business_priority,
                        now_iso,
                        now_iso,
                        now_iso,
                        next_check,
                        now_iso,
                        now_iso,
                    ),
                )
            else:
                changed = row["content_hash"] != content_hash
                ttl = float(row["current_ttl_hours"])
                change_count = int(row["change_count"])
                last_changed_at = row["last_changed_at"]
                if changed:
                    ttl = self.policy.after_changed(ttl, int(row["access_count"]))
                    change_count += 1
                    last_changed_at = now_iso
                next_check = _iso(now + timedelta(hours=ttl))
                connection.execute(
                    """
                    UPDATE page_lifecycle_targets
                    SET content_hash=?, status='active', policy_version=?,
                        current_ttl_hours=?, quality_score=?,
                        business_priority=?, indexed_at=?, last_validated_at=?,
                        last_changed_at=?, next_check_at=?, change_count=?,
                        access_count=0, consecutive_failures=0, last_error=NULL,
                        lease_until=NULL, worker_id=NULL, pending_job_id=NULL,
                        pending_content_hash=NULL, updated_at=?
                    WHERE source_url=?
                    """,
                    (
                        content_hash,
                        self.policy.policy_version,
                        ttl,
                        quality_score,
                        business_priority,
                        now_iso,
                        now_iso,
                        last_changed_at,
                        next_check,
                        change_count,
                        now_iso,
                        url,
                    ),
                )
            updated = connection.execute(
                "SELECT * FROM page_lifecycle_targets WHERE source_url=?", (url,)
            ).fetchone()
        return self._from_row(updated)

    def bootstrap_indexed_pages(self, pages: Iterable[dict]) -> int:
        """Idempotently register graph pages created before the scheduler existed."""
        created = 0
        for page in pages:
            url = str(page.get("url", "")).strip()
            content_hash = str(page.get("content_hash", "")).strip()
            if not url or not content_hash or self.get(url) is not None:
                continue
            validated_at = None
            timestamp = page.get("last_validated_at") or page.get("fetched_at")
            if timestamp:
                try:
                    validated_at = datetime.fromisoformat(
                        str(timestamp).replace("Z", "+00:00")
                    )
                    if validated_at.tzinfo is None:
                        validated_at = validated_at.replace(tzinfo=UTC)
                except ValueError:
                    validated_at = None
            self.register_indexed(
                url,
                content_hash,
                quality_score=float(page.get("quality_score", 0.0) or 0.0),
                validated_at=validated_at,
            )
            created += 1
        return created

    def recover_stale_indexing(
        self, index_jobs: dict[str, tuple[str, str | None]]
    ) -> dict[str, int]:
        """Reconcile lifecycle rows after a crash between index and status writes."""
        report = {"activated": 0, "retried": 0, "waiting": 0}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM page_lifecycle_targets WHERE status='indexing'"
            ).fetchall()
        for row in rows:
            target = self._from_row(row)
            job_status, job_error = index_jobs.get(
                target.pending_job_id or "", ("missing", None)
            )
            now = _utc_now()
            if job_status == "succeeded":
                new_hash = target.pending_content_hash or target.content_hash
                self.register_indexed(
                    target.source_url,
                    new_hash,
                    quality_score=target.quality_score,
                    business_priority=target.business_priority,
                    validated_at=now,
                )
                report["activated"] += 1
            elif job_status in {"dead_letter", "missing"}:
                failures = target.consecutive_failures + 1
                delay = self.policy.after_failure(failures)
                with self._connect() as connection:
                    connection.execute(
                        """
                        UPDATE page_lifecycle_targets
                        SET status='retry', consecutive_failures=?, last_error=?,
                            next_check_at=?, pending_job_id=NULL,
                            pending_content_hash=NULL, updated_at=?
                        WHERE source_url=? AND status='indexing'
                        """,
                        (
                            failures,
                            (job_error or f"index job {job_status}")[:4000],
                            _iso(now + timedelta(hours=delay)),
                            _iso(now),
                            target.source_url,
                        ),
                    )
                report["retried"] += 1
            else:
                report["waiting"] += 1
        return report

    def get(self, source_url: str) -> PageLifecycleTarget | None:
        url = normalize_url(source_url)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM page_lifecycle_targets WHERE source_url=?", (url,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def get_many(self, urls: Iterable[str]) -> dict[str, PageLifecycleTarget]:
        normalized = list(dict.fromkeys(normalize_url(url) for url in urls if url))
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM page_lifecycle_targets "  # noqa: S608
                f"WHERE source_url IN ({placeholders})",
                normalized,
            ).fetchall()
        return {row["source_url"]: self._from_row(row) for row in rows}

    def list(
        self, *, status: LifecycleStatus | None = None, limit: int = 50
    ) -> list[PageLifecycleTarget]:
        sql = "SELECT * FROM page_lifecycle_targets"
        params: list[object] = []
        if status is not None:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY next_check_at ASC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def claim(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 120,
        now: datetime | None = None,
    ) -> PageLifecycleTarget | None:
        current = now or _utc_now()
        now_iso = _iso(current)
        lease_until = _iso(current + timedelta(seconds=lease_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM page_lifecycle_targets
                WHERE (
                    status IN ('active', 'retry') AND next_check_at <= ?
                ) OR (
                    status='checking' AND lease_until IS NOT NULL AND lease_until <= ?
                )
                ORDER BY next_check_at ASC LIMIT 1
                """,
                (now_iso, now_iso),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE page_lifecycle_targets
                SET status='checking', worker_id=?, lease_until=?,
                    last_checked_at=?, updated_at=?
                WHERE source_url=? AND updated_at=?
                """,
                (
                    worker_id,
                    lease_until,
                    now_iso,
                    now_iso,
                    row["source_url"],
                    row["updated_at"],
                ),
            )
            if cursor.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM page_lifecycle_targets WHERE source_url=?",
                (row["source_url"],),
            ).fetchone()
        return self._from_row(claimed)

    def mark_unchanged(
        self,
        source_url: str,
        worker_id: str,
        *,
        validated_at: datetime | None = None,
    ) -> PageLifecycleTarget:
        target = self._leased(source_url, worker_id)
        now = validated_at or _utc_now()
        ttl = self.policy.after_unchanged(
            target.current_ttl_hours, target.access_count
        )
        return self._complete_lease(
            target,
            worker_id,
            """
            status='active', current_ttl_hours=?, last_validated_at=?,
            next_check_at=?, unchanged_count=unchanged_count + 1,
            access_count=0, consecutive_failures=0, last_error=NULL,
            lease_until=NULL, worker_id=NULL, updated_at=?
            """,
            (
                ttl,
                _iso(now),
                _iso(now + timedelta(hours=ttl)),
                _iso(now),
            ),
        )

    def mark_query_validated_unchanged(
        self,
        source_url: str,
        *,
        validated_at: datetime | None = None,
    ) -> PageLifecycleTarget:
        target = self.get(source_url)
        if target is None:
            raise KeyError(source_url)
        now = validated_at or _utc_now()
        ttl = self.policy.after_unchanged(
            target.current_ttl_hours, target.access_count
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE page_lifecycle_targets
                SET status='active',
                    current_ttl_hours=?, last_checked_at=?, last_validated_at=?,
                    next_check_at=?, unchanged_count=unchanged_count + 1,
                    access_count=0, consecutive_failures=0, last_error=NULL,
                    updated_at=?
                WHERE source_url=? AND status IN ('active', 'retry')
                """,
                (
                    ttl,
                    _iso(now),
                    _iso(now),
                    _iso(now + timedelta(hours=ttl)),
                    _iso(now),
                    target.source_url,
                ),
            )
            row = connection.execute(
                "SELECT * FROM page_lifecycle_targets WHERE source_url=?",
                (target.source_url,),
            ).fetchone()
        return self._from_row(row)

    def mark_indexing(
        self,
        source_url: str,
        worker_id: str,
        job_id: str,
        content_hash: str,
    ) -> PageLifecycleTarget:
        target = self.get(source_url)
        if target is None:
            raise KeyError(source_url)
        if target.status == "active" and target.content_hash == content_hash:
            return target
        if target.status != "checking" or target.worker_id != worker_id:
            raise ValueError("Lifecycle target is not leased by this worker")
        now = _iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE page_lifecycle_targets
                SET status='indexing', pending_job_id=?, pending_content_hash=?,
                    lease_until=NULL, worker_id=NULL, consecutive_failures=0,
                    last_error=NULL, updated_at=?
                WHERE source_url=? AND status='checking' AND worker_id=?
                """,
                (job_id, content_hash, now, target.source_url, worker_id),
            )
            row = connection.execute(
                "SELECT * FROM page_lifecycle_targets WHERE source_url=?",
                (target.source_url,),
            ).fetchone()
        current = self._from_row(row)
        if cursor.rowcount == 1:
            return current
        if current.status == "active" and current.content_hash == content_hash:
            return current
        raise ValueError("Lifecycle target lease changed before indexing was recorded")

    def quarantine(
        self, source_url: str, worker_id: str, reason: str
    ) -> PageLifecycleTarget:
        target = self._leased(source_url, worker_id)
        return self._complete_lease(
            target,
            worker_id,
            """
            status='quarantined', last_error=?, lease_until=NULL,
            worker_id=NULL, updated_at=?
            """,
            (reason[:4000], _iso()),
        )

    def fail(
        self, source_url: str, worker_id: str, error: str
    ) -> PageLifecycleTarget:
        target = self._leased(source_url, worker_id)
        failures = target.consecutive_failures + 1
        delay = self.policy.after_failure(failures)
        now = _utc_now()
        return self._complete_lease(
            target,
            worker_id,
            """
            status='retry', consecutive_failures=?, last_error=?,
            next_check_at=?, lease_until=NULL, worker_id=NULL, updated_at=?
            """,
            (failures, error[:4000], _iso(now + timedelta(hours=delay)), _iso(now)),
        )

    def record_access_many(self, urls: Iterable[str]) -> int:
        normalized = list(dict.fromkeys(normalize_url(url) for url in urls if url))
        if not normalized:
            return 0
        placeholders = ",".join("?" for _ in normalized)
        now = _iso()
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE page_lifecycle_targets SET access_count=access_count + 1, "  # noqa: S608
                f"last_accessed_at=?, updated_at=? WHERE source_url IN ({placeholders})",
                (now, now, *normalized),
            )
        return cursor.rowcount

    def refresh_now(
        self, source_url: str, *, now: datetime | None = None
    ) -> PageLifecycleTarget:
        current = _iso(now)
        return self._set_manual_state(
            source_url,
            "status=CASE WHEN status IN ('checking', 'indexing', 'paused') "
            "THEN status ELSE 'active' END, "
            "next_check_at=CASE WHEN status IN ('checking', 'indexing', 'paused') "
            "THEN next_check_at ELSE ? END, updated_at=?",
            (current, current),
        )

    def pause(self, source_url: str) -> PageLifecycleTarget:
        target = self.get(source_url)
        if target is None:
            raise KeyError(source_url)
        if target.status in {"checking", "indexing"}:
            raise ValueError("Checking or indexing targets cannot be paused")
        return self._set_manual_state(
            source_url,
            "status='paused', lease_until=NULL, worker_id=NULL, updated_at=?",
            (_iso(),),
        )

    def resume(
        self, source_url: str, *, now: datetime | None = None
    ) -> PageLifecycleTarget:
        target = self.get(source_url)
        if target is None:
            raise KeyError(source_url)
        if target.status in {"checking", "indexing"}:
            raise ValueError("Checking or indexing targets cannot be resumed")
        now_iso = _iso(now)
        return self._set_manual_state(
            source_url,
            "status='active', next_check_at=?, lease_until=NULL, worker_id=NULL, "
            "last_error=NULL, updated_at=?",
            (now_iso, now_iso),
        )

    def stats(self, *, now: datetime | None = None) -> dict[str, int | float]:
        current = now or _utc_now()
        now_iso = _iso(current)
        result: dict[str, int | float] = {
            status: 0
            for status in (
                "active", "checking", "indexing", "retry", "quarantined", "paused"
            )
        }
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, count(*) AS count FROM page_lifecycle_targets GROUP BY status"
            ).fetchall()
            aggregate = connection.execute(
                """
                SELECT count(*) AS total,
                       coalesce(avg(current_ttl_hours), 0) AS average_ttl,
                       coalesce(sum(change_count), 0) AS changes,
                       coalesce(sum(unchanged_count), 0) AS unchanged
                FROM page_lifecycle_targets
                """
            ).fetchone()
            due = connection.execute(
                """
                SELECT count(*) AS count, min(next_check_at) AS oldest
                FROM page_lifecycle_targets
                WHERE status IN ('active', 'retry') AND next_check_at <= ?
                """,
                (now_iso,),
            ).fetchone()
        result.update({row["status"]: int(row["count"]) for row in rows})
        result.update(
            {
                "total": int(aggregate["total"]),
                "average_ttl_hours": round(float(aggregate["average_ttl"]), 4),
                "total_changes": int(aggregate["changes"]),
                "total_unchanged": int(aggregate["unchanged"]),
                "due_targets": int(due["count"]),
                "oldest_overdue_seconds": 0.0,
            }
        )
        if due["oldest"]:
            oldest = datetime.fromisoformat(due["oldest"])
            result["oldest_overdue_seconds"] = round(
                max(0.0, (current - oldest).total_seconds()), 3
            )
        return result

    def _leased(self, source_url: str, worker_id: str) -> PageLifecycleTarget:
        target = self.get(source_url)
        if target is None:
            raise KeyError(source_url)
        if target.status != "checking" or target.worker_id != worker_id:
            raise ValueError("Lifecycle target is not leased by this worker")
        return target

    def _complete_lease(
        self,
        target: PageLifecycleTarget,
        worker_id: str,
        set_clause: str,
        values: tuple[object, ...],
    ) -> PageLifecycleTarget:
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE page_lifecycle_targets SET {set_clause} "  # noqa: S608
                "WHERE source_url=? AND status='checking' AND worker_id=?",
                (*values, target.source_url, worker_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Lifecycle target lease changed before completion")
            row = connection.execute(
                "SELECT * FROM page_lifecycle_targets WHERE source_url=?",
                (target.source_url,),
            ).fetchone()
        return self._from_row(row)

    def _set_manual_state(
        self, source_url: str, set_clause: str, values: tuple[object, ...]
    ) -> PageLifecycleTarget:
        url = normalize_url(source_url)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE page_lifecycle_targets SET {set_clause} "  # noqa: S608
                "WHERE source_url=?",
                (*values, url),
            )
            if cursor.rowcount != 1:
                raise KeyError(url)
            row = connection.execute(
                "SELECT * FROM page_lifecycle_targets WHERE source_url=?", (url,)
            ).fetchone()
        return self._from_row(row)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> PageLifecycleTarget:
        return PageLifecycleTarget.model_validate(dict(row))


page_lifecycle_store = PageLifecycleStore()
