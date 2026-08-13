"""Neo4j storage: heterogeneous graph schema, CRUD, and queries."""

from __future__ import annotations

from typing import Any

import structlog
from neo4j import GraphDatabase

from agent_rag.config import settings

logger = structlog.get_logger(__name__)


class Neo4jStore:
    def __init__(self):
        self._driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )

    def close(self):
        self._driver.close()

    def __enter__(self):
        return self

    def __exit__(self, *args: Any):
        self.close()

    # ── Schema Setup ──────────────────────────────────────────────

    def init_schema(self):
        """Create constraints and indexes for all node types."""
        constraints = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (w:WebPage) REQUIRE w.url IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (b:Block) REQUIRE b.block_id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (e:Entity) REQUIRE e.entity_id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (t:TopicKeyword) REQUIRE t.keyword IS UNIQUE",
        ]
        indexes = [
            "CREATE INDEX IF NOT EXISTS FOR (w:WebPage) ON (w.page_type)",
            "CREATE INDEX IF NOT EXISTS FOR (w:WebPage) ON (w.department)",
            "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.entity_type)",
            "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.entity_name)",
            "CREATE INDEX IF NOT EXISTS FOR (b:Block) ON (b.url)",
        ]
        with self._driver.session() as session:
            for stmt in constraints + indexes:
                session.run(stmt)
        logger.info("neo4j_schema_initialized")

    # ── WebPage CRUD ──────────────────────────────────────────────

    def upsert_webpage(self, page: dict[str, Any]):
        query = """
        MERGE (w:WebPage {url: $url})
        SET w.title = $title,
            w.meta_description = $meta_description,
            w.department = $department,
            w.page_type = $page_type,
            w.last_crawled = $crawled_at
        """
        with self._driver.session() as session:
            session.run(query, **page)

    def bulk_upsert_webpages(
        self,
        pages: list[dict[str, Any]],
        build_id: str,
        batch_size: int = 500,
    ):
        """Batch upsert WebPage nodes via UNWIND. Each page must contain:
        url, title, meta_description, department, page_type, crawled_at.
        Missing keys default to empty strings."""
        if not pages:
            return
        query = """
        UNWIND $rows AS p
        MERGE (w:WebPage {url: p.url})
        ON CREATE SET w.created_build_id = $build_id
        SET w.title = p.title,
            w.meta_description = p.meta_description,
            w.department = p.department,
            w.page_type = p.page_type,
            w.last_crawled = p.crawled_at,
            w.fetched_at = p.fetched_at,
            w.content_hash = p.content_hash,
            w.etag = p.etag,
            w.last_modified = p.last_modified,
            w.source_type = p.source_type,
            w.agent_run_id = p.agent_run_id,
            w.patch_id = p.patch_id,
            w.patch_status = p.patch_status,
            w.last_seen_build_id = $build_id
        """
        rows = [
            {
                "url": p["url"],
                "title": p.get("title", ""),
                "meta_description": p.get("meta_description", ""),
                "department": p.get("department", ""),
                "page_type": p.get("page_type", "other"),
                "crawled_at": p.get("crawled_at", ""),
                "fetched_at": p.get("fetched_at", ""),
                "content_hash": p.get("content_hash", ""),
                "etag": p.get("etag", ""),
                "last_modified": p.get("last_modified", ""),
                "source_type": p.get("source_type", "batch_crawl"),
                "agent_run_id": p.get("agent_run_id", ""),
                "patch_id": p.get("patch_id", ""),
                "patch_status": p.get("patch_status", ""),
            }
            for p in pages
        ]
        with self._driver.session() as session:
            for i in range(0, len(rows), batch_size):
                session.run(query, rows=rows[i : i + batch_size], build_id=build_id)

    def upsert_links_to(self, from_url: str, to_url: str, link_type: str, anchor_text: str = ""):
        query = """
        MATCH (a:WebPage {url: $from_url})
        MATCH (b:WebPage {url: $to_url})
        MERGE (a)-[r:LINKS_TO {link_type: $link_type}]->(b)
        SET r.anchor_text = $anchor_text
        """
        with self._driver.session() as session:
            session.run(
                query, from_url=from_url, to_url=to_url,
                link_type=link_type, anchor_text=anchor_text,
            )

    def bulk_upsert_links_to(
        self,
        links: list[dict[str, Any]],
        build_id: str,
        batch_size: int = 1000,
    ):
        """Batch upsert LINKS_TO edges. Each link: from_url, to_url, link_type, anchor_text.
        Ensures target WebPage nodes exist (stub with empty title/meta)."""
        if not links:
            return
        # Stub WebPages for unknown link targets get build_id so they aren't
        # immediately classified as orphans by the next cleanup pass.
        stub_query = """
        UNWIND $rows AS u
        MERGE (w:WebPage {url: u.url})
        ON CREATE SET w.title = '', w.meta_description = '',
                      w.department = '', w.page_type = 'other',
                      w.last_crawled = '',
                      w.created_build_id = $build_id,
                      w.source_type = u.source_type,
                      w.agent_run_id = u.agent_run_id,
                      w.patch_id = u.patch_id
        SET w.last_seen_build_id = $build_id
        """
        edge_query = """
        UNWIND $rows AS l
        MATCH (a:WebPage {url: l.from_url})
        MATCH (b:WebPage {url: l.to_url})
        MERGE (a)-[r:LINKS_TO {link_type: l.link_type}]->(b)
        ON CREATE SET r.created_build_id = $build_id
        SET r.anchor_text = l.anchor_text,
            r.source_type = l.source_type,
            r.agent_run_id = l.agent_run_id,
            r.patch_id = l.patch_id,
            r.last_seen_build_id = $build_id
        """
        target_rows: dict[str, dict[str, str]] = {}
        for link in links:
            to_url = link.get("to_url")
            if not to_url:
                continue
            target_rows[to_url] = {
                "url": to_url,
                "source_type": link.get("source_type", "batch_crawl"),
                "agent_run_id": link.get("agent_run_id", ""),
                "patch_id": link.get("patch_id", ""),
            }
        targets = list(target_rows.values())
        with self._driver.session() as session:
            for i in range(0, len(targets), batch_size):
                session.run(
                    stub_query,
                    rows=targets[i : i + batch_size],
                    build_id=build_id,
                )
            rows = [
                {
                    **link,
                    "source_type": link.get("source_type", "batch_crawl"),
                    "agent_run_id": link.get("agent_run_id", ""),
                    "patch_id": link.get("patch_id", ""),
                }
                for link in links
            ]
            for i in range(0, len(rows), batch_size):
                session.run(edge_query, rows=rows[i : i + batch_size], build_id=build_id)

    # ── Block CRUD ────────────────────────────────────────────────

    def upsert_block(self, block: dict[str, Any]):
        query = """
        MERGE (b:Block {block_id: $block_id})
        SET b.content = $content,
            b.html_tag_path = $html_tag_path,
            b.heading_context = $heading_context,
            b.token_count = $token_count,
            b.url = $url,
            b.depth = $depth
        """
        with self._driver.session() as session:
            session.run(query, **block)

    def upsert_contains(self, url: str, block_id: str, depth: int):
        query = """
        MATCH (w:WebPage {url: $url})
        MATCH (b:Block {block_id: $block_id})
        MERGE (w)-[r:CONTAINS]->(b)
        SET r.depth = $depth
        """
        with self._driver.session() as session:
            session.run(query, url=url, block_id=block_id, depth=depth)

    def upsert_parent_block(self, child_id: str, parent_id: str, child_index: int):
        query = """
        MATCH (c:Block {block_id: $child_id})
        MATCH (p:Block {block_id: $parent_id})
        MERGE (c)-[r:PARENT_BLOCK]->(p)
        SET r.child_index = $child_index
        """
        with self._driver.session() as session:
            session.run(query, child_id=child_id, parent_id=parent_id, child_index=child_index)

    def bulk_upsert_blocks(
        self,
        blocks: list[dict[str, Any]],
        build_id: str,
        batch_size: int = 500,
    ):
        """Batch upsert Block nodes + CONTAINS + PARENT_BLOCK relationships.

        Block creation and the CONTAINS edge are emitted in a SINGLE Cypher
        statement so the two cannot diverge — previously they ran as two
        separate session.run calls, and an interrupt in between produced
        Block nodes without CONTAINS (ghost blocks with no source URL).
        Callers must ensure WebPage{url=b.url} already exists; this query
        does not stub it.
        """
        if not blocks:
            return
        block_and_contains_query = """
        UNWIND $rows AS b
        MERGE (n:Block {block_id: b.block_id})
        ON CREATE SET n.created_build_id = $build_id
        SET n.content = b.content,
            n.html_tag_path = b.html_tag_path,
            n.heading_context = b.heading_context,
            n.token_count = b.token_count,
            n.url = b.url,
            n.depth = b.depth,
            n.fetched_at = b.fetched_at,
            n.content_hash = b.content_hash,
            n.source_type = b.source_type,
            n.agent_run_id = b.agent_run_id,
            n.patch_id = b.patch_id,
            n.patch_status = b.patch_status,
            n.last_seen_build_id = $build_id
        WITH n, b
        MATCH (w:WebPage {url: b.url})
        MERGE (w)-[r:CONTAINS]->(n)
        SET r.depth = b.depth
        """
        parent_query = """
        UNWIND $rows AS b
        MATCH (c:Block {block_id: b.block_id})
        MATCH (p:Block {block_id: b.parent_block_id})
        MERGE (c)-[r:PARENT_BLOCK]->(p)
        SET r.child_index = b.child_index
        """
        block_rows = [
            {
                "block_id": b["block_id"],
                "content": b.get("content", ""),
                "html_tag_path": b.get("html_tag_path", ""),
                "heading_context": b.get("heading_context", ""),
                "token_count": b.get("token_count", 0),
                "url": b.get("url", ""),
                "depth": b.get("depth", 0),
                "fetched_at": b.get("fetched_at", ""),
                "content_hash": b.get("content_hash", ""),
                "source_type": b.get("source_type", "batch_crawl"),
                "agent_run_id": b.get("agent_run_id", ""),
                "patch_id": b.get("patch_id", ""),
                "patch_status": b.get("patch_status", ""),
            }
            for b in blocks
        ]
        parent_rows = [
            {
                "block_id": b["block_id"],
                "parent_block_id": b["parent_block_id"],
                "child_index": b.get("child_index", 0),
            }
            for b in blocks
            if b.get("parent_block_id")
        ]
        with self._driver.session() as session:
            for i in range(0, len(block_rows), batch_size):
                session.run(
                    block_and_contains_query,
                    rows=block_rows[i : i + batch_size],
                    build_id=build_id,
                )
            for i in range(0, len(parent_rows), batch_size):
                session.run(parent_query, rows=parent_rows[i : i + batch_size])

    def bulk_add_block_contains_edges(
        self,
        pairs: list[tuple[str, str]],
        batch_size: int = 500,
    ) -> int:
        """Add extra CONTAINS edges from non-canonical source URLs to a
        canonical Block.

        After cross-URL content dedup, one canonical Block represents content
        that originally appeared on multiple URLs. The Block's own `url`
        property stays as the canonical URL (for back-compat with single-URL
        consumers), but every source page that originally contained that
        content gets a CONTAINS edge to the canonical Block so that
        page-scoped retrieval ("blocks under this URL") still returns it.

        Pairs are (source_url, canonical_block_id). The source WebPage must
        already exist; this query does not stub it.

        Returns the number of pairs processed.
        """
        if not pairs:
            return 0
        query = """
        UNWIND $rows AS p
        MATCH (w:WebPage {url: p.url})
        MATCH (n:Block {block_id: p.block_id})
        MERGE (w)-[r:CONTAINS]->(n)
        ON CREATE SET r.depth = coalesce(n.depth, 0), r.via_dedup = true
        """
        rows = [{"url": u, "block_id": bid} for u, bid in pairs]
        with self._driver.session() as session:
            for i in range(0, len(rows), batch_size):
                session.run(query, rows=rows[i : i + batch_size])
        return len(rows)

    # ── Entity CRUD ───────────────────────────────────────────────

    def upsert_entity(self, entity: dict[str, Any]):
        query = """
        MERGE (e:Entity {entity_id: $entity_id})
        SET e.entity_name = $entity_name,
            e.entity_type = $entity_type,
            e.description = $description,
            e.aliases = $aliases
        """
        with self._driver.session() as session:
            session.run(query, **entity)

    def upsert_relates_to(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
        description: str = "",
        keywords: list[str] | None = None,
        weight: float = 1.0,
        source_block_ids: list[str] | None = None,
    ):
        query = """
        MATCH (a:Entity {entity_id: $source_id})
        MATCH (b:Entity {entity_id: $target_id})
        MERGE (a)-[r:RELATES_TO {relation_type: $relation_type}]->(b)
        ON CREATE SET r.description = $description,
                      r.keywords = $keywords,
                      r.weight = $weight,
                      r.source_block_ids = $source_block_ids
        ON MATCH SET r.weight = r.weight + $weight,
                     r.source_block_ids = r.source_block_ids + $source_block_ids,
                     r.description = CASE WHEN size(r.description) < size($description)
                                     THEN $description ELSE r.description END
        """
        with self._driver.session() as session:
            session.run(
                query,
                source_id=source_id,
                target_id=target_id,
                relation_type=relation_type,
                description=description,
                keywords=keywords or [],
                weight=weight,
                source_block_ids=source_block_ids or [],
            )

    def upsert_extracted_from(self, entity_id: str, block_id: str, mention_form: str = ""):
        query = """
        MATCH (e:Entity {entity_id: $entity_id})
        MATCH (b:Block {block_id: $block_id})
        MERGE (e)-[r:EXTRACTED_FROM]->(b)
        SET r.mention_form = $mention_form
        """
        with self._driver.session() as session:
            session.run(query, entity_id=entity_id, block_id=block_id, mention_form=mention_form)

    def upsert_topic_keyword(self, keyword: str):
        query = "MERGE (t:TopicKeyword {keyword: $keyword})"
        with self._driver.session() as session:
            session.run(query, keyword=keyword)

    def upsert_has_topic(self, entity_id: str, keyword: str):
        query = """
        MATCH (e:Entity {entity_id: $entity_id})
        MATCH (t:TopicKeyword {keyword: $keyword})
        MERGE (e)-[:HAS_TOPIC]->(t)
        """
        with self._driver.session() as session:
            session.run(query, entity_id=entity_id, keyword=keyword)

    def bulk_upsert_entities(
        self,
        entities: list[dict[str, Any]],
        build_id: str,
        batch_size: int = 500,
    ):
        """Batch upsert Entity nodes via UNWIND."""
        if not entities:
            return
        query = """
        UNWIND $rows AS e
        MERGE (n:Entity {entity_id: e.entity_id})
        ON CREATE SET n.created_build_id = $build_id
        SET n.entity_name = e.entity_name,
            n.entity_type = e.entity_type,
            n.description = e.description,
            n.aliases = e.aliases,
            n.last_seen_build_id = $build_id
        """
        rows = [
            {
                "entity_id": e["entity_id"],
                "entity_name": e.get("entity_name", ""),
                "entity_type": e.get("entity_type", ""),
                "description": e.get("description", ""),
                "aliases": e.get("aliases", []),
            }
            for e in entities
        ]
        with self._driver.session() as session:
            for i in range(0, len(rows), batch_size):
                session.run(query, rows=rows[i : i + batch_size], build_id=build_id)

    def bulk_upsert_relates_to(
        self,
        relations: list[dict[str, Any]],
        build_id: str,
        batch_size: int = 500,
    ):
        """Batch upsert RELATES_TO relationships. Each relation dict:
        source_id, target_id, relation_type, description, keywords, weight, source_block_ids."""
        if not relations:
            return
        query = """
        UNWIND $rows AS r
        MATCH (a:Entity {entity_id: r.source_id})
        MATCH (b:Entity {entity_id: r.target_id})
        MERGE (a)-[rel:RELATES_TO {relation_type: r.relation_type}]->(b)
        ON CREATE SET rel.description = r.description,
                      rel.keywords = r.keywords,
                      rel.weight = r.weight,
                      rel.source_block_ids = r.source_block_ids,
                      rel.created_build_id = $build_id
        ON MATCH SET rel.weight = r.weight,
                     rel.source_block_ids = r.source_block_ids,
                     rel.keywords = r.keywords,
                     rel.description = r.description
        SET rel.last_seen_build_id = $build_id
        """
        rows = [
            {
                "source_id": r["source_id"],
                "target_id": r["target_id"],
                "relation_type": r.get("relation_type", ""),
                "description": r.get("description", ""),
                "keywords": r.get("keywords", []),
                "weight": r.get("weight", 1.0),
                "source_block_ids": r.get("source_block_ids", []),
            }
            for r in relations
        ]
        with self._driver.session() as session:
            for i in range(0, len(rows), batch_size):
                session.run(query, rows=rows[i : i + batch_size], build_id=build_id)

    def bulk_upsert_extracted_from(self, links: list[dict[str, Any]], batch_size: int = 1000):
        """Batch upsert EXTRACTED_FROM. Each link: entity_id, block_id, mention_form (optional)."""
        if not links:
            return
        query = """
        UNWIND $rows AS l
        MATCH (e:Entity {entity_id: l.entity_id})
        MATCH (b:Block {block_id: l.block_id})
        MERGE (e)-[r:EXTRACTED_FROM]->(b)
        SET r.mention_form = l.mention_form
        """
        rows = [
            {
                "entity_id": l["entity_id"],
                "block_id": l["block_id"],
                "mention_form": l.get("mention_form", ""),
            }
            for l in links
        ]
        with self._driver.session() as session:
            for i in range(0, len(rows), batch_size):
                session.run(query, rows=rows[i : i + batch_size])

    def bulk_upsert_topic_keywords(
        self,
        keywords: list[str],
        build_id: str,
        batch_size: int = 1000,
    ):
        if not keywords:
            return
        query = """
        UNWIND $rows AS k
        MERGE (t:TopicKeyword {keyword: k})
        ON CREATE SET t.created_build_id = $build_id
        SET t.last_seen_build_id = $build_id
        """
        with self._driver.session() as session:
            for i in range(0, len(keywords), batch_size):
                session.run(query, rows=keywords[i : i + batch_size], build_id=build_id)

    def bulk_upsert_has_topic(self, links: list[dict[str, str]], batch_size: int = 1000):
        """Batch upsert HAS_TOPIC. Each link: entity_id, keyword."""
        if not links:
            return
        query = """
        UNWIND $rows AS l
        MATCH (e:Entity {entity_id: l.entity_id})
        MATCH (t:TopicKeyword {keyword: l.keyword})
        MERGE (e)-[:HAS_TOPIC]->(t)
        """
        with self._driver.session() as session:
            for i in range(0, len(links), batch_size):
                session.run(query, rows=links[i : i + batch_size])

    # ── build_id touch / orphan helpers ───────────────────────────

    def touch_unchanged_pages(
        self,
        urls: list[str],
        build_id: str,
        batch_size: int = 1000,
    ) -> dict[str, int]:
        """Refresh `last_seen_build_id` for unchanged pages and their satellites.

        Targets WebPage, contained Block, related Entity (via EXTRACTED_FROM),
        and the RELATES_TO edges connecting two same-build entities.
        Returns counters per object class for observability.
        """
        if not urls:
            return {}

        wp_query = """
        UNWIND $urls AS u
        MATCH (p:WebPage {url: u})
        SET p.last_seen_build_id = $build_id
        RETURN count(p) AS c
        """
        block_query = """
        UNWIND $urls AS u
        MATCH (:WebPage {url: u})-[:CONTAINS]->(b:Block)
        SET b.last_seen_build_id = $build_id
        RETURN count(DISTINCT b) AS c
        """
        entity_query = """
        UNWIND $urls AS u
        MATCH (:WebPage {url: u})-[:CONTAINS]->(:Block)<-[:EXTRACTED_FROM]-(e:Entity)
        SET e.last_seen_build_id = $build_id
        RETURN count(DISTINCT e) AS c
        """
        # Touch a RELATES_TO edge only if both endpoints were touched in this build.
        rel_query = """
        UNWIND $urls AS u
        MATCH (:WebPage {url: u})-[:CONTAINS]->(:Block)<-[:EXTRACTED_FROM]-(e:Entity)
        WITH DISTINCT e
        WHERE e.last_seen_build_id = $build_id
        MATCH (e)-[r:RELATES_TO]-(other:Entity)
        WHERE other.last_seen_build_id = $build_id
        SET r.last_seen_build_id = $build_id
        RETURN count(DISTINCT r) AS c
        """
        counts = {"webpages": 0, "blocks": 0, "entities": 0, "relates_to": 0}
        with self._driver.session() as session:
            for i in range(0, len(urls), batch_size):
                sub = urls[i : i + batch_size]
                counts["webpages"] += session.run(
                    wp_query, urls=sub, build_id=build_id
                ).single()["c"]
                counts["blocks"] += session.run(
                    block_query, urls=sub, build_id=build_id
                ).single()["c"]
                counts["entities"] += session.run(
                    entity_query, urls=sub, build_id=build_id
                ).single()["c"]
                counts["relates_to"] += session.run(
                    rel_query, urls=sub, build_id=build_id
                ).single()["c"]
        return counts

    def collect_qdrant_ids_for_pages(self, urls: list[str]) -> dict[str, list[str]]:
        """Return string ids (block_id / entity_id) under the given pages.

        Used to drive Qdrant payload-touch for the same set of objects.
        Webpages/keywords/relations are returned for symmetry: webpages by url,
        relations are computed from any entity touched here (caller side).
        """
        if not urls:
            return {"webpages": [], "blocks": [], "entities": []}
        block_query = """
        UNWIND $urls AS u
        MATCH (:WebPage {url: u})-[:CONTAINS]->(b:Block)
        RETURN DISTINCT b.block_id AS id
        """
        entity_query = """
        UNWIND $urls AS u
        MATCH (:WebPage {url: u})-[:CONTAINS]->(:Block)<-[:EXTRACTED_FROM]-(e:Entity)
        RETURN DISTINCT e.entity_id AS id
        """
        with self._driver.session() as session:
            blocks = [r["id"] for r in session.run(block_query, urls=urls)]
            entities = [r["id"] for r in session.run(entity_query, urls=urls)]
        return {"webpages": list(urls), "blocks": blocks, "entities": entities}

    def collect_relation_ids_for_entities(self, entity_ids: list[str]) -> list[tuple[str, str, str]]:
        """Return (source_id, target_id, relation_type) tuples for RELATES_TO
        edges where at least one endpoint is in `entity_ids`.

        Used by touch_unchanged for relations whose both endpoints are still alive.
        """
        if not entity_ids:
            return []
        query = """
        UNWIND $eids AS eid
        MATCH (e:Entity {entity_id: eid})-[r:RELATES_TO]-(o:Entity)
        RETURN DISTINCT
          startNode(r).entity_id AS sid,
          endNode(r).entity_id AS tid,
          r.relation_type AS rtype
        """
        with self._driver.session() as session:
            return [(r["sid"], r["tid"], r["rtype"]) for r in session.run(query, eids=entity_ids)]

    def find_orphans(self, current_build_id: str, limit: int = 50000) -> dict[str, list[str]]:
        """Return per-class orphan ids whose last_seen_build_id != current.

        Edges are returned as count placeholders ([str(count)]) since edge ids
        aren't directly addressable; deletion uses last_seen_build_id again.
        """
        out: dict[str, list[str]] = {}
        node_specs = [
            ("WebPage", "url"),
            ("Block", "block_id"),
            ("Entity", "entity_id"),
            ("TopicKeyword", "keyword"),
        ]
        with self._driver.session() as session:
            for label, key in node_specs:
                q = (
                    f"MATCH (n:{label}) "
                    f"WHERE n.last_seen_build_id IS NOT NULL "
                    f"  AND n.last_seen_build_id <> $cur "
                    f"  AND coalesce(n.source_type, '') <> 'agent_fetch' "
                    f"RETURN n.{key} AS id LIMIT $lim"
                )
                out[label] = [r["id"] for r in session.run(q, cur=current_build_id, lim=limit)]
            for rel_type in ("RELATES_TO", "LINKS_TO"):
                q = (
                    f"MATCH ()-[r:{rel_type}]->() "
                    f"WHERE r.last_seen_build_id IS NOT NULL "
                    f"  AND r.last_seen_build_id <> $cur "
                    f"  AND coalesce(r.source_type, '') <> 'agent_fetch' "
                    f"RETURN count(r) AS c"
                )
                rec = session.run(q, cur=current_build_id).single()
                out[rel_type] = [str(rec["c"] if rec else 0)]
        return out

    def delete_orphans(self, current_build_id: str) -> dict[str, int]:
        """DETACH DELETE all nodes/edges whose last_seen_build_id != current.

        Order: delete edges first, then nodes (with DETACH DELETE auto-cleanup).
        """
        out: dict[str, int] = {}
        edge_queries = [
            ("RELATES_TO",
             "MATCH ()-[r:RELATES_TO]->() "
             "WHERE r.last_seen_build_id IS NOT NULL AND r.last_seen_build_id <> $cur "
             "AND coalesce(r.source_type, '') <> 'agent_fetch' "
             "DELETE r RETURN count(r) AS c"),
            ("LINKS_TO",
             "MATCH ()-[r:LINKS_TO]->() "
             "WHERE r.last_seen_build_id IS NOT NULL AND r.last_seen_build_id <> $cur "
             "AND coalesce(r.source_type, '') <> 'agent_fetch' "
             "DELETE r RETURN count(r) AS c"),
        ]
        node_queries = [
            ("Block",
             "MATCH (n:Block) WHERE n.last_seen_build_id IS NOT NULL AND n.last_seen_build_id <> $cur "
             "AND coalesce(n.source_type, '') <> 'agent_fetch' "
             "DETACH DELETE n RETURN count(n) AS c"),
            ("Entity",
             "MATCH (n:Entity) WHERE n.last_seen_build_id IS NOT NULL AND n.last_seen_build_id <> $cur "
             "AND coalesce(n.source_type, '') <> 'agent_fetch' "
             "DETACH DELETE n RETURN count(n) AS c"),
            ("TopicKeyword",
             "MATCH (n:TopicKeyword) WHERE n.last_seen_build_id IS NOT NULL AND n.last_seen_build_id <> $cur "
             "AND coalesce(n.source_type, '') <> 'agent_fetch' "
             "DETACH DELETE n RETURN count(n) AS c"),
            ("WebPage",
             "MATCH (n:WebPage) WHERE n.last_seen_build_id IS NOT NULL AND n.last_seen_build_id <> $cur "
             "AND coalesce(n.source_type, '') <> 'agent_fetch' "
             "DETACH DELETE n RETURN count(n) AS c"),
        ]
        with self._driver.session() as session:
            for name, q in edge_queries + node_queries:
                rec = session.run(q, cur=current_build_id).single()
                out[name] = int(rec["c"]) if rec else 0
        return out

    # ── Query helpers ─────────────────────────────────────────────

    def get_block_by_id(self, block_id: str) -> dict[str, Any] | None:
        query = "MATCH (b:Block {block_id: $block_id}) RETURN b"
        with self._driver.session() as session:
            result = session.run(query, block_id=block_id).single()
            return dict(result["b"]) if result else None

    def get_parent_block(self, block_id: str) -> dict[str, Any] | None:
        query = """
        MATCH (c:Block {block_id: $block_id})-[:PARENT_BLOCK]->(p:Block)
        RETURN p
        """
        with self._driver.session() as session:
            result = session.run(query, block_id=block_id).single()
            return dict(result["p"]) if result else None

    def get_webpage_for_block(self, block_id: str) -> dict[str, Any] | None:
        # A block may be CONTAINS-linked from many WebPages — boilerplate
        # (nav menus, cookie banner) is shared verbatim across hundreds of
        # pages. We want a single, deterministic source page, so prefer the
        # page matching the block's own authoritative `url`, then fall back to
        # the lexicographically-first page. LIMIT 1 avoids the "expected a
        # single record, found multiple" warning .single() would otherwise emit.
        query = """
        MATCH (b:Block {block_id: $block_id})
        MATCH (w:WebPage)-[:CONTAINS]->(b)
        RETURN w
        ORDER BY (w.url = b.url) DESC, w.url ASC
        LIMIT 1
        """
        with self._driver.session() as session:
            result = session.run(query, block_id=block_id).single()
            return dict(result["w"]) if result else None

    def get_blocks_for_webpage(self, url: str) -> list[dict[str, Any]]:
        query = """
        MATCH (w:WebPage {url: $url})-[:CONTAINS]->(b:Block)
        RETURN b ORDER BY b.depth, b.block_id
        """
        with self._driver.session() as session:
            return [dict(r["b"]) for r in session.run(query, url=url)]

    def reconcile_agent_page_snapshot(
        self,
        url: str,
        keep_block_ids: list[str],
        keep_link_targets: list[str],
    ) -> tuple[list[str], int]:
        """Remove relationships absent from the newest Agent page snapshot.

        Blocks are deleted only after their stale CONTAINS edge is removed and
        no other WebPage still references them. Returns orphan block ids for
        matching Qdrant cleanup plus the number of removed outgoing links.
        """
        def _reconcile(tx):
            stale_rows = tx.run(
                """
                MATCH (w:WebPage {url: $url})-[r:CONTAINS]->(b:Block)
                WHERE NOT b.block_id IN $keep_block_ids
                RETURN collect(DISTINCT b.block_id) AS ids
                """,
                url=url,
                keep_block_ids=keep_block_ids,
            ).single()
            stale_ids = list(stale_rows["ids"] or []) if stale_rows else []
            tx.run(
                """
                MATCH (w:WebPage {url: $url})-[r:CONTAINS]->(b:Block)
                WHERE NOT b.block_id IN $keep_block_ids
                DELETE r
                """,
                url=url,
                keep_block_ids=keep_block_ids,
            ).consume()
            orphan_rows = tx.run(
                """
                MATCH (b:Block)
                WHERE b.block_id IN $stale_ids
                  AND NOT EXISTS { MATCH (:WebPage)-[:CONTAINS]->(b) }
                RETURN collect(b.block_id) AS ids
                """,
                stale_ids=stale_ids,
            ).single()
            orphan_ids = list(orphan_rows["ids"] or []) if orphan_rows else []
            if orphan_ids:
                tx.run(
                    """
                    MATCH (b:Block)
                    WHERE b.block_id IN $ids
                    DETACH DELETE b
                    """,
                    ids=orphan_ids,
                ).consume()
            link_row = tx.run(
                """
                MATCH (w:WebPage {url: $url})-[r:LINKS_TO]->(target:WebPage)
                WHERE NOT target.url IN $keep_link_targets
                WITH collect(r) AS rels
                FOREACH (rel IN rels | DELETE rel)
                RETURN size(rels) AS deleted
                """,
                url=url,
                keep_link_targets=keep_link_targets,
            ).single()
            return orphan_ids, int(link_row["deleted"] or 0) if link_row else 0

        with self._driver.session() as session:
            return session.execute_write(_reconcile)

    def get_linked_pages(self, url: str, link_type: str | None = None) -> list[dict[str, Any]]:
        if link_type:
            query = """
            MATCH (w:WebPage {url: $url})-[r:LINKS_TO {link_type: $link_type}]->(p:WebPage)
            RETURN p, r.anchor_text AS anchor
            """
            params = {"url": url, "link_type": link_type}
        else:
            query = """
            MATCH (w:WebPage {url: $url})-[r:LINKS_TO]->(p:WebPage)
            RETURN p, r.anchor_text AS anchor, r.link_type AS link_type
            """
            params = {"url": url}
        with self._driver.session() as session:
            return [{"page": dict(r["p"]), "anchor": r.get("anchor", "")} for r in session.run(query, **params)]

    def get_entity_neighbors(
        self, entity_id: str, max_hops: int = 2, top_m: int = 10
    ) -> list[dict[str, Any]]:
        query = """
        MATCH (e:Entity {entity_id: $entity_id})-[r:RELATES_TO*1..%d]-(n:Entity)
        WITH DISTINCT n, r
        UNWIND r AS rel
        RETURN n.entity_id AS entity_id, n.entity_name AS entity_name,
               n.entity_type AS entity_type, n.description AS description,
               rel.weight AS weight, rel.relation_type AS relation_type
        ORDER BY rel.weight DESC
        LIMIT $top_m
        """ % max_hops
        with self._driver.session() as session:
            return [dict(r) for r in session.run(query, entity_id=entity_id, top_m=top_m)]

    def get_entity_neighbors_batch(
        self, entity_ids: list[str], max_hops: int = 2, top_m: int = 10
    ) -> dict[str, list[dict[str, Any]]]:
        """Batched variant of ``get_entity_neighbors``.

        Runs a single Cypher call with UNWIND over the provided entity IDs and
        returns a mapping ``entity_id -> list of neighbor dicts``. Each
        neighbor dict has the same keys as ``get_entity_neighbors``. Entities
        with no neighbors are still present in the output mapping (empty list),
        so callers can rely on key existence.

        ``max_hops`` is interpolated into the Cypher (Neo4j doesn't allow
        parameterised variable-length bounds); ``top_m`` is per-source, applied
        via collect + slice.
        """
        if not entity_ids:
            return {}
        ids_list = list(dict.fromkeys([eid for eid in entity_ids if eid]))
        if not ids_list:
            return {}

        query = """
        UNWIND $ids AS eid
        MATCH (e:Entity {entity_id: eid})-[r:RELATES_TO*1..%d]-(n:Entity)
        WITH eid, n, r, reduce(w = 0.0, rel IN r | w + coalesce(rel.weight, 0.0)) AS path_weight,
             [rel IN r | rel.relation_type] AS rel_types
        ORDER BY path_weight DESC
        WITH eid, collect({
            entity_id: n.entity_id,
            entity_name: n.entity_name,
            entity_type: n.entity_type,
            description: n.description,
            weight: path_weight,
            relation_type: rel_types[0]
        })[0..$top_m] AS neighbors
        RETURN eid, neighbors
        """ % max_hops

        result: dict[str, list[dict[str, Any]]] = {eid: [] for eid in ids_list}
        with self._driver.session() as session:
            records = session.run(query, ids=ids_list, top_m=top_m)
            for rec in records:
                eid = rec["eid"]
                neighbors = rec["neighbors"] or []
                seen: set[str] = set()
                deduped: list[dict[str, Any]] = []
                for nb in neighbors:
                    nb_dict = dict(nb)
                    nbid = nb_dict.get("entity_id")
                    if not nbid or nbid in seen:
                        continue
                    seen.add(nbid)
                    deduped.append(nb_dict)
                result[eid] = deduped
        return result

    def get_relations_among_entities(
        self,
        entity_ids: list[str],
        limit: int = 40,
        anchor_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """RELATES_TO edges whose BOTH endpoints sit inside ``entity_ids``.

        Unlike ``get_entity_neighbors_batch`` (which expands beyond the seed
        set), this returns only the *internal* edges of a closed set V — the
        E of a (V, E) subgraph for mode_d.

        ``a.entity_id < b.entity_id`` deduplicates the two directions of an
        undirected MATCH without ``DISTINCT`` (cheaper on large E).

        When ``anchor_ids`` is provided, edges where BOTH endpoints are
        anchors are surfaced before single-anchor or anchor-less edges. This
        is the mitigation for cross-page bridges getting evicted by single-
        page co-occurrence (see plan risk #2).
        """
        if not entity_ids:
            return []
        ids_list = list(dict.fromkeys([e for e in entity_ids if e]))
        if not ids_list:
            return []
        anchors = list(dict.fromkeys([a for a in (anchor_ids or []) if a]))

        query = """
        MATCH (a:Entity)-[r:RELATES_TO]-(b:Entity)
        WHERE a.entity_id IN $ids AND b.entity_id IN $ids
          AND a.entity_id < b.entity_id
        WITH a, b, r,
             CASE WHEN a.entity_id IN $anchors AND b.entity_id IN $anchors THEN 1 ELSE 0 END AS anchor_pair
        RETURN a.entity_id   AS source_id,
               a.entity_name AS source_name,
               b.entity_id   AS target_id,
               b.entity_name AS target_name,
               r.relation_type AS relation_type,
               r.description   AS description,
               coalesce(r.weight, 0.0) AS weight,
               anchor_pair
        ORDER BY anchor_pair DESC, weight DESC
        LIMIT $limit
        """
        with self._driver.session() as session:
            return [
                dict(r)
                for r in session.run(query, ids=ids_list, anchors=anchors, limit=limit)
            ]

    def get_entities_batch(self, entity_ids: list[str]) -> dict[str, dict[str, Any]]:
        """UNWIND batch: returns {entity_id: entity_dict} for prompt rendering.

        Used by mode_d's subgraph packing — pulls entity_name / entity_type /
        description in one round-trip after V has been pruned to entity_cap.
        """
        if not entity_ids:
            return {}
        ids_list = list(dict.fromkeys([e for e in entity_ids if e]))
        if not ids_list:
            return {}
        query = """
        UNWIND $ids AS eid
        MATCH (e:Entity {entity_id: eid})
        RETURN eid, e
        """
        with self._driver.session() as session:
            return {
                rec["eid"]: dict(rec["e"])
                for rec in session.run(query, ids=ids_list)
            }

    def get_blocks_for_entity(self, entity_id: str) -> list[dict[str, Any]]:
        query = """
        MATCH (e:Entity {entity_id: $entity_id})-[:EXTRACTED_FROM]->(b:Block)
        RETURN b
        """
        with self._driver.session() as session:
            return [dict(r["b"]) for r in session.run(query, entity_id=entity_id)]

    def get_blocks_for_entities_batch(
        self, entity_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """UNWIND batch: returns {entity_id: [block, ...]} in one Cypher call."""
        if not entity_ids:
            return {}
        query = """
        UNWIND $eids AS eid
        MATCH (e:Entity {entity_id: eid})-[:EXTRACTED_FROM]->(b:Block)
        RETURN eid, collect(b) AS blocks
        """
        with self._driver.session() as session:
            return {
                rec["eid"]: [dict(b) for b in rec["blocks"]]
                for rec in session.run(query, eids=list(entity_ids))
            }

    def get_webpages_for_blocks_batch(
        self, block_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """UNWIND batch: returns {block_id: webpage_dict} in one Cypher call."""
        if not block_ids:
            return {}
        query = """
        UNWIND $bids AS bid
        MATCH (w:WebPage)-[:CONTAINS]->(b:Block {block_id: bid})
        RETURN bid, w
        """
        with self._driver.session() as session:
            return {
                rec["bid"]: dict(rec["w"])
                for rec in session.run(query, bids=list(block_ids))
            }

    def get_blocks_for_webpages_batch(
        self, urls: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """UNWIND batch: returns {url: [block, ...]} in one Cypher call.

        Blocks are ordered by ``depth`` then ``block_id`` to match the
        per-URL ``get_blocks_for_webpage`` query, so callers that fall back
        to ``page_blocks[:k]`` get deterministic results.
        """
        if not urls:
            return {}
        query = """
        UNWIND $urls AS url
        MATCH (w:WebPage {url: url})-[:CONTAINS]->(b:Block)
        WITH url, b ORDER BY b.depth, b.block_id
        RETURN url, collect(b) AS blocks
        """
        with self._driver.session() as session:
            return {
                rec["url"]: [dict(b) for b in rec["blocks"]]
                for rec in session.run(query, urls=list(urls))
            }

    def get_webpages_batch(self, urls: list[str]) -> dict[str, dict[str, Any]]:
        """UNWIND batch: returns {url: webpage_dict} in one Cypher call."""
        if not urls:
            return {}
        query = "UNWIND $urls AS u MATCH (w:WebPage {url: u}) RETURN u, w"
        with self._driver.session() as session:
            return {
                rec["u"]: dict(rec["w"])
                for rec in session.run(query, urls=list(urls))
            }

    def get_linked_pages_batch(
        self, urls: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """UNWIND batch: returns {url: [{page, anchor, link_type}, ...]} in one Cypher call."""
        if not urls:
            return {}
        query = """
        UNWIND $urls AS url
        MATCH (w:WebPage {url: url})-[r:LINKS_TO]->(p:WebPage)
        RETURN url,
               collect({page: p, anchor: r.anchor_text, link_type: r.link_type}) AS links
        """
        with self._driver.session() as session:
            return {
                rec["url"]: [
                    {
                        "page": dict(lnk["page"]),
                        "anchor": lnk["anchor"],
                        "link_type": lnk["link_type"],
                    }
                    for lnk in rec["links"]
                ]
                for rec in session.run(query, urls=list(urls))
            }

    def get_entities_for_block(self, block_id: str) -> list[dict[str, Any]]:
        query = """
        MATCH (e:Entity)-[:EXTRACTED_FROM]->(b:Block {block_id: $block_id})
        RETURN e
        """
        with self._driver.session() as session:
            return [dict(r["e"]) for r in session.run(query, block_id=block_id)]

    def get_all_block_ids(self) -> set[str]:
        query = "MATCH (b:Block) RETURN b.block_id AS bid"
        with self._driver.session() as session:
            return {r["bid"] for r in session.run(query)}

    def get_all_entity_ids(self) -> set[str]:
        query = "MATCH (e:Entity) RETURN e.entity_id AS eid"
        with self._driver.session() as session:
            return {r["eid"] for r in session.run(query)}

    def get_graph_stats(self) -> dict[str, int]:
        queries = {
            "webpages": "MATCH (w:WebPage) RETURN count(w) AS c",
            "fetched_webpages": (
                "MATCH (w:WebPage) WHERE coalesce(w.content_hash, '') <> '' "
                "RETURN count(w) AS c"
            ),
            "stub_webpages": (
                "MATCH (w:WebPage) WHERE coalesce(w.content_hash, '') = '' "
                "RETURN count(w) AS c"
            ),
            "blocks": "MATCH (b:Block) RETURN count(b) AS c",
            "entities": "MATCH (e:Entity) RETURN count(e) AS c",
            "topic_keywords": "MATCH (t:TopicKeyword) RETURN count(t) AS c",
            "links_to": "MATCH ()-[r:LINKS_TO]->() RETURN count(r) AS c",
            "contains": "MATCH ()-[r:CONTAINS]->() RETURN count(r) AS c",
            "relates_to": "MATCH ()-[r:RELATES_TO]->() RETURN count(r) AS c",
            "extracted_from": "MATCH ()-[r:EXTRACTED_FROM]->() RETURN count(r) AS c",
        }
        stats: dict[str, int] = {}
        with self._driver.session() as session:
            for key, q in queries.items():
                result = session.run(q).single()
                stats[key] = result["c"] if result else 0
        return stats
