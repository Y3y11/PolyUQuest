"""Explicit stage/publish boundary for query-driven graph updates."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_rag.tools.observations import (
    ObservationStore,
    PatchStore,
    observation_store,
    patch_store,
)

if TYPE_CHECKING:
    from agent_rag.storage.graph_vector_store import GraphVectorStore
from agent_rag.tools.schemas import (
    GraphPatch,
    PublishPatchInput,
    PublishPatchOutput,
    StagePatchInput,
)

if TYPE_CHECKING:
    from agent_rag.freshness import PageLifecycleStore
    from agent_rag.versioning import PageVersionStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


_URL_LOCKS: dict[str, threading.RLock] = {}
_URL_LOCKS_GUARD = threading.Lock()


def _url_lock(url: str) -> threading.RLock:
    with _URL_LOCKS_GUARD:
        return _URL_LOCKS.setdefault(url, threading.RLock())


class StagePatchTool:
    name = "polyuquest.stage_patch"

    def __init__(
        self,
        observations: ObservationStore = observation_store,
        patches: PatchStore = patch_store,
    ):
        self._observations = observations
        self._patches = patches

    def run(self, tool_input: StagePatchInput) -> GraphPatch:
        observation = self._observations.get(tool_input.observation_id)
        if observation is None:
            raise KeyError(f"Unknown observation: {tool_input.observation_id}")
        if observation.run_id != tool_input.run_id:
            raise ValueError("Observation does not belong to this agent run")
        source_url = observation.metadata.get("url", "")
        content_hash = observation.metadata.get("content_hash", "")
        if not source_url or not content_hash or not observation.blocks:
            raise ValueError("Observation has insufficient provenance or no blocks")
        now = _now()
        patch = GraphPatch(
            patch_id=f"patch-{uuid.uuid4().hex}",
            observation_id=observation.observation_id,
            run_id=observation.run_id,
            source_url=source_url,
            content_hash=content_hash,
            persist_level=tool_input.persist_level,
            created_at=now,
            updated_at=now,
        )
        self._patches.put(patch)
        return patch

    def discard_duplicate(self, patch_id: str) -> bool:
        return self._patches.delete_if_staged(patch_id)


class PublishPatchTool:
    name = "polyuquest.publish_patch"

    def __init__(
        self,
        observations: ObservationStore = observation_store,
        patches: PatchStore = patch_store,
        graph_store_factory: Callable[[], GraphVectorStore] | None = None,
        embedder: Callable[[list[str]], list[list[float]]] | None = None,
        lifecycle_store: PageLifecycleStore | None = None,
        version_store: PageVersionStore | None = None,
    ):
        self._observations = observations
        self._patches = patches
        self._graph_store_factory = graph_store_factory
        self._embedder = embedder
        if lifecycle_store is None:
            from agent_rag.freshness import page_lifecycle_store

            lifecycle_store = page_lifecycle_store
        self._lifecycle_store = lifecycle_store
        if version_store is None:
            from agent_rag.versioning import page_version_store

            version_store = page_version_store
        self._version_store = version_store

    def run(self, tool_input: PublishPatchInput) -> PublishPatchOutput:
        patch = self._patches.get(tool_input.patch_id)
        if patch is None:
            raise KeyError(f"Unknown graph patch: {tool_input.patch_id}")
        if patch.status == "published":
            return PublishPatchOutput(patch=patch, read_after_write_ok=True)
        observation = self._observations.get(patch.observation_id)
        if observation is None:
            raise KeyError(f"Observation expired before publish: {patch.observation_id}")

        with _url_lock(patch.source_url):
            return self._publish_locked(patch, observation)

    def _publish_locked(self, patch: GraphPatch, observation) -> PublishPatchOutput:
        patch.attempts += 1
        patch.last_attempt_at = _now()
        patch.status = "publishing"
        patch.error = None
        patch.updated_at = _now()
        self._patches.put(patch)
        graph_store = None
        version = None
        try:
            if self._graph_store_factory is None:
                from agent_rag.storage.graph_vector_store import GraphVectorStore

                graph_store = GraphVectorStore()
            else:
                graph_store = self._graph_store_factory()
            graph_store.init_all()
            current_page = graph_store.neo4j.get_webpages_batch(
                [patch.source_url]
            ).get(patch.source_url)
            previous_hash = (current_page or {}).get("content_hash") or None
            patch.previous_content_hash = previous_hash
            page = {
                **observation.metadata,
                "url": patch.source_url,
                "patch_id": patch.patch_id,
                "patch_status": "published",
            }
            blocks = [
                {
                    **block,
                    "url": patch.source_url,
                    "content_hash": patch.content_hash,
                    "fetched_at": observation.metadata.get("fetched_at", ""),
                    "source_type": "agent_fetch",
                    "agent_run_id": patch.run_id,
                    "patch_id": patch.patch_id,
                    "patch_status": "published",
                }
                for block in observation.blocks
            ]
            links = [
                {
                    "from_url": patch.source_url,
                    "to_url": link["url"],
                    "link_type": "observed",
                    "anchor_text": link.get("anchor_text", ""),
                    "source_type": "agent_fetch",
                    "agent_run_id": patch.run_id,
                    "patch_id": patch.patch_id,
                }
                for link in observation.discovered_links
                if link.get("url")
            ]
            block_ids = [block["block_id"] for block in blocks]
            existing_blocks = graph_store.neo4j.get_blocks_for_webpage(
                patch.source_url
            )
            existing_block_ids = {
                block.get("block_id", "") for block in existing_blocks
            }
            from agent_rag.versioning import PageVersion, build_block_diff

            new_plan = build_block_diff(
                existing_blocks,
                blocks,
                old_page=current_page,
                new_page=page,
            )
            version = self._version_store.put_planned(
                PageVersion(
                    patch_id=patch.patch_id,
                    observation_id=patch.observation_id,
                    run_id=patch.run_id,
                    source_url=patch.source_url,
                    previous_content_hash=previous_hash,
                    content_hash=patch.content_hash,
                    diff=new_plan,
                )
            )
            plan = version.diff
            version.status = "publishing"
            version.error = None
            self._version_store.save(version)

            lookup_ids = list(
                dict.fromkeys(
                    block_ids + [item.old_id for item in plan.relocated]
                )
            )
            existing_vectors = graph_store.qdrant.retrieve_vectors(
                "blocks", lookup_ids
            )
            page_vector = graph_store.qdrant.retrieve_vectors(
                "webpages", [patch.source_url]
            )
            page_text = (
                f"{page.get('title', '')}\n{page.get('meta_description', '')}".strip()
            )
            page_vector_ok = not page_text or patch.source_url in page_vector

            if current_page is None or not previous_hash:
                patch.operation = "create"
            elif previous_hash != patch.content_hash:
                patch.operation = "update"
            elif (
                existing_block_ids == set(block_ids)
                and all(item in existing_vectors for item in block_ids)
                and page_vector_ok
                and not plan.page_semantic_changed
                and not plan.write_ids
            ):
                patch.operation = "unchanged"
                patch.status = "published"
                patch.updated_at = _now()
                self._patches.put(patch)
                version.status = "published"
                self._version_store.save(version)
                lifecycle = self._register_lifecycle(patch, observation)
                graph_store.neo4j.update_webpage_lifecycle(
                    patch.source_url, lifecycle.model_dump()
                )
                return PublishPatchOutput(
                    patch=patch,
                    version_id=version.version_id,
                    read_after_write_ok=True,
                )
            else:
                patch.operation = "repair"

            if self._embedder is None:
                from agent_rag.retrieval._embedding import embed_texts

                embedder = embed_texts
            else:
                embedder = self._embedder
            build_id = f"agent:{patch.run_id}:{patch.patch_id}"
            must_embed_page = bool(page_text) and (
                current_page is None
                or plan.page_semantic_changed
                or not page_vector_ok
            )
            page_embedding_count = 0
            if must_embed_page:
                page_embeddings = embedder([page_text or patch.source_url])
                page_embedding_count = 1
                graph_store.bulk_store_webpages([page], page_embeddings, build_id)
            else:
                graph_store.bulk_update_webpage_metadata([page], build_id)
            graph_store.bulk_store_links(links, build_id)

            block_by_id = {block["block_id"]: block for block in blocks}
            vectors_by_id: dict[str, list[float]] = {}
            reused_count = 0
            for relocation in plan.relocated:
                if relocation.new_id in existing_vectors:
                    vectors_by_id[relocation.new_id] = existing_vectors[relocation.new_id]
                elif relocation.old_id in existing_vectors:
                    vectors_by_id[relocation.new_id] = existing_vectors[relocation.old_id]
                    reused_count += 1

            # Same-ID modified blocks still have their old vectors; they must be
            # regenerated. Any current block whose vector is missing is repair work,
            # regardless of whether its content or metadata changed in this patch.
            embed_ids = [
                item
                for item in block_ids
                if item in plan.modified_ids
                or (item not in existing_vectors and item not in vectors_by_id)
            ]
            embed_ids = list(dict.fromkeys(embed_ids))
            if embed_ids:
                generated = embedder(
                    [
                        f"{block_by_id[item].get('heading_context', '')}\n"
                        f"{block_by_id[item].get('content', '')}"
                        for item in embed_ids
                    ]
                )
                vectors_by_id.update(dict(zip(embed_ids, generated, strict=True)))

            write_ids = plan.write_ids | set(embed_ids)
            write_blocks = [block for block in blocks if block["block_id"] in write_ids]
            graph_store.bulk_store_blocks_incremental(
                write_blocks, vectors_by_id, build_id
            )

            orphan_block_ids, links_deleted = (
                graph_store.neo4j.reconcile_agent_page_snapshot(
                    patch.source_url,
                    block_ids,
                    [link["to_url"] for link in links],
                )
            )
            if orphan_block_ids:
                graph_store.qdrant.delete_points("blocks", orphan_block_ids)

            neo_pages = graph_store.neo4j.get_webpages_batch([patch.source_url])
            neo_blocks = graph_store.neo4j.get_blocks_for_webpage(patch.source_url)
            vector_blocks = graph_store.qdrant.retrieve_vectors(
                "blocks", block_ids
            )
            vector_pages = graph_store.qdrant.retrieve_vectors(
                "webpages", [patch.source_url]
            )
            read_ok = (
                patch.source_url in neo_pages
                and {block.get("block_id") for block in neo_blocks} == set(block_ids)
                and len(vector_blocks) == len(blocks)
                and (not page_text or patch.source_url in vector_pages)
            )
            patch.status = "published" if read_ok else "repair_required"
            patch.updated_at = _now()
            if not read_ok:
                patch.error = "Read-after-write verification failed"
            self._patches.put(patch)
            if read_ok:
                version.status = "published"
                version.page_embeddings = page_embedding_count
                version.block_embeddings = len(embed_ids)
                version.reused_block_vectors = reused_count
                version.blocks_written = len(write_blocks)
                version.blocks_deleted = len(orphan_block_ids)
                self._version_store.save(version)
                lifecycle = self._register_lifecycle(patch, observation)
                graph_store.neo4j.update_webpage_lifecycle(
                    patch.source_url, lifecycle.model_dump()
                )
            else:
                version.status = "repair_required"
                version.error = patch.error
                self._version_store.save(version)
            return PublishPatchOutput(
                patch=patch,
                version_id=version.version_id,
                webpages_written=1,
                blocks_written=len(write_blocks),
                page_embeddings=page_embedding_count,
                block_embeddings=len(embed_ids),
                blocks_reused=reused_count,
                links_written=len(links),
                blocks_deleted=len(orphan_block_ids),
                links_deleted=links_deleted,
                read_after_write_ok=read_ok,
            )
        except Exception as exc:
            # Neo4j and Qdrant cannot share an atomic transaction. We retain an
            # idempotent patch in repair_required state for a later retry.
            patch.status = "repair_required"
            patch.updated_at = _now()
            patch.error = str(exc)
            self._patches.put(patch)
            if version is not None:
                version.status = "repair_required"
                version.error = str(exc)
                self._version_store.save(version)
            raise
        finally:
            if graph_store is not None:
                graph_store.close()

    def _register_lifecycle(self, patch: GraphPatch, observation):
        return self._lifecycle_store.register_indexed(
            patch.source_url,
            patch.content_hash,
            quality_score=float(observation.metadata.get("quality_score", 0.0)),
        )
