"""Read an indexed webpage snapshot for conditional fetch and 304 reuse."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_rag.storage.neo4j_store import Neo4jStore


@dataclass(slots=True)
class PageSnapshot:
    page: dict[str, Any] = field(default_factory=dict)
    blocks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return bool(self.page)


class PageSnapshotTool:
    name = "polyuquest.load_page_snapshot"

    def __init__(self, neo4j_factory: Callable[[], Neo4jStore] = Neo4jStore):
        self._neo4j_factory = neo4j_factory

    def run(self, url: str) -> PageSnapshot:
        store = self._neo4j_factory()
        try:
            page = store.get_webpages_batch([url]).get(url, {})
            if not page:
                return PageSnapshot()
            return PageSnapshot(
                page=page,
                blocks=store.get_blocks_for_webpage(url),
            )
        finally:
            store.close()
