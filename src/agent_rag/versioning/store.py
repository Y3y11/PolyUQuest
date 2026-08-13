"""SQLite version ledger for query-driven page snapshots."""

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
from agent_rag.versioning.diff import BlockDiffPlan

VersionStatus = Literal["planned", "publishing", "published", "repair_required"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PageVersion(BaseModel):
    version_id: str = Field(default_factory=lambda: f"ver-{uuid.uuid4().hex}")
    patch_id: str
    observation_id: str
    run_id: str
    source_url: str
    previous_content_hash: str | None = None
    content_hash: str
    status: VersionStatus = "planned"
    diff: BlockDiffPlan
    page_embeddings: int = 0
    block_embeddings: int = 0
    reused_block_vectors: int = 0
    blocks_written: int = 0
    blocks_deleted: int = 0
    error: str | None = None
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    published_at: str | None = None


class PageVersionStore:
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
                CREATE TABLE IF NOT EXISTS page_versions (
                    version_id TEXT PRIMARY KEY,
                    patch_id TEXT NOT NULL UNIQUE,
                    observation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    previous_content_hash TEXT,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    diff_json TEXT NOT NULL,
                    page_embeddings INTEGER NOT NULL DEFAULT 0,
                    block_embeddings INTEGER NOT NULL DEFAULT 0,
                    reused_block_vectors INTEGER NOT NULL DEFAULT 0,
                    blocks_written INTEGER NOT NULL DEFAULT 0,
                    blocks_deleted INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    published_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_page_versions_url_created
                ON page_versions(source_url, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_page_versions_status_created
                ON page_versions(status, created_at DESC);
                """
            )

    def put_planned(self, version: PageVersion) -> PageVersion:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO page_versions(
                    version_id, patch_id, observation_id, run_id, source_url,
                    previous_content_hash, content_hash, status, diff_json,
                    page_embeddings, block_embeddings, reused_block_vectors,
                    blocks_written, blocks_deleted, error, created_at, updated_at,
                    published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._values(version),
            )
            row = connection.execute(
                "SELECT * FROM page_versions WHERE patch_id=?", (version.patch_id,)
            ).fetchone()
        return self._from_row(row)

    def save(self, version: PageVersion) -> PageVersion:
        version.updated_at = _now()
        if version.status == "published" and version.published_at is None:
            version.published_at = version.updated_at
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE page_versions SET status=?, page_embeddings=?,
                    block_embeddings=?, reused_block_vectors=?, blocks_written=?,
                    blocks_deleted=?, error=?, updated_at=?, published_at=?
                WHERE version_id=?
                """,
                (
                    version.status,
                    version.page_embeddings,
                    version.block_embeddings,
                    version.reused_block_vectors,
                    version.blocks_written,
                    version.blocks_deleted,
                    version.error,
                    version.updated_at,
                    version.published_at,
                    version.version_id,
                ),
            )
        return version

    def get(self, version_id: str) -> PageVersion | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM page_versions WHERE version_id=?", (version_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def get_by_patch(self, patch_id: str) -> PageVersion | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM page_versions WHERE patch_id=?", (patch_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def list(
        self, *, source_url: str | None = None, status: VersionStatus | None = None,
        limit: int = 50,
    ) -> list[PageVersion]:
        clauses: list[str] = []
        params: list[object] = []
        if source_url:
            clauses.append("source_url=?")
            params.append(source_url)
        if status:
            clauses.append("status=?")
            params.append(status)
        sql = "SELECT * FROM page_versions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, version_id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def stats(self) -> dict[str, int | float]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, diff_json, page_embeddings, block_embeddings, "
                "reused_block_vectors FROM page_versions"
            ).fetchall()
        total_new = changed = block_embeds = page_embeds = reused = 0
        published = failed = 0
        for row in rows:
            diff = BlockDiffPlan.model_validate_json(row["diff_json"])
            total_new += diff.new_count
            changed += len(diff.modified_ids) + len(diff.added_ids)
            block_embeds += int(row["block_embeddings"])
            page_embeds += int(row["page_embeddings"])
            reused += int(row["reused_block_vectors"])
            published += row["status"] == "published"
            failed += row["status"] == "repair_required"
        total = len(rows)
        return {
            "total_versions": total,
            "published": published,
            "repair_required": failed,
            "changed_block_ratio": round(changed / total_new, 4) if total_new else 0.0,
            "block_embedding_savings": (
                round(1 - block_embeds / total_new, 4) if total_new else 0.0
            ),
            "page_embedding_savings": (
                round(1 - page_embeds / total, 4) if total else 0.0
            ),
            "relocated_reuse_count": reused,
        }

    @staticmethod
    def _values(version: PageVersion) -> tuple[object, ...]:
        return (
            version.version_id, version.patch_id, version.observation_id,
            version.run_id, version.source_url, version.previous_content_hash,
            version.content_hash, version.status, version.diff.model_dump_json(),
            version.page_embeddings, version.block_embeddings,
            version.reused_block_vectors, version.blocks_written,
            version.blocks_deleted, version.error, version.created_at,
            version.updated_at, version.published_at,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> PageVersion:
        return PageVersion.model_validate(
            {
                **dict(row),
                "diff": json.loads(row["diff_json"]),
            }
        )


page_version_store = PageVersionStore()
