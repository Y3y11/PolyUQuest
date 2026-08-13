from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agent_rag.freshness import FreshnessPolicy, PageLifecycleStore
from agent_rag.freshness.worker import FreshnessWorker
from agent_rag.indexing.outbox import IndexOutbox
from agent_rag.quality import PageQualityDecision, PageQualityFeatures
from agent_rag.tools.observations import ObservationRecord, ObservationStore
from agent_rag.tools.schemas import (
    EvidenceGain,
    FetchMetadata,
    FetchOutput,
    GraphPatch,
)
from agent_rag.tools.snapshot import PageSnapshot


def _policy() -> FreshnessPolicy:
    return FreshnessPolicy(
        "test-v1", 1, 24, 240, 2, 0.5, 2, 0.5, 1
    )


class FakeSnapshot:
    def run(self, url):
        return PageSnapshot(
            page={
                "url": url,
                "title": "Guide",
                "etag": "etag-1",
                "last_modified": "Wed, 12 Aug 2026 00:00:00 GMT",
            },
            blocks=[{"block_id": "old", "content": "old"}],
        )


class FakeNeo4j:
    updates = []

    def update_webpage_lifecycle(self, url, lifecycle):
        self.updates.append((url, lifecycle["status"]))

    def close(self):
        pass


class FailingMirrorNeo4j(FakeNeo4j):
    def update_webpage_lifecycle(self, url, lifecycle):
        raise ConnectionError("neo4j mirror unavailable")


class FakeFetch:
    def __init__(self, store, *, result="304"):
        self.store = store
        self.result = result
        self.last_input = None

    async def run(self, tool_input):
        self.last_input = tool_input
        now = datetime.now(UTC).isoformat()
        if self.result == "fail":
            raise ConnectionError("origin unavailable")
        if self.result == "304":
            return FetchOutput(
                observation_id="fetch-304",
                metadata=FetchMetadata(
                    requested_url=str(tool_input.url),
                    final_url=str(tool_input.url),
                    fetched_at=now,
                    content_hash="",
                    status_code=304,
                ),
                not_modified=True,
            )
        content = "Detailed reusable official procedure and requirements. " * 20
        self.store.put(
            ObservationRecord(
                observation_id="obs-new",
                run_id=tool_input.run_id,
                raw_html=f"<main>{content}</main>",
                metadata={
                    "url": str(tool_input.url),
                    "title": "Guide",
                    "content_hash": "hash-new",
                },
                blocks=[{"block_id": "new", "content": content}],
            )
        )
        return FetchOutput(
            observation_id="obs-new",
            metadata=FetchMetadata(
                requested_url=str(tool_input.url),
                final_url=str(tool_input.url),
                title="Guide",
                fetched_at=now,
                content_hash="hash-new",
                status_code=200,
            ),
            block_refs=["new"],
            evidence_gain=EvidenceGain(relevant_blocks=1, total_blocks=1),
        )


class FakeQualityGate:
    def __init__(self, action="index"):
        self.action = action

    def evaluate(self, observation, _fetched, *, require_query_relevance=True):
        return PageQualityDecision(
            observation_id=observation.observation_id,
            run_id=observation.run_id,
            source_url=observation.metadata["url"],
            content_hash=observation.metadata["content_hash"],
            action=self.action,
            evidence_usable=self.action != "discard",
            score=0.9,
            policy_version="test-v1",
            reasons=["test"],
            features=PageQualityFeatures(block_count=1),
        )


class FakeQualityStore:
    def put(self, decision):
        return decision


class FakeStage:
    def run(self, tool_input):
        now = datetime.now(UTC).isoformat()
        return GraphPatch(
            patch_id="patch-refresh",
            observation_id=tool_input.observation_id,
            run_id=tool_input.run_id,
            source_url="https://example.org/guide",
            content_hash="hash-new",
            created_at=now,
            updated_at=now,
        )

    def discard_duplicate(self, _patch_id):
        return True


class FreshnessWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "ledger.sqlite3"
        self.lifecycle = PageLifecycleStore(self.path, policy=_policy())
        self.outbox = IndexOutbox(self.path)
        self.observations = ObservationStore()
        self.url = "https://example.org/guide"
        self.lifecycle.register_indexed(self.url, "hash-old")
        self.lifecycle.refresh_now(self.url)
        FakeNeo4j.updates = []

    def tearDown(self):
        self.temp_dir.cleanup()

    def worker(self, fetch, quality_action="index"):
        return FreshnessWorker(
            self.lifecycle,
            self.outbox,
            self.observations,
            fetch_tool=fetch,
            snapshot_tool=FakeSnapshot(),
            stage_tool=FakeStage(),
            quality_gate=FakeQualityGate(quality_action),
            quality_store=FakeQualityStore(),
            neo4j_factory=FakeNeo4j,
            worker_id="freshness-test",
        )

    async def test_304_reschedules_without_index_job(self):
        fetch = FakeFetch(self.observations, result="304")
        result = await self.worker(fetch).process_once()
        self.assertEqual(result.status, "active")
        self.assertEqual(result.unchanged_count, 1)
        self.assertEqual(self.outbox.stats()["pending"], 0)
        self.assertEqual(fetch.last_input.if_none_match, "etag-1")

    async def test_changed_page_enqueues_index_job(self):
        result = await self.worker(
            FakeFetch(self.observations, result="changed")
        ).process_once()
        self.assertEqual(result.status, "indexing")
        self.assertEqual(result.pending_content_hash, "hash-new")
        self.assertEqual(self.outbox.stats()["pending"], 1)

    async def test_failed_fetch_uses_retry(self):
        result = await self.worker(
            FakeFetch(self.observations, result="fail")
        ).process_once()
        self.assertEqual(result.status, "retry")
        self.assertEqual(result.consecutive_failures, 1)

    async def test_low_quality_changed_page_is_quarantined(self):
        result = await self.worker(
            FakeFetch(self.observations, result="changed"),
            quality_action="evidence_only",
        ).process_once()
        self.assertEqual(result.status, "quarantined")
        self.assertEqual(self.outbox.stats()["pending"], 0)

    async def test_graph_mirror_failure_does_not_revert_schedule(self):
        worker = self.worker(FakeFetch(self.observations, result="304"))
        worker.neo4j_factory = FailingMirrorNeo4j
        result = await worker.process_once()
        self.assertEqual(result.status, "active")
        self.assertEqual(self.lifecycle.get(self.url).unchanged_count, 1)


if __name__ == "__main__":
    unittest.main()
