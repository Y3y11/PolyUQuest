"""Durable, bounded, body-free security audit ledger."""

from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from agent_rag.config import settings
from agent_rag.security.models import SecurityAuditEvent


class SecurityAuditStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS security_audit_events (
                    event_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    auth_mode TEXT NOT NULL,
                    method TEXT NOT NULL,
                    route TEXT NOT NULL,
                    required_role TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    outcome TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_security_audit_created "
                "ON security_audit_events(created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_security_audit_principal "
                "ON security_audit_events(principal_id, created_at DESC)"
            )

    def record(self, event: SecurityAuditEvent) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO security_audit_events (
                    event_id, request_id, created_at, principal_id, role,
                    auth_mode, method, route, required_role, status_code, outcome
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.request_id,
                    event.created_at,
                    event.principal_id,
                    event.role,
                    event.auth_mode,
                    event.method,
                    event.route,
                    event.required_role,
                    event.status_code,
                    event.outcome,
                ),
            )

    def list(
        self,
        *,
        principal_id: str = "",
        outcome: str = "",
        limit: int = 100,
    ) -> list[SecurityAuditEvent]:
        clauses: list[str] = []
        values: list[object] = []
        if principal_id:
            clauses.append("principal_id = ?")
            values.append(principal_id)
        if outcome:
            clauses.append("outcome = ?")
            values.append(outcome)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM security_audit_events {where} "
                "ORDER BY created_at DESC LIMIT ?",
                values,
            ).fetchall()
        return [SecurityAuditEvent.model_validate(dict(row)) for row in rows]

    def stats(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT outcome, COUNT(*) AS count FROM security_audit_events "
                "GROUP BY outcome"
            ).fetchall()
        counts = Counter({row["outcome"]: row["count"] for row in rows})
        return {
            "total": sum(counts.values()),
            "allowed": counts["allowed"],
            "unauthorized": counts["unauthorized"],
            "forbidden": counts["forbidden"],
            "error": counts["error"],
        }

    def purge(self, cutoff_iso: str) -> int:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM security_audit_events WHERE created_at < ?", (cutoff_iso,)
            )
            return cursor.rowcount


security_audit_store = SecurityAuditStore(settings.security_audit_path)
