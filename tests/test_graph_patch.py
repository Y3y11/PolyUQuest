from __future__ import annotations

import unittest

from agent_rag.tools.graph_patch import PublishPatchTool, StagePatchTool
from agent_rag.tools.observations import ObservationRecord, ObservationStore, PatchStore
from agent_rag.tools.schemas import PublishPatchInput, StagePatchInput


class _FakeNeo4j:
    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.blocks: dict[str, list[dict]] = {}
        self.reconciled: list[tuple[str, list[str], list[str]]] = []

    def get_webpages_batch(self, urls):
        return {url: self.pages[url] for url in urls if url in self.pages}

    def get_blocks_for_webpage(self, url):
        return list(self.blocks.get(url, []))

    def reconcile_agent_page_snapshot(self, url, keep_block_ids, keep_link_targets):
        self.reconciled.append((url, keep_block_ids, keep_link_targets))
        old_ids = {item["block_id"] for item in self.blocks.get(url, [])}
        self.blocks[url] = [{"block_id": item} for item in keep_block_ids]
        return sorted(old_ids - set(keep_block_ids)), 1 if old_ids else 0


class _FakeQdrant:
    def __init__(self):
        self.vectors: dict[str, set[str]] = {"webpages": set(), "blocks": set()}
        self.deleted: list[tuple[str, list[str]]] = []

    def retrieve_vectors(self, collection, ids):
        return {item: [1.0] for item in ids if item in self.vectors[collection]}

    def delete_points(self, collection, ids):
        self.deleted.append((collection, list(ids)))
        self.vectors[collection].difference_update(ids)


class _FakeGraphStore:
    def __init__(self):
        self.neo4j = _FakeNeo4j()
        self.qdrant = _FakeQdrant()
        self.initialized = False

    def init_all(self):
        self.initialized = True

    def bulk_store_webpages(self, pages, _embeddings, _build_id):
        for page in pages:
            self.neo4j.pages[page["url"]] = dict(page)
            self.qdrant.vectors["webpages"].add(page["url"])

    def bulk_store_links(self, _links, _build_id):
        pass

    def bulk_store_blocks(self, blocks, _embeddings, _build_id):
        for block in blocks:
            self.qdrant.vectors["blocks"].add(block["block_id"])
            current = self.neo4j.blocks.setdefault(block["url"], [])
            if block["block_id"] not in {item["block_id"] for item in current}:
                current.append({"block_id": block["block_id"]})

    def close(self):
        pass


def _observation_store(content_hash: str = "new", block_id: str = "new-block"):
    observations = ObservationStore()
    observations.put(
        ObservationRecord(
            observation_id="obs-1",
            run_id="run-1",
            raw_html="<p>evidence</p>",
            metadata={
                "url": "https://www.polyu.edu.hk/study/",
                "content_hash": content_hash,
                "title": "Study",
                "fetched_at": "2026-08-13T00:00:00+00:00",
            },
            blocks=[{"block_id": block_id, "content": "evidence"}],
            discovered_links=[{"url": "https://www.polyu.edu.hk/study/pg"}],
        )
    )
    return observations


class StagePatchTests(unittest.TestCase):
    def test_patch_keeps_run_and_source_provenance(self) -> None:
        observations = ObservationStore()
        observations.put(
            ObservationRecord(
                observation_id="obs-1",
                run_id="run-1",
                raw_html="<p>evidence</p>",
                metadata={
                    "url": "https://www.polyu.edu.hk/study/",
                    "content_hash": "abc",
                },
                blocks=[{"block_id": "b1", "content": "evidence"}],
            )
        )
        tool = StagePatchTool(observations=observations, patches=PatchStore())
        patch = tool.run(StagePatchInput(observation_id="obs-1", run_id="run-1"))
        self.assertEqual(patch.status, "staged")
        self.assertEqual(patch.source_url, "https://www.polyu.edu.hk/study/")
        self.assertEqual(patch.content_hash, "abc")

    def test_patch_rejects_cross_run_observation(self) -> None:
        observations = ObservationStore()
        observations.put(
            ObservationRecord(
                observation_id="obs-1",
                run_id="run-1",
                raw_html="<p>evidence</p>",
                metadata={"url": "https://www.polyu.edu.hk/", "content_hash": "abc"},
                blocks=[{"block_id": "b1", "content": "evidence"}],
            )
        )
        tool = StagePatchTool(observations=observations, patches=PatchStore())
        with self.assertRaises(ValueError):
            tool.run(StagePatchInput(observation_id="obs-1", run_id="run-2"))

    def test_duplicate_patch_can_only_be_discarded_while_staged(self) -> None:
        observations = _observation_store()
        patches = PatchStore()
        tool = StagePatchTool(observations=observations, patches=patches)
        patch = tool.run(StagePatchInput(observation_id="obs-1", run_id="run-1"))
        self.assertTrue(tool.discard_duplicate(patch.patch_id))
        self.assertIsNone(patches.get(patch.patch_id))


class PublishPatchTests(unittest.TestCase):
    def _tools(self, graph):
        observations = _observation_store()
        patches = PatchStore()
        stage = StagePatchTool(observations=observations, patches=patches)
        publish = PublishPatchTool(
            observations=observations,
            patches=patches,
            graph_store_factory=lambda: graph,
            embedder=lambda texts: [[1.0] for _ in texts],
        )
        patch = stage.run(StagePatchInput(observation_id="obs-1", run_id="run-1"))
        return publish, patch

    def test_first_snapshot_is_created(self) -> None:
        graph = _FakeGraphStore()
        publish, patch = self._tools(graph)
        result = publish.run(PublishPatchInput(patch_id=patch.patch_id))
        self.assertEqual(result.patch.operation, "create")
        self.assertTrue(result.read_after_write_ok)
        self.assertEqual(result.blocks_written, 1)
        self.assertTrue(graph.initialized)

    def test_unchanged_snapshot_skips_embedding_and_write(self) -> None:
        graph = _FakeGraphStore()
        url = "https://www.polyu.edu.hk/study/"
        graph.neo4j.pages[url] = {"url": url, "content_hash": "new"}
        graph.neo4j.blocks[url] = [{"block_id": "new-block"}]
        graph.qdrant.vectors["webpages"].add(url)
        graph.qdrant.vectors["blocks"].add("new-block")
        publish, patch = self._tools(graph)
        publish._embedder = lambda _texts: (_ for _ in ()).throw(  # noqa: SLF001
            AssertionError("unchanged snapshots must not be embedded")
        )
        result = publish.run(PublishPatchInput(patch_id=patch.patch_id))
        self.assertEqual(result.patch.operation, "unchanged")
        self.assertEqual(result.blocks_written, 0)

    def test_changed_snapshot_removes_orphan_vectors(self) -> None:
        graph = _FakeGraphStore()
        url = "https://www.polyu.edu.hk/study/"
        graph.neo4j.pages[url] = {"url": url, "content_hash": "old"}
        graph.neo4j.blocks[url] = [{"block_id": "old-block"}]
        graph.qdrant.vectors["webpages"].add(url)
        graph.qdrant.vectors["blocks"].add("old-block")
        publish, patch = self._tools(graph)
        result = publish.run(PublishPatchInput(patch_id=patch.patch_id))
        self.assertEqual(result.patch.operation, "update")
        self.assertEqual(result.blocks_deleted, 1)
        self.assertIn(("blocks", ["old-block"]), graph.qdrant.deleted)


if __name__ == "__main__":
    unittest.main()
