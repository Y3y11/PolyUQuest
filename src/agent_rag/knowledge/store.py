"""Bitemporal SQLite history for relationship facts."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agent_rag.config import settings
from agent_rag.knowledge.delta import FactDelta

FactStatus = Literal["active", "retired"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FactVersion(BaseModel):
    fact_version_id: str = Field(default_factory=lambda: f"fact-{uuid.uuid4().hex}")
    fact_key: str
    source_url: str
    source_id: str
    source_name: str
    target_id: str
    target_name: str
    relation_type: str
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    weight: float = 1.0
    source_block_ids: list[str] = Field(default_factory=list)
    status: FactStatus = "active"
    valid_from: str
    valid_to: str | None = None
    transaction_from: str = Field(default_factory=_now)
    transaction_to: str | None = None
    introduced_version_id: str
    retired_version_id: str | None = None


class FactVersionStore:
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
                CREATE TABLE IF NOT EXISTS fact_versions (
                    fact_version_id TEXT PRIMARY KEY,
                    fact_key TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    weight REAL NOT NULL,
                    source_block_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_to TEXT,
                    transaction_from TEXT NOT NULL,
                    transaction_to TEXT,
                    introduced_version_id TEXT NOT NULL,
                    retired_version_id TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_versions_active
                ON fact_versions(fact_key) WHERE status='active';
                CREATE INDEX IF NOT EXISTS idx_fact_versions_url_status
                ON fact_versions(source_url, status, transaction_from DESC);
                CREATE TABLE IF NOT EXISTS fact_version_events (
                    version_id TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(version_id, fact_key, operation)
                );
                CREATE INDEX IF NOT EXISTS idx_fact_events_version
                ON fact_version_events(version_id, operation);
                """
            )

    def apply(
        self,
        *,
        source_url: str,
        version_id: str,
        valid_at: str,
        current_facts: list[FactDelta],
        retired_fact_keys: list[str],
        previous_facts: list[FactDelta] | None = None,
    ) -> tuple[int, int, int]:
        """Atomically close changed versions and insert current versions."""
        added = updated = retired = 0
        now = _now()
        incoming = {item.fact_key: item for item in current_facts}
        previous = {item.fact_key: item for item in (previous_facts or [])}
        with self._connect() as connection:
            existing_events = connection.execute(
                "SELECT operation, COUNT(*) AS count FROM fact_version_events "
                "WHERE version_id=? GROUP BY operation",
                (version_id,),
            ).fetchall()
            if existing_events:
                counts = {row["operation"]: int(row["count"]) for row in existing_events}
                return (
                    counts.get("added", 0),
                    counts.get("updated", 0),
                    counts.get("retired", 0),
                )
            if previous:
                placeholders = ",".join("?" for _ in previous)
                known_keys = {
                    row["fact_key"]
                    for row in connection.execute(
                        "SELECT DISTINCT fact_key FROM fact_versions "
                        f"WHERE fact_key IN ({placeholders})",
                        list(previous),
                    ).fetchall()
                }
            else:
                known_keys = set()
            bootstrapped: set[str] = set()
            for fact_key, fact in previous.items():
                if fact_key in known_keys:
                    continue
                bootstrapped.add(fact_key)
                baseline = FactVersion(
                    fact_key=fact.fact_key,
                    source_url=source_url,
                    source_id=fact.source_id,
                    source_name=fact.source_name,
                    target_id=fact.target_id,
                    target_name=fact.target_name,
                    relation_type=fact.relation_type,
                    description=fact.description,
                    keywords=fact.keywords,
                    weight=fact.weight,
                    source_block_ids=fact.source_block_ids,
                    status="retired",
                    valid_from="unknown",
                    valid_to=valid_at,
                    transaction_from=now,
                    transaction_to=now,
                    introduced_version_id="bootstrap-current-graph",
                    retired_version_id=version_id,
                )
                connection.execute(
                    """
                    INSERT INTO fact_versions VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    self._values(baseline),
                )
            for fact_key in sorted(set(retired_fact_keys) - incoming.keys()):
                changed = connection.execute(
                    """
                    UPDATE fact_versions SET status='retired', valid_to=?,
                        transaction_to=?, retired_version_id=?
                    WHERE fact_key=? AND status='active'
                    """,
                    (valid_at, now, version_id, fact_key),
                ).rowcount
                if changed or fact_key in bootstrapped:
                    retired += 1
                    self._record_event(
                        connection, version_id, fact_key, "retired", now
                    )

            for fact in incoming.values():
                row = connection.execute(
                    "SELECT * FROM fact_versions WHERE fact_key=? AND status='active'",
                    (fact.fact_key,),
                ).fetchone()
                signature = self._signature(fact)
                if row is not None and self._row_signature(row) == signature:
                    continue
                if row is not None:
                    connection.execute(
                        """
                        UPDATE fact_versions SET status='retired', valid_to=?,
                            transaction_to=?, retired_version_id=?
                        WHERE fact_version_id=?
                        """,
                        (valid_at, now, version_id, row["fact_version_id"]),
                    )
                    updated += 1
                    operation = "updated"
                else:
                    operation = "updated" if fact.fact_key in previous else "added"
                    if operation == "updated":
                        updated += 1
                    else:
                        added += 1
                version = FactVersion(
                    fact_key=fact.fact_key,
                    source_url=source_url,
                    source_id=fact.source_id,
                    source_name=fact.source_name,
                    target_id=fact.target_id,
                    target_name=fact.target_name,
                    relation_type=fact.relation_type,
                    description=fact.description,
                    keywords=fact.keywords,
                    weight=fact.weight,
                    source_block_ids=fact.source_block_ids,
                    valid_from=valid_at,
                    introduced_version_id=version_id,
                    transaction_from=now,
                )
                connection.execute(
                    """
                    INSERT INTO fact_versions VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    self._values(version),
                )
                self._record_event(
                    connection, version_id, fact.fact_key, operation, now
                )
        return added, updated, retired

    @staticmethod
    def _record_event(
        connection: sqlite3.Connection,
        version_id: str,
        fact_key: str,
        operation: str,
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO fact_version_events "
            "(version_id, fact_key, operation, created_at) VALUES (?, ?, ?, ?)",
            (version_id, fact_key, operation, created_at),
        )

    def list(
        self, *, status: FactStatus | None = None, source_url: str | None = None,
        limit: int = 100,
    ) -> list[FactVersion]:
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if source_url:
            clauses.append("source_url=?")
            params.append(source_url)
        sql = "SELECT * FROM fact_versions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY transaction_from DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def history(self, fact_key: str) -> list[FactVersion]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fact_versions WHERE fact_key=? "
                "ORDER BY transaction_from DESC", (fact_key,)
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def active_map(self) -> dict[str, FactVersion]:
        """Return the complete active fact ledger keyed by stable fact key."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fact_versions WHERE status='active'"
            ).fetchall()
        return {row["fact_key"]: self._from_row(row) for row in rows}

    def stats(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM fact_versions GROUP BY status"
            ).fetchall()
        counts = {"active": 0, "retired": 0}
        for row in rows:
            counts[row["status"]] = int(row["count"])
        return {**counts, "total_versions": counts["active"] + counts["retired"]}

    @staticmethod
    def _signature(fact: FactDelta) -> tuple[object, ...]:
        return (
            fact.description, tuple(fact.keywords), round(fact.weight, 6),
            tuple(sorted(fact.source_block_ids)),
        )

    @staticmethod
    def _row_signature(row: sqlite3.Row) -> tuple[object, ...]:
        return (
            row["description"], tuple(json.loads(row["keywords_json"])),
            round(float(row["weight"]), 6),
            tuple(sorted(json.loads(row["source_block_ids_json"]))),
        )

    @staticmethod
    def _values(item: FactVersion) -> tuple[object, ...]:
        return (
            item.fact_version_id, item.fact_key, item.source_url, item.source_id,
            item.source_name, item.target_id, item.target_name, item.relation_type,
            item.description, json.dumps(item.keywords, ensure_ascii=False), item.weight,
            json.dumps(item.source_block_ids, ensure_ascii=False), item.status,
            item.valid_from, item.valid_to, item.transaction_from, item.transaction_to,
            item.introduced_version_id, item.retired_version_id,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> FactVersion:
        return FactVersion.model_validate(
            {
                **dict(row),
                "keywords": json.loads(row["keywords_json"]),
                "source_block_ids": json.loads(row["source_block_ids_json"]),
            }
        )


fact_version_store = FactVersionStore()
