"""Durable cross-process heartbeat and readiness contract for workers."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agent_rag.config import settings


def _now() -> datetime:
    return datetime.now(UTC)


class WorkerStatus(BaseModel):
    instance_id: str
    pid: int = Field(ge=1)
    state: Literal["running", "stopped"]
    capabilities: list[str] = Field(default_factory=list)
    started_at: str
    heartbeat_at: str
    stopped_at: str | None = None
    healthy: bool = False
    heartbeat_age_seconds: float = 0.0


class WorkerStatusStore:
    def __init__(self, db_path: str | Path | None = None):
        path = Path(db_path or settings.agent_ledger_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS worker_heartbeats (
                    instance_id TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    stopped_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_worker_heartbeat_latest
                ON worker_heartbeats(started_at DESC);
                """
            )

    def register(
        self,
        instance_id: str,
        *,
        pid: int,
        capabilities: list[str],
    ) -> WorkerStatus:
        now = _now().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO worker_heartbeats(
                    instance_id, pid, state, capabilities_json,
                    started_at, heartbeat_at, stopped_at
                ) VALUES (?, ?, 'running', ?, ?, ?, NULL)
                """,
                (instance_id, pid, json.dumps(sorted(capabilities)), now, now),
            )
        status = self.get(instance_id)
        if status is None:  # pragma: no cover - defensive read-after-write guard
            raise RuntimeError("Worker heartbeat registration was not persisted")
        return status

    def heartbeat(self, instance_id: str) -> WorkerStatus:
        now = _now().isoformat()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE worker_heartbeats
                SET heartbeat_at=?
                WHERE instance_id=? AND state='running'
                """,
                (now, instance_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(instance_id)
        status = self.get(instance_id)
        if status is None:  # pragma: no cover
            raise RuntimeError("Worker heartbeat disappeared after update")
        return status

    def stop(self, instance_id: str) -> WorkerStatus:
        now = _now().isoformat()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE worker_heartbeats
                SET state='stopped', heartbeat_at=?, stopped_at=?
                WHERE instance_id=? AND state='running'
                """,
                (now, now, instance_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(instance_id)
        status = self.get(instance_id)
        if status is None:  # pragma: no cover
            raise RuntimeError("Worker heartbeat disappeared after stop")
        return status

    def get(
        self,
        instance_id: str,
        *,
        max_age_seconds: float | None = None,
    ) -> WorkerStatus | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM worker_heartbeats WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
        return self._from_row(row, max_age_seconds=max_age_seconds) if row else None

    def list(
        self,
        *,
        limit: int = 50,
        max_age_seconds: float | None = None,
    ) -> list[WorkerStatus]:
        selected_limit = max(1, min(int(limit), 500))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM worker_heartbeats ORDER BY started_at DESC LIMIT ?",
                (selected_limit,),
            ).fetchall()
        return [
            self._from_row(row, max_age_seconds=max_age_seconds) for row in rows
        ]

    def latest(
        self,
        *,
        max_age_seconds: float | None = None,
    ) -> WorkerStatus | None:
        records = self.list(limit=1, max_age_seconds=max_age_seconds)
        return records[0] if records else None

    @staticmethod
    def _from_row(
        row: sqlite3.Row,
        *,
        max_age_seconds: float | None,
    ) -> WorkerStatus:
        heartbeat = datetime.fromisoformat(str(row["heartbeat_at"]))
        age = max(0.0, (_now() - heartbeat).total_seconds())
        threshold = float(
            max_age_seconds
            if max_age_seconds is not None
            else settings.worker_heartbeat_max_age_seconds
        )
        state = str(row["state"])
        return WorkerStatus(
            instance_id=str(row["instance_id"]),
            pid=int(row["pid"]),
            state=state,
            capabilities=json.loads(str(row["capabilities_json"])),
            started_at=str(row["started_at"]),
            heartbeat_at=str(row["heartbeat_at"]),
            stopped_at=row["stopped_at"],
            healthy=state == "running" and age <= threshold,
            heartbeat_age_seconds=round(age, 3),
        )


worker_status_store = WorkerStatusStore()


def health_main() -> None:
    parser = argparse.ArgumentParser(description="Check the latest Worker heartbeat")
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=settings.worker_heartbeat_max_age_seconds,
    )
    args = parser.parse_args()
    latest = worker_status_store.latest(max_age_seconds=args.max_age_seconds)
    if latest is None or not latest.healthy:
        raise SystemExit(1)
    print(
        json.dumps(
            {
                "instance_id": latest.instance_id,
                "state": latest.state,
                "healthy": latest.healthy,
                "heartbeat_age_seconds": latest.heartbeat_age_seconds,
            },
            sort_keys=True,
        )
    )
