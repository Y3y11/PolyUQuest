from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_rag.tools.graph_patch import PublishPatchTool, StagePatchTool
from agent_rag.tools.observations import ObservationRecord, ObservationStore, PatchStore
from agent_rag.tools.schemas import PublishPatchInput, StagePatchInput
from agent_rag.versioning import PageVersionStore


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

    def update_webpage_lifecycle(self, url, lifecycle):
        self.pages[url].update(lifecycle)


class _FakeQdrant:
    def __init__(self):
        self.vectors: dict[str, dict[str, list[float]]] = {
            "webpages": {}, "blocks": {}
        }
        self.deleted: list[tuple[str, list[str]]] = []

    def retrieve_vectors(self, collection, ids):
        return {
            item: self.vectors[collection][item]
            for item in ids if item in self.vectors[collection]
        }

    def delete_points(self, collection, ids):
        self.deleted.append((collection, list(ids)))
        for item in ids:
            self.vectors[collection].pop(item, None)


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
            self.qdrant.vectors["webpages"][page["url"]] = [1.0]

    def bulk_update_webpage_metadata(self, pages, _build_id):
        for page in pages:
            self.neo4j.pages[page["url"]] = dict(page)

    def bulk_store_links(self, _links, _build_id):
        pass

    def bulk_store_blocks_incremental(self, blocks, vectors_by_id, _build_id):
        for block in blocks:
            if block["block_id"] in vectors_by_id:
                self.qdrant.vectors["blocks"][block["block_id"]] = vectors_by_id[
                    block["block_id"]
                ]
            current = self.neo4j.blocks.setdefault(block["url"], [])
            current[:] = [
                item for item in current if item["block_id"] != block["block_id"]
            ]
            current.append(dict(block))

    def close(self):
        pass


def _observation_store(
    content_hash: str = "new",
    block_id: str = "new-block",
    content: str = "evidence",
):
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
            blocks=[{"block_id": block_id, "content": content}],
            discovered_links=[{"url": "https://www.polyu.edu.hk/study/pg"}],
        )
    )
    return observations


