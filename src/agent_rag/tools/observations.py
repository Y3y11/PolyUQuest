"""Observation and graph-patch ledger.

Unit tests may use the bounded in-memory implementation by omitting ``db_path``.
The process-wide stores use SQLite so an accepted fetch and its patch state survive
an API restart and can be repaired idempotently.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import zlib
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_rag.config import settings
from agent_rag.tools.schemas import GraphPatch


@dataclass(slots=True)
class ObservationRecord:
    observation_id: str
    run_id: str
    raw_html: str
    metadata: dict[str, Any]
    blocks: list[dict[str, Any]]
    discovered_links: list[dict[str, Any]] = field(default_factory=list)


class _SQLiteLedger:
    def __init__(self, db_path: str | Path):
        path = Path(db_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
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
                    CREATE TABLE IF NOT EXISTS observations (
                        observation_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        source_url TEXT NOT NULL DEFAULT '',
                        content_hash TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        raw_html_zlib BLOB NOT NULL,
                        metadata_json TEXT NOT NULL,
                        blocks_json TEXT NOT NULL,
                        links_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS graph_patches (
                        patch_id TEXT PRIMARY KEY,
                        observation_id TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        status TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_graph_patches_status_updated
                    ON graph_patches(status, updated_at);
                    """
                )
                connection.commit()
            finally:
                connection.close()
            self._initialized = True


class ObservationStore:
    def __init__(self, max_records: int = 256, db_path: str | Path | None = None):
        self._max_records = max_records
        self._records: OrderedDict[str, ObservationRecord] = OrderedDict()
        self._lock = threading.RLock()
        self._ledger = _SQLiteLedger(db_path) if db_path is not None else None

    def put(self, record: ObservationRecord) -> None:
        if self._ledger is not None:
            raw_html = zlib.compress(record.raw_html.encode("utf-8"), level=6)
            metadata_json = json.dumps(record.metadata, ensure_ascii=False, default=str)
            blocks_json = json.dumps(record.blocks, ensure_ascii=False, default=str)
            links_json = json.dumps(
                record.discovered_links, ensure_ascii=False, default=str
            )
            with self._ledger.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO observations(
                        observation_id, run_id, source_url, content_hash,
                        raw_html_zlib, metadata_json, blocks_json, links_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(observation_id) DO UPDATE SET
                        run_id=excluded.run_id,
                        source_url=excluded.source_url,
                        content_hash=excluded.content_hash,
                        raw_html_zlib=excluded.raw_html_zlib,
                        metadata_json=excluded.metadata_json,
                        blocks_json=excluded.blocks_json,
                        links_json=excluded.links_json
                    """,
                    (
                        record.observation_id,
                        record.run_id,
                        str(record.metadata.get("url", "")),
                        str(record.metadata.get("content_hash", "")),
                        raw_html,
                        metadata_json,
                        blocks_json,
                        links_json,
                    ),
                )
            return
        with self._lock:
            self._records[record.observation_id] = record
            self._records.move_to_end(record.observation_id)
            while len(self._records) > self._max_records:
                self._records.popitem(last=False)

    def get(self, observation_id: str) -> ObservationRecord | None:
        if self._ledger is not None:
            with self._ledger.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM observations WHERE observation_id = ?",
                    (observation_id,),
                ).fetchone()
            if row is None:
                return None
            return ObservationRecord(
                observation_id=row["observation_id"],
                run_id=row["run_id"],
                raw_html=zlib.decompress(row["raw_html_zlib"]).decode("utf-8"),
                metadata=json.loads(row["metadata_json"]),
                blocks=json.loads(row["blocks_json"]),
                discovered_links=json.loads(row["links_json"]),
            )
        with self._lock:
            record = self._records.get(observation_id)
            if record is not None:
                self._records.move_to_end(observation_id)
            return record


class PatchStore:
    def __init__(self, max_records: int = 256, db_path: str | Path | None = None):
        self._max_records = max_records
        self._records: OrderedDict[str, GraphPatch] = OrderedDict()
        self._lock = threading.RLock()
        self._ledger = _SQLiteLedger(db_path) if db_path is not None else None

    def put(self, patch: GraphPatch) -> None:
        if self._ledger is not None:
            with self._ledger.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO graph_patches(
                        patch_id, observation_id, source_url, status,
                        updated_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(patch_id) DO UPDATE SET
                        observation_id=excluded.observation_id,
                        source_url=excluded.source_url,
                        status=excluded.status,
                        updated_at=excluded.updated_at,
                        payload_json=excluded.payload_json
                    """,
                    (
                        patch.patch_id,
                        patch.observation_id,
                        patch.source_url,
                        patch.status,
                        patch.updated_at,
                        patch.model_dump_json(),
                    ),
                )
            return
        with self._lock:
            self._records[patch.patch_id] = patch.model_copy(deep=True)
            self._records.move_to_end(patch.patch_id)
            while len(self._records) > self._max_records:
                self._records.popitem(last=False)

    def get(self, patch_id: str) -> GraphPatch | None:
        if self._ledger is not None:
            with self._ledger.connect() as connection:
                row = connection.execute(
                    "SELECT payload_json FROM graph_patches WHERE patch_id = ?",
                    (patch_id,),
                ).fetchone()
            return GraphPatch.model_validate_json(row[0]) if row is not None else None
        with self._lock:
            patch = self._records.get(patch_id)
            return patch.model_copy(deep=True) if patch is not None else None

    def list_by_status(
        self, statuses: Iterable[str], limit: int = 25
    ) -> list[GraphPatch]:
        wanted = tuple(dict.fromkeys(statuses))
        if not wanted or limit <= 0:
            return []
        if self._ledger is not None:
            placeholders = ",".join("?" for _ in wanted)
            with self._ledger.connect() as connection:
                rows = connection.execute(
                    f"""SELECT payload_json FROM graph_patches
                    WHERE status IN ({placeholders})
                    ORDER BY updated_at ASC LIMIT ?""",  # noqa: S608
                    (*wanted, limit),
                ).fetchall()
            return [GraphPatch.model_validate_json(row[0]) for row in rows]
        with self._lock:
            return [
                patch.model_copy(deep=True)
                for patch in self._records.values()
                if patch.status in wanted
            ][:limit]

    def delete_if_staged(self, patch_id: str) -> bool:
        """Remove a duplicate Patch only while it has never been published."""
        if self._ledger is not None:
            with self._ledger.connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM graph_patches WHERE patch_id=? AND status='staged'",
                    (patch_id,),
                )
            return cursor.rowcount == 1
        with self._lock:
            patch = self._records.get(patch_id)
            if patch is None or patch.status != "staged":
                return False
            del self._records[patch_id]
            return True


_ledger_path = settings.agent_ledger_path
observation_store = ObservationStore(max_records=2048, db_path=_ledger_path)
patch_store = PatchStore(max_records=2048, db_path=_ledger_path)
