"""Explicit stage/publish boundary for query-driven graph updates."""

from __future__ import annotations

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


def _now() -> str:
    return datetime.now(UTC).isoformat()


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


class PublishPatchTool:
    name = "polyuquest.publish_patch"

    def __init__(
        self,
        observations: ObservationStore = observation_store,
        patches: PatchStore = patch_store,
        graph_store_factory: Callable[[], GraphVectorStore] | None = None,
        embedder: Callable[[list[str]], list[list[float]]] | None = None,
    ):
        self._observations = observations
        self._patches = patches
        self._graph_store_factory = graph_store_factory
        self._embedder = embedder

    def run(self, tool_input: PublishPatchInput) -> PublishPatchOutput:
        patch = self._patches.get(tool_input.patch_id)
        if patch is None:
            raise KeyError(f"Unknown graph patch: {tool_input.patch_id}")
        if patch.status == "published":
            return PublishPatchOutput(patch=patch, read_after_write_ok=True)
        observation = self._observations.get(patch.observation_id)
        if observation is None:
            raise KeyError(f"Observation expired before publish: {patch.observation_id}")

        patch.status = "publishing"
        patch.updated_at = _now()
        self._patches.put(patch)
        graph_store = None
        try:
            if self._graph_store_factory is None:
                from agent_rag.storage.graph_vector_store import GraphVectorStore

                graph_store = GraphVectorStore()
            else:
                graph_store = self._graph_store_factory()
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
            page_text = f"{page.get('title', '')}\n{page.get('meta_description', '')}".strip()
            if self._embedder is None:
                from agent_rag.retrieval._embedding import embed_texts

                embedder = embed_texts
            else:
                embedder = self._embedder
            page_embeddings = embedder([page_text or patch.source_url])
            block_embeddings = embedder(
                [
                    f"{block.get('heading_context', '')}\n{block.get('content', '')}"
                    for block in blocks
                ]
            )
            build_id = f"agent:{patch.run_id}:{patch.patch_id}"
            graph_store.bulk_store_webpages([page], page_embeddings, build_id)
            graph_store.bulk_store_links(links, build_id)
            graph_store.bulk_store_blocks(blocks, block_embeddings, build_id)

            neo_pages = graph_store.neo4j.get_webpages_batch([patch.source_url])
            vector_blocks = graph_store.qdrant.retrieve_vectors(
                "blocks", [block["block_id"] for block in blocks]
            )
            read_ok = patch.source_url in neo_pages and len(vector_blocks) == len(blocks)
            patch.status = "published" if read_ok else "repair_required"
            patch.updated_at = _now()
            if not read_ok:
                patch.error = "Read-after-write verification failed"
            self._patches.put(patch)
            return PublishPatchOutput(
                patch=patch,
                webpages_written=1,
                blocks_written=len(blocks),
                links_written=len(links),
                read_after_write_ok=read_ok,
            )
        except Exception as exc:
            # Neo4j and Qdrant cannot share an atomic transaction. We retain an
            # idempotent patch in repair_required state for a later retry.
            patch.status = "repair_required"
            patch.updated_at = _now()
            patch.error = str(exc)
            self._patches.put(patch)
            raise
        finally:
            if graph_store is not None:
                graph_store.close()
