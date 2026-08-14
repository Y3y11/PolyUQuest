"""SQLite ledger for reconciliation scans and confirmed repairs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_rag.config import settings
from agent_rag.reconciliation.models import (
    ConsistencyFinding,
    ReconciliationRun,
    ReconciliationRunDetail,
    RepairAction,
    utc_now,
)


class ReconciliationStore:
    def __init__(self, db_path: str | Path | None = None):
        path = Path(db_path or settings.agent_ledger_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS reconciliation_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    verification_of_run_id TEXT,
                    findings_count INTEGER NOT NULL DEFAULT 0,
                    actions_count INTEGER NOT NULL DEFAULT 0,
                    automatic_count INTEGER NOT NULL DEFAULT 0,
                    manual_review_count INTEGER NOT NULL DEFAULT 0,
                    succeeded_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    summary_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    scan_completed_at TEXT,
                    execution_started_at TEXT,
                    execution_lease_until TEXT,
                    execution_owner_id TEXT,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_created
                ON reconciliation_runs(created_at DESC);
                CREATE TABLE IF NOT EXISTS consistency_findings (
                    finding_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    patch_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    expected_json TEXT NOT NULL,
                    actual_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    repairability TEXT NOT NULL,
                    recommended_action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES reconciliation_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_findings_run
                ON consistency_findings(run_id, severity, category);
                CREATE TABLE IF NOT EXISTS repair_actions (
                    action_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    finding_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requires_confirmation INTEGER NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    execution_owner_id TEXT,
                    UNIQUE(run_id, action_type, target_id),
                    FOREIGN KEY(run_id) REFERENCES reconciliation_runs(run_id),
                    FOREIGN KEY(finding_id) REFERENCES consistency_findings(finding_id)
                );
                CREATE INDEX IF NOT EXISTS idx_repair_actions_run
                ON repair_actions(run_id, status);
                """
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(reconciliation_runs)"
                )
            }
            if "execution_lease_until" not in columns:
                connection.execute(
                    "ALTER TABLE reconciliation_runs "
                    "ADD COLUMN execution_lease_until TEXT"
                )
            if "execution_owner_id" not in columns:
                connection.execute(
                    "ALTER TABLE reconciliation_runs "
                    "ADD COLUMN execution_owner_id TEXT"
                )
            if "verification_of_run_id" not in columns:
                connection.execute(
                    "ALTER TABLE reconciliation_runs "
                    "ADD COLUMN verification_of_run_id TEXT"
                )
            action_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(repair_actions)"
                )
            }
            if "execution_owner_id" not in action_columns:
                connection.execute(
                    "ALTER TABLE repair_actions "
                    "ADD COLUMN execution_owner_id TEXT"
                )

    def create(self, run: ReconciliationRun) -> ReconciliationRun:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO reconciliation_runs(
                    run_id, status, scope, verification_of_run_id,
                    findings_count, actions_count,
                    automatic_count, manual_review_count, succeeded_count,
                    failed_count, skipped_count, summary_json, error,
                    created_at, scan_completed_at, execution_started_at,
                    execution_lease_until, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                self._run_values(run),
            )
        return run

    def save_plan(
        self,
        run: ReconciliationRun,
        findings: list[ConsistencyFinding],
        actions: list[RepairAction],
    ) -> ReconciliationRunDetail:
        run.status = "planned"
        run.findings_count = len(findings)
        run.actions_count = len(actions)
        run.automatic_count = sum(
            item.repairability == "automatic" for item in findings
        )
        run.manual_review_count = sum(
            item.repairability == "manual_review" for item in findings
        )
        run.scan_completed_at = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._update_run(connection, run)
            for finding in findings:
                connection.execute(
                    """INSERT INTO consistency_findings(
                        finding_id, run_id, category, severity, object_type,
                        object_id, source_url, patch_id, version_id, job_id,
                        expected_json, actual_json, reason, repairability,
                        recommended_action, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    self._finding_values(finding),
                )
            for action in actions:
                connection.execute(
                    """INSERT OR IGNORE INTO repair_actions(
                        action_id, run_id, finding_id, action_type, target_id,
                        status, requires_confirmation, before_json, after_json,
                        error, created_at, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    self._action_values(action),
                )
        return self.get_detail(run.run_id)  # type: ignore[return-value]

    def mark_scan_failed(self, run: ReconciliationRun, error: str) -> ReconciliationRun:
        run.status = "failed"
        run.error = error[:4000]
        run.scan_completed_at = utc_now()
        run.completed_at = run.scan_completed_at
        with self._connect() as connection:
            self._update_run(connection, run)
        return run

    def get(self, run_id: str) -> ReconciliationRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reconciliation_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._run_from_row(row) if row else None

    def get_detail(self, run_id: str) -> ReconciliationRunDetail | None:
        with self._connect() as connection:
            run_row = connection.execute(
                "SELECT * FROM reconciliation_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run_row is None:
                return None
            finding_rows = connection.execute(
                "SELECT * FROM consistency_findings WHERE run_id=? "
                "ORDER BY severity DESC, created_at, finding_id",
                (run_id,),
            ).fetchall()
            action_rows = connection.execute(
                "SELECT * FROM repair_actions WHERE run_id=? "
                "ORDER BY created_at, action_id",
                (run_id,),
            ).fetchall()
        return ReconciliationRunDetail(
            run=self._run_from_row(run_row),
            findings=[self._finding_from_row(row) for row in finding_rows],
            actions=[self._action_from_row(row) for row in action_rows],
        )

    def list(self, limit: int = 50) -> list[ReconciliationRun]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reconciliation_runs "
                "ORDER BY created_at DESC, run_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def claim_execution(
        self, run_id: str, owner_id: str, *, lease_seconds: int = 300
    ) -> ReconciliationRun:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE reconciliation_runs SET status='executing', "
                "execution_started_at=?, execution_lease_until=?, "
                "execution_owner_id=?, error=NULL "
                "WHERE run_id=? AND (status='planned' OR "
                "(status='executing' AND execution_lease_until <= ?))",
                (now, lease_until, owner_id, run_id, now),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only a planned reconciliation run can execute")
            row = connection.execute(
                "SELECT * FROM reconciliation_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._run_from_row(row)

    def claim_action(self, action_id: str, owner_id: str) -> RepairAction | None:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE repair_actions SET status='running', started_at=?, "
                "execution_owner_id=? WHERE action_id=? AND status='planned'",
                (now, owner_id, action_id),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM repair_actions WHERE action_id=?", (action_id,)
            ).fetchone()
        return self._action_from_row(row)

    def heartbeat_execution(
        self, run_id: str, owner_id: str, *, lease_seconds: int = 300
    ) -> ReconciliationRun:
        now_dt = datetime.now(UTC)
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE reconciliation_runs SET execution_lease_until=? "
                "WHERE run_id=? AND status='executing' AND execution_owner_id=?",
                (lease_until, run_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Reconciliation execution lease is no longer active")
        return self.get(run_id)  # type: ignore[return-value]

    def recover_stale_actions(self, run_id: str) -> int:
        """Return interrupted running actions to planned after lease reclaim."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE repair_actions SET status='planned', started_at=NULL, "
                "execution_owner_id=NULL, "
                "error='Recovered after expired execution lease' "
                "WHERE run_id=? AND status='running'", (run_id,)
            )
        return cursor.rowcount

    def finish_action(self, action: RepairAction, owner_id: str) -> RepairAction:
        action.completed_at = action.completed_at or utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE repair_actions SET status=?, before_json=?, after_json=?,
                error=?, started_at=?, completed_at=?, execution_owner_id=NULL
                WHERE action_id=? AND execution_owner_id=?""",
                (
                    action.status,
                    json.dumps(action.before, ensure_ascii=False, default=str),
                    json.dumps(action.after, ensure_ascii=False, default=str),
                    action.error,
                    action.started_at,
                    action.completed_at,
                    action.action_id,
                    owner_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Repair action is no longer owned by this execution")
        return action

    def finish_execution(
        self, run_id: str, owner_id: str
    ) -> ReconciliationRunDetail:
        with self._connect() as connection:
            counts = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM repair_actions "
                    "WHERE run_id=? GROUP BY status", (run_id,)
                ).fetchall()
            }
            cursor = connection.execute(
                """UPDATE reconciliation_runs SET status='completed',
                succeeded_count=?, failed_count=?, skipped_count=?,
                execution_lease_until=NULL, execution_owner_id=NULL,
                completed_at=? WHERE run_id=? AND status='executing'
                AND execution_owner_id=?""",
                (
                    counts.get("succeeded", 0),
                    counts.get("failed", 0),
                    counts.get("skipped", 0),
                    utc_now(),
                    run_id,
                    owner_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Reconciliation execution lease was lost")
        return self.get_detail(run_id)  # type: ignore[return-value]

    def fail_execution(
        self, run_id: str, owner_id: str, error: str
    ) -> ReconciliationRun:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE reconciliation_runs SET status='failed', error=?, "
                "execution_lease_until=NULL, execution_owner_id=NULL, "
                "completed_at=? WHERE run_id=? AND status='executing' "
                "AND execution_owner_id=?",
                (error[:4000], utc_now(), run_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Reconciliation execution lease was lost")
        return self.get(run_id)  # type: ignore[return-value]

    def attach_verification(
        self,
        run_id: str,
        *,
        verification_run_id: str | None,
        findings_count: int | None = None,
        error: str | None = None,
    ) -> ReconciliationRunDetail:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT summary_json FROM reconciliation_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            summary = json.loads(row["summary_json"])
            summary["verification_run_id"] = verification_run_id
            summary["verification_findings"] = findings_count
            if error:
                summary["verification_error"] = error[:1000]
            connection.execute(
                "UPDATE reconciliation_runs SET summary_json=? WHERE run_id=?",
                (json.dumps(summary, ensure_ascii=False), run_id),
            )
        return self.get_detail(run_id)  # type: ignore[return-value]

    def stats(self) -> dict[str, int | float | str | None]:
        with self._connect() as connection:
            run_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM reconciliation_runs GROUP BY status"
            ).fetchall()
            finding_rows = connection.execute(
                "SELECT severity, COUNT(*) AS count FROM consistency_findings "
                "GROUP BY severity"
            ).fetchall()
            action_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM repair_actions GROUP BY status"
            ).fetchall()
            manual = connection.execute(
                "SELECT COUNT(*) FROM consistency_findings "
                "WHERE repairability='manual_review'"
            ).fetchone()[0]
            latest = connection.execute(
                "SELECT run_id, scan_completed_at FROM reconciliation_runs "
                "WHERE status IN ('planned','completed') "
                "ORDER BY scan_completed_at DESC LIMIT 1"
            ).fetchone()
            latest_findings = {"total": 0, "critical": 0, "manual": 0}
            if latest:
                latest_findings_row = connection.execute(
                    """SELECT COUNT(*) AS total,
                    SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END) AS critical,
                    SUM(CASE WHEN repairability='manual_review' THEN 1 ELSE 0 END) AS manual
                    FROM consistency_findings WHERE run_id=?""",
                    (latest["run_id"],),
                ).fetchone()
                latest_findings = {
                    "total": int(latest_findings_row["total"] or 0),
                    "critical": int(latest_findings_row["critical"] or 0),
                    "manual": int(latest_findings_row["manual"] or 0),
                }
        runs = {row["status"]: int(row["count"]) for row in run_rows}
        findings = {row["severity"]: int(row["count"]) for row in finding_rows}
        actions = {row["status"]: int(row["count"]) for row in action_rows}
        return {
            "runs_total": sum(runs.values()),
            "runs_failed": runs.get("failed", 0),
            "findings_total": sum(findings.values()),
            "critical_findings": findings.get("critical", 0),
            "manual_review_findings": int(manual),
            "repairs_succeeded": actions.get("succeeded", 0),
            "repairs_failed": actions.get("failed", 0),
            "repairs_skipped": actions.get("skipped", 0),
            "latest_findings": latest_findings["total"],
            "latest_critical_findings": latest_findings["critical"],
            "latest_manual_review_findings": latest_findings["manual"],
            "latest_successful_scan_at": (
                latest["scan_completed_at"] if latest else None
            ),
        }

    @staticmethod
    def _update_run(connection: sqlite3.Connection, run: ReconciliationRun) -> None:
        connection.execute(
            """UPDATE reconciliation_runs SET status=?, scope=?,
            verification_of_run_id=?, findings_count=?, actions_count=?, automatic_count=?,
            manual_review_count=?, succeeded_count=?, failed_count=?,
            skipped_count=?, summary_json=?, error=?, scan_completed_at=?,
            execution_started_at=?, execution_lease_until=?, completed_at=?
            WHERE run_id=?""",
            (
                run.status, run.scope, run.verification_of_run_id,
                run.findings_count, run.actions_count,
                run.automatic_count, run.manual_review_count, run.succeeded_count,
                run.failed_count, run.skipped_count,
                json.dumps(run.summary, ensure_ascii=False, default=str), run.error,
                run.scan_completed_at, run.execution_started_at,
                run.execution_lease_until, run.completed_at,
                run.run_id,
            ),
        )

    @staticmethod
    def _run_values(run: ReconciliationRun) -> tuple[object, ...]:
        return (
            run.run_id, run.status, run.scope, run.verification_of_run_id,
            run.findings_count,
            run.actions_count, run.automatic_count, run.manual_review_count,
            run.succeeded_count, run.failed_count, run.skipped_count,
            json.dumps(run.summary, ensure_ascii=False, default=str), run.error,
            run.created_at, run.scan_completed_at, run.execution_started_at,
            run.execution_lease_until, run.completed_at,
        )

    @staticmethod
    def _finding_values(item: ConsistencyFinding) -> tuple[object, ...]:
        return (
            item.finding_id, item.run_id, item.category, item.severity,
            item.object_type, item.object_id, item.source_url, item.patch_id,
            item.version_id, item.job_id,
            json.dumps(item.expected, ensure_ascii=False, default=str),
            json.dumps(item.actual, ensure_ascii=False, default=str), item.reason,
            item.repairability, item.recommended_action, item.created_at,
        )

    @staticmethod
    def _action_values(item: RepairAction) -> tuple[object, ...]:
        return (
            item.action_id, item.run_id, item.finding_id, item.action_type,
            item.target_id, item.status, int(item.requires_confirmation),
            json.dumps(item.before, ensure_ascii=False, default=str),
            json.dumps(item.after, ensure_ascii=False, default=str), item.error,
            item.created_at, item.started_at, item.completed_at,
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> ReconciliationRun:
        return ReconciliationRun.model_validate(
            {**dict(row), "summary": json.loads(row["summary_json"])}
        )

    @staticmethod
    def _finding_from_row(row: sqlite3.Row) -> ConsistencyFinding:
        return ConsistencyFinding.model_validate(
            {
                **dict(row),
                "expected": json.loads(row["expected_json"]),
                "actual": json.loads(row["actual_json"]),
            }
        )

    @staticmethod
    def _action_from_row(row: sqlite3.Row) -> RepairAction:
        return RepairAction.model_validate(
            {
                **dict(row),
                "requires_confirmation": bool(row["requires_confirmation"]),
                "before": json.loads(row["before_json"]),
                "after": json.loads(row["after_json"]),
            }
        )


reconciliation_store = ReconciliationStore()
