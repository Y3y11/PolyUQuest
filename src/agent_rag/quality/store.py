"""Durable audit store for page-quality decisions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agent_rag.config import settings
from agent_rag.quality.gate import PageQualityDecision, QualityAction


class PageQualityStore:
    def __init__(self, db_path: str | Path):
        path = Path(db_path)
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
                CREATE TABLE IF NOT EXISTS page_quality_decisions (
                    decision_id TEXT PRIMARY KEY,
                    observation_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    action TEXT NOT NULL,
                    evidence_usable INTEGER NOT NULL,
                    score REAL NOT NULL,
                    policy_version TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_page_quality_action_created
                ON page_quality_decisions(action, created_at DESC);
                """
            )

    def put(self, decision: PageQualityDecision) -> PageQualityDecision:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM page_quality_decisions WHERE observation_id=?",
                (decision.observation_id,),
            ).fetchone()
            if existing is not None:
                return self._from_row(existing)
            connection.execute(
                """
                INSERT INTO page_quality_decisions(
                    decision_id, observation_id, run_id, source_url, content_hash,
                    action, evidence_usable, score, policy_version,
                    reasons_json, features_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.observation_id,
                    decision.run_id,
                    decision.source_url,
                    decision.content_hash,
                    decision.action,
                    int(decision.evidence_usable),
                    decision.score,
                    decision.policy_version,
                    json.dumps(decision.reasons, ensure_ascii=False),
                    decision.features.model_dump_json(),
                ),
            )
        return decision

    def get(self, decision_id: str) -> PageQualityDecision | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM page_quality_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list(
        self, action: QualityAction | None = None, limit: int = 50
    ) -> list[PageQualityDecision]:
        sql = "SELECT * FROM page_quality_decisions"
        params: list[object] = []
        if action is not None:
            sql += " WHERE action=?"
            params.append(action)
        sql += " ORDER BY created_at DESC, decision_id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def stats(self) -> dict[str, int | float]:
        result: dict[str, int | float] = {
            "index": 0,
            "evidence_only": 0,
            "discard": 0,
            "total": 0,
            "average_score": 0.0,
        }
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT action, COUNT(*) AS count FROM page_quality_decisions GROUP BY action"
            ).fetchall()
            aggregate = connection.execute(
                "SELECT COUNT(*) AS total, COALESCE(AVG(score), 0) AS average_score "
                "FROM page_quality_decisions"
            ).fetchone()
        for row in rows:
            result[row["action"]] = int(row["count"])
        result["total"] = int(aggregate["total"])
        result["average_score"] = round(float(aggregate["average_score"]), 4)
        return result

    @staticmethod
    def _from_row(row: sqlite3.Row) -> PageQualityDecision:
        return PageQualityDecision.model_validate(
            {
                "decision_id": row["decision_id"],
                "observation_id": row["observation_id"],
                "run_id": row["run_id"],
                "source_url": row["source_url"],
                "content_hash": row["content_hash"],
                "action": row["action"],
                "evidence_usable": bool(row["evidence_usable"]),
                "score": row["score"],
                "policy_version": row["policy_version"],
                "reasons": json.loads(row["reasons_json"]),
                "features": json.loads(row["features_json"]),
            }
        )


page_quality_store = PageQualityStore(settings.agent_ledger_path)
