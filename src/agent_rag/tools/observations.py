"""Bounded process-local observation and graph-patch stores for the MVP.

Observations are deliberately separate from the durable graph. A fetched page can
support the current answer without mutating Neo4j/Qdrant; publishing is an explicit
second step through ``GraphPatch``.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from agent_rag.tools.schemas import GraphPatch


@dataclass(slots=True)
class ObservationRecord:
    observation_id: str
    run_id: str
    raw_html: str
    metadata: dict[str, Any]
    blocks: list[dict[str, Any]]
    discovered_links: list[dict[str, Any]] = field(default_factory=list)


class ObservationStore:
    def __init__(self, max_records: int = 256):
        self._max_records = max_records
        self._records: OrderedDict[str, ObservationRecord] = OrderedDict()
        self._lock = threading.RLock()

    def put(self, record: ObservationRecord) -> None:
        with self._lock:
            self._records[record.observation_id] = record
            self._records.move_to_end(record.observation_id)
            while len(self._records) > self._max_records:
                self._records.popitem(last=False)

    def get(self, observation_id: str) -> ObservationRecord | None:
        with self._lock:
            record = self._records.get(observation_id)
            if record is not None:
                self._records.move_to_end(observation_id)
            return record


class PatchStore:
    def __init__(self, max_records: int = 256):
        self._max_records = max_records
        self._records: OrderedDict[str, GraphPatch] = OrderedDict()
        self._lock = threading.RLock()

    def put(self, patch: GraphPatch) -> None:
        with self._lock:
            self._records[patch.patch_id] = patch
            self._records.move_to_end(patch.patch_id)
            while len(self._records) > self._max_records:
                self._records.popitem(last=False)

    def get(self, patch_id: str) -> GraphPatch | None:
        with self._lock:
            patch = self._records.get(patch_id)
            return patch.model_copy(deep=True) if patch is not None else None


observation_store = ObservationStore()
patch_store = PatchStore()
