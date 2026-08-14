from __future__ import annotations

from agent_rag.versioning import BlockDiffPlan, PageVersion, PageVersionStore


def test_version_store_is_idempotent_and_reports_savings(tmp_path) -> None:
    store = PageVersionStore(tmp_path / "versions.db")
    plan = BlockDiffPlan(
        old_count=3,
        new_count=4,
        unchanged_ids=["a", "b"],
        modified_ids=["c"],
        added_ids=["d"],
    )
    version = PageVersion(
        patch_id="patch-1",
        observation_id="obs-1",
        run_id="run-1",
        source_url="https://example.org/page",
        previous_content_hash="old",
        content_hash="new",
        diff=plan,
    )
    first = store.put_planned(version)
    duplicate = store.put_planned(version.model_copy(update={"version_id": "other"}))
    assert duplicate.version_id == first.version_id

    first.status = "published"
    first.block_embeddings = 2
    first.page_embeddings = 0
    store.save(first)
    stats = store.stats()
    assert stats["total_versions"] == 1
    assert stats["changed_block_ratio"] == 0.5
    assert stats["block_embedding_savings"] == 0.5
    assert stats["page_embedding_savings"] == 1.0


def test_static_version_stats_route_precedes_dynamic_version_route() -> None:
    from agent_rag.api.routes.indexing_router import router

    paths = [route.path for route in router.routes]
    assert paths.index("/indexing/version-stats") < paths.index(
        "/indexing/versions/{version_id}"
    )
    assert paths.index("/indexing/knowledge-stats") < paths.index(
        "/indexing/versions/{version_id}"
    )
