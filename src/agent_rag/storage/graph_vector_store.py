"""Transactional wrapper ensuring Neo4j + Qdrant consistency."""

from __future__ import annotations

from typing import Any

import structlog

from agent_rag.storage.neo4j_store import Neo4jStore
from agent_rag.storage.qdrant_store import QdrantStore

logger = structlog.get_logger(__name__)


class GraphVectorStore:
    """Coordinates writes to both Neo4j and Qdrant, with basic consistency guarantees."""

    def __init__(self):
        self.neo4j = Neo4jStore()
        self.qdrant = QdrantStore()

    def close(self):
        self.neo4j.close()
        self.qdrant.close()

    def init_all(self):
        self.neo4j.init_schema()
        self.qdrant.init_collections()

    def store_webpage(self, page_meta: dict[str, Any], embedding: list[float]):
        """Write WebPage to Neo4j and its embedding to Qdrant."""
        self.neo4j.upsert_webpage(page_meta)
        self.qdrant.upsert_points(
            "webpages",
            ids=[page_meta["url"]],
            vectors=[embedding],
            payloads=[{
                "url": page_meta["url"],
                "page_type": page_meta.get("page_type", "other"),
                "department": page_meta.get("department", ""),
                "title": page_meta.get("title", ""),
            }],
        )

    def store_block(self, block: dict[str, Any], embedding: list[float]):
        """Write Block to Neo4j and its embedding to Qdrant."""
        self.neo4j.upsert_block(block)
        self.neo4j.upsert_contains(block["url"], block["block_id"], block.get("depth", 0))
        if block.get("parent_block_id"):
            self.neo4j.upsert_parent_block(
                block["block_id"], block["parent_block_id"], block.get("child_index", 0)
            )
        self.qdrant.upsert_points(
            "blocks",
            ids=[block["block_id"]],
            vectors=[embedding],
            payloads=[{
                "block_id": block["block_id"],
                "url": block["url"],
                "heading_context": block.get("heading_context", ""),
                "token_count": block.get("token_count", 0),
                "content": block.get("content", ""),
            }],
        )

    def store_entity(self, entity: dict[str, Any], embedding: list[float]):
        """Write Entity to Neo4j and its embedding to Qdrant."""
        self.neo4j.upsert_entity(entity)
        self.qdrant.upsert_points(
            "entities",
            ids=[entity["entity_id"]],
            vectors=[embedding],
            payloads=[{
                "entity_id": entity["entity_id"],
                "entity_type": entity.get("entity_type", ""),
                "entity_name": entity.get("entity_name", ""),
            }],
        )

    def store_relation_vector(
        self, rel_id: str, embedding: list[float], payload: dict[str, Any]
    ):
        self.qdrant.upsert_points(
            "relations", ids=[rel_id], vectors=[embedding], payloads=[payload]
        )

    def store_topic_keyword(self, keyword: str, embedding: list[float]):
        self.neo4j.upsert_topic_keyword(keyword)
        self.qdrant.upsert_points(
            "topic_keywords",
            ids=[keyword],
            vectors=[embedding],
            payloads=[{"keyword": keyword}],
        )

    # ── Batch write helpers (UNWIND + Qdrant batch) ──────────────

    def bulk_store_webpages(
        self,
        pages: list[dict[str, Any]],
        embeddings: list[list[float]],
        build_id: str,
    ):
        """Batch write WebPage nodes + Qdrant embeddings.

        Pages whose title AND meta_description are both blank are skipped on
        the Qdrant side only — their embeddings would be near-zero noise and
        pollute mode_b's webpage ANN pool (see audit §1.1). Neo4j still gets
        the node so block CONTAINS edges remain valid.
        """
        if not pages:
            return
        self.neo4j.bulk_upsert_webpages(pages, build_id=build_id)

        keep_pages: list[dict[str, Any]] = []
        keep_embs: list[list[float]] = []
        skipped = 0
        for p, emb in zip(pages, embeddings, strict=True):
            has_signal = (p.get("title") or "").strip() or (
                p.get("meta_description") or ""
            ).strip()
            if has_signal:
                keep_pages.append(p)
                keep_embs.append(emb)
            else:
                skipped += 1
        if skipped:
            logger.info(
                "bulk_store_webpages_skip_blank_qdrant",
                skipped=skipped,
                kept=len(keep_pages),
            )
        if not keep_pages:
            return
        self.qdrant.upsert_points(
            "webpages",
            ids=[p["url"] for p in keep_pages],
            vectors=keep_embs,
            payloads=[
                {
                    "url": p["url"],
                    "page_type": p.get("page_type", "other"),
                    "department": p.get("department", ""),
                    "title": p.get("title", ""),
                    "fetched_at": p.get("fetched_at", ""),
                    "content_hash": p.get("content_hash", ""),
                    "source_type": p.get("source_type", "batch_crawl"),
                    "agent_run_id": p.get("agent_run_id", ""),
                    "patch_id": p.get("patch_id", ""),
                    "patch_status": p.get("patch_status", ""),
                    "last_seen_build_id": build_id,
                }
                for p in keep_pages
            ],
        )

    @staticmethod
    def _webpage_payload(page: dict[str, Any], build_id: str) -> dict[str, Any]:
        return {
            "url": page["url"],
            "page_type": page.get("page_type", "other"),
            "department": page.get("department", ""),
            "title": page.get("title", ""),
            "fetched_at": page.get("fetched_at", ""),
            "content_hash": page.get("content_hash", ""),
            "source_type": page.get("source_type", "batch_crawl"),
            "agent_run_id": page.get("agent_run_id", ""),
            "patch_id": page.get("patch_id", ""),
            "patch_status": page.get("patch_status", ""),
            "last_seen_build_id": build_id,
        }

    def bulk_update_webpage_metadata(
        self, pages: list[dict[str, Any]], build_id: str
    ) -> None:
        """Refresh graph and vector payload while retaining the existing vector."""
        if not pages:
            return
        self.neo4j.bulk_upsert_webpages(pages, build_id=build_id)
        vector_pages = [
            page
            for page in pages
            if (page.get("title") or "").strip()
            or (page.get("meta_description") or "").strip()
        ]
        if not vector_pages:
            return
        self.qdrant.update_payloads(
            "webpages",
            {p["url"]: self._webpage_payload(p, build_id) for p in vector_pages},
        )

    def bulk_store_links(self, links: list[dict[str, Any]], build_id: str):
        """Batch write LINKS_TO relationships (auto-stubs missing target WebPages)."""
        self.neo4j.bulk_upsert_links_to(links, build_id=build_id)

    def bulk_add_block_contains_edges(self, pairs: list[tuple[str, str]]) -> int:
        """Extra (source_url -> canonical_block) CONTAINS edges from dedup."""
        return self.neo4j.bulk_add_block_contains_edges(pairs)

    def bulk_store_blocks(
        self,
        blocks: list[dict[str, Any]],
        embeddings: list[list[float]],
        build_id: str,
    ):
        """Batch write Block nodes + CONTAINS/PARENT_BLOCK + Qdrant embeddings."""
        if not blocks:
            return
        self.neo4j.bulk_upsert_blocks(blocks, build_id=build_id)
        self.qdrant.upsert_points(
            "blocks",
            ids=[b["block_id"] for b in blocks],
            vectors=embeddings,
            payloads=[
                {
                    "block_id": b["block_id"],
                    "url": b.get("url", ""),
                    # After block-content dedup, one canonical Block may cover
                    # multiple source URLs (e.g. "Contact Us" appearing on
                    # several programme pages). source_urls lists all, sorted
                    # with canonical first; downstream Provenance Theatre /
                    # multi-page evidence joins read this. Defaults to [url]
                    # for back-compat with callers that don't supply it.
                    "source_urls": b.get("source_urls") or [b.get("url", "")],
                    "heading_context": b.get("heading_context", ""),
                    "token_count": b.get("token_count", 0),
                    "content": b.get("content", ""),
                    "fetched_at": b.get("fetched_at", ""),
                    "content_hash": b.get("content_hash", ""),
                    "source_type": b.get("source_type", "batch_crawl"),
                    "agent_run_id": b.get("agent_run_id", ""),
                    "patch_id": b.get("patch_id", ""),
                    "patch_status": b.get("patch_status", ""),
                    "last_seen_build_id": build_id,
                }
                for b in blocks
            ],
        )

    @staticmethod
    def _block_payload(block: dict[str, Any], build_id: str) -> dict[str, Any]:
        return {
            "block_id": block["block_id"],
            "url": block.get("url", ""),
            "source_urls": block.get("source_urls") or [block.get("url", "")],
            "heading_context": block.get("heading_context", ""),
            "token_count": block.get("token_count", 0),
            "content": block.get("content", ""),
            "fetched_at": block.get("fetched_at", ""),
            "content_hash": block.get("content_hash", ""),
            "source_type": block.get("source_type", "batch_crawl"),
            "agent_run_id": block.get("agent_run_id", ""),
            "patch_id": block.get("patch_id", ""),
            "patch_status": block.get("patch_status", ""),
            "last_seen_build_id": build_id,
        }

    def bulk_store_blocks_incremental(
        self,
        blocks: list[dict[str, Any]],
        vectors_by_id: dict[str, list[float]],
        build_id: str,
    ) -> None:
        """Upsert only blocks selected by a diff plan.

        Blocks with a supplied vector get a Qdrant upsert. Metadata-only blocks
        retain their vector and receive only a payload update.
        """
        if not blocks:
            return
        self.neo4j.bulk_upsert_blocks(blocks, build_id=build_id)
        vector_blocks = [b for b in blocks if b["block_id"] in vectors_by_id]
        metadata_blocks = [b for b in blocks if b["block_id"] not in vectors_by_id]
        if vector_blocks:
            self.qdrant.upsert_points(
                "blocks",
                ids=[b["block_id"] for b in vector_blocks],
                vectors=[vectors_by_id[b["block_id"]] for b in vector_blocks],
                payloads=[self._block_payload(b, build_id) for b in vector_blocks],
            )
        if metadata_blocks:
            self.qdrant.update_payloads(
                "blocks",
                {
                    b["block_id"]: self._block_payload(b, build_id)
                    for b in metadata_blocks
                },
            )

    def bulk_store_entities(
        self,
        entities: list[dict[str, Any]],
        embeddings: list[list[float]],
        build_id: str,
    ):
        if not entities:
            return
        self.neo4j.bulk_upsert_entities(entities, build_id=build_id)
        self.qdrant.upsert_points(
            "entities",
            ids=[e["entity_id"] for e in entities],
            vectors=embeddings,
            payloads=[
                {
                    "entity_id": e["entity_id"],
                    "entity_type": e.get("entity_type", ""),
                    "entity_name": e.get("entity_name", ""),
                    "last_seen_build_id": build_id,
                }
                for e in entities
            ],
        )

    def bulk_store_extracted_from(self, links: list[dict[str, Any]]):
        self.neo4j.bulk_upsert_extracted_from(links)

    def bulk_store_relations(
        self,
        relations: list[dict[str, Any]],
        relation_ids: list[str],
        embeddings: list[list[float]],
        payloads: list[dict[str, Any]],
        build_id: str,
    ):
        """Batch: Neo4j RELATES_TO UNWIND + Qdrant relation vectors."""
        if not relations:
            return
        self.neo4j.bulk_upsert_relates_to(relations, build_id=build_id)
        stamped = [{**p, "last_seen_build_id": build_id} for p in payloads]
        self.qdrant.upsert_points(
            "relations",
            ids=relation_ids,
            vectors=embeddings,
            payloads=stamped,
        )

    def bulk_store_topic_keywords(
        self,
        keywords: list[str],
        embeddings: list[list[float]],
        build_id: str,
    ):
        if not keywords:
            return
        self.neo4j.bulk_upsert_topic_keywords(keywords, build_id=build_id)
        self.qdrant.upsert_points(
            "topic_keywords",
            ids=keywords,
            vectors=embeddings,
            payloads=[
                {"keyword": k, "last_seen_build_id": build_id}
                for k in keywords
            ],
        )

    def bulk_store_has_topic(self, links: list[dict[str, str]]):
        self.neo4j.bulk_upsert_has_topic(links)

    # ── Touch unchanged pages: refresh last_seen_build_id only ───

    def touch_unchanged(self, urls: list[str], build_id: str) -> dict[str, int]:
        """For pages whose content_hash hasn't changed (and were therefore
        skipped by stage_write_base), bump last_seen_build_id on the page,
        its blocks, and entities derived from them — both in Neo4j and in
        Qdrant payloads. Vectors are not touched.

        Returns counters per object class for logging.
        """
        if not urls:
            return {}
        neo_counts = self.neo4j.touch_unchanged_pages(urls, build_id)
        ids_by_collection = self.neo4j.collect_qdrant_ids_for_pages(urls)
        # Webpages: filter to ones that actually exist in Neo4j to avoid
        # touching dangling Qdrant points (caller passes urls that came from
        # the current jsonl, which is the live set).
        qd_counts = {
            "webpages": self.qdrant.touch_payload(
                "webpages", ids_by_collection["webpages"], build_id
            ),
            "blocks": self.qdrant.touch_payload(
                "blocks", ids_by_collection["blocks"], build_id
            ),
            "entities": self.qdrant.touch_payload(
                "entities", ids_by_collection["entities"], build_id
            ),
        }
        # Relations: any RELATES_TO edge where both endpoints are in the
        # touched-entity set has been touched in Neo4j; mirror to Qdrant by
        # recomputing relation_id and set_payload.
        relation_keys = self.neo4j.collect_relation_ids_for_entities(
            ids_by_collection["entities"]
        )
        if relation_keys:
            from agent_rag.kg.profiler import relation_id

            rel_ids = [relation_id(s, t, rt) for s, t, rt in relation_keys]
            qd_counts["relations"] = self.qdrant.touch_payload(
                "relations", rel_ids, build_id
            )
        else:
            qd_counts["relations"] = 0
        return {"neo4j": neo_counts, "qdrant": qd_counts}

    def check_consistency(self) -> dict[str, Any]:
        """Compare Neo4j and Qdrant IDs for blocks and entities."""
        neo_blocks = self.neo4j.get_all_block_ids()
        qd_blocks = self.qdrant.get_all_ids("blocks")
        neo_entities = self.neo4j.get_all_entity_ids()
        qd_entities = self.qdrant.get_all_ids("entities")

        report = {
            "blocks_only_neo4j": len(neo_blocks - qd_blocks),
            "blocks_only_qdrant": len(qd_blocks - neo_blocks),
            "blocks_consistent": neo_blocks == qd_blocks,
            "entities_only_neo4j": len(neo_entities - qd_entities),
            "entities_only_qdrant": len(qd_entities - neo_entities),
            "entities_consistent": neo_entities == qd_entities,
        }
        if report["blocks_consistent"] and report["entities_consistent"]:
            logger.info("consistency_check_passed")
        else:
            logger.warning("consistency_check_failed", **report)
        return report