class _FakeLifecycleStore:
    def __init__(self):
        self.registrations = []

    def register_indexed(self, source_url, content_hash, **kwargs):
        self.registrations.append((source_url, content_hash, kwargs))
        return type(
            "Target",
            (),
            {"model_dump": lambda self: {"status": "active"}},
        )()


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
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.version_store = PageVersionStore(Path(self._tmp.name) / "versions.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _tools(self, graph):
        observations = _observation_store()
        patches = PatchStore()
        stage = StagePatchTool(observations=observations, patches=patches)
        lifecycle = _FakeLifecycleStore()
        publish = PublishPatchTool(
            observations=observations,
            patches=patches,
            graph_store_factory=lambda: graph,
            embedder=lambda texts: [[1.0] for _ in texts],
            lifecycle_store=lifecycle,
            version_store=self.version_store,
        )
        patch = stage.run(StagePatchInput(observation_id="obs-1", run_id="run-1"))
        return publish, patch, lifecycle

    def test_first_snapshot_is_created(self) -> None:
        graph = _FakeGraphStore()
        publish, patch, lifecycle = self._tools(graph)
        result = publish.run(PublishPatchInput(patch_id=patch.patch_id))
        self.assertEqual(result.patch.operation, "create")
        self.assertTrue(result.read_after_write_ok)
        self.assertEqual(result.blocks_written, 1)
        self.assertTrue(graph.initialized)
        self.assertEqual(len(lifecycle.registrations), 1)

    def test_unchanged_snapshot_skips_embedding_and_write(self) -> None:
        graph = _FakeGraphStore()
        url = "https://www.polyu.edu.hk/study/"
        graph.neo4j.pages[url] = {
            "url": url,
            "content_hash": "new",
            "title": "Study",
        }
        graph.neo4j.blocks[url] = [
            {"block_id": "new-block", "content": "evidence"}
        ]
        graph.qdrant.vectors["webpages"][url] = [1.0]
        graph.qdrant.vectors["blocks"]["new-block"] = [1.0]
        publish, patch, lifecycle = self._tools(graph)
        publish._embedder = lambda _texts: (_ for _ in ()).throw(  # noqa: SLF001
            AssertionError("unchanged snapshots must not be embedded")
        )
        result = publish.run(PublishPatchInput(patch_id=patch.patch_id))
        self.assertEqual(result.patch.operation, "unchanged")
        self.assertEqual(result.blocks_written, 0)
        self.assertEqual(len(lifecycle.registrations), 1)

    def test_changed_snapshot_removes_orphan_vectors(self) -> None:
        graph = _FakeGraphStore()
        url = "https://www.polyu.edu.hk/study/"
        graph.neo4j.pages[url] = {"url": url, "content_hash": "old"}
        graph.neo4j.blocks[url] = [{"block_id": "old-block"}]
        graph.qdrant.vectors["webpages"][url] = [1.0]
        graph.qdrant.vectors["blocks"]["old-block"] = [1.0]
        publish, patch, lifecycle = self._tools(graph)
        result = publish.run(PublishPatchInput(patch_id=patch.patch_id))
        self.assertEqual(result.patch.operation, "update")
        self.assertEqual(result.blocks_deleted, 1)
        self.assertIn(("blocks", ["old-block"]), graph.qdrant.deleted)
        self.assertEqual(len(lifecycle.registrations), 1)

    def test_one_modified_block_only_embeds_that_block(self) -> None:
        graph = _FakeGraphStore()
        url = "https://www.polyu.edu.hk/study/"
        graph.neo4j.pages[url] = {
            "url": url,
            "content_hash": "old",
            "title": "Study",
        }
        graph.neo4j.blocks[url] = [
            {"block_id": "new-block", "content": "old evidence"},
            {"block_id": "stable", "content": "unchanged"},
        ]
        graph.qdrant.vectors["webpages"][url] = [1.0]
        graph.qdrant.vectors["blocks"].update(
            {"new-block": [1.0], "stable": [1.0]}
        )
        observations = _observation_store(content="new evidence")
        observations.get("obs-1").blocks.append(  # type: ignore[union-attr]
            {"block_id": "stable", "content": "unchanged"}
        )
        patches = PatchStore()
        patch = StagePatchTool(observations, patches).run(
            StagePatchInput(observation_id="obs-1", run_id="run-1")
        )
        calls: list[list[str]] = []

        def embed(texts):
            calls.append(texts)
            return [[2.0] for _ in texts]

        result = PublishPatchTool(
            observations=observations,
            patches=patches,
            graph_store_factory=lambda: graph,
            embedder=embed,
            lifecycle_store=_FakeLifecycleStore(),
            version_store=self.version_store,
        ).run(PublishPatchInput(patch_id=patch.patch_id))

        assert result.block_embeddings == 1
        assert result.page_embeddings == 0
        assert len(calls) == 1
        assert calls[0] == ["\nnew evidence"]

    def test_same_hash_with_changed_page_semantics_is_not_skipped(self) -> None:
        graph = _FakeGraphStore()
        url = "https://www.polyu.edu.hk/study/"
        graph.neo4j.pages[url] = {
            "url": url,
            "content_hash": "new",
            "title": "Old title",
        }
        graph.neo4j.blocks[url] = [
            {"block_id": "new-block", "content": "evidence"}
        ]
        graph.qdrant.vectors["webpages"][url] = [1.0]
        graph.qdrant.vectors["blocks"]["new-block"] = [1.0]
        publish, patch, _lifecycle = self._tools(graph)
        result = publish.run(PublishPatchInput(patch_id=patch.patch_id))
        assert result.patch.operation == "repair"
        assert result.page_embeddings == 1


if __name__ == "__main__":
    unittest.main()
