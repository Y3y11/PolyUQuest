"""Graph visualization data endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from agent_rag.api.schemas import (
    GraphDataRequest,
    GraphDataResponse,
    GraphEdge,
    GraphNode,
    GraphStatsResponse,
)
from agent_rag.storage.neo4j_store import Neo4jStore

router = APIRouter()


# ── Helpers ──────────────────────────────────────────────────────────

def _node_from_record(raw: dict[str, Any], labels: list[str]) -> GraphNode | None:
    """Convert a Neo4j node (as dict) + its labels into our GraphNode schema."""
    if "Entity" in labels:
        return GraphNode(
            id=raw.get("entity_id", ""),
            label=raw.get("entity_name", raw.get("entity_id", "")),
            type="Entity",
            properties=raw,
        )
    if "Block" in labels:
        return GraphNode(
            id=raw.get("block_id", ""),
            label=(raw.get("heading_context") or raw.get("content", "Block"))[:80],
            type="Block",
            properties=raw,
        )
    if "TopicKeyword" in labels:
        kw = raw.get("keyword", "")
        return GraphNode(id=kw, label=kw, type="TopicKeyword", properties=raw)
    if "WebPage" in labels:
        url = raw.get("url", "")
        return GraphNode(
            id=url,
            label=raw.get("title") or url,
            type="WebPage",
            properties=raw,
        )
    return None


def _edge_from_rel(rel_props: dict[str, Any], rel_type: str, src: str, tgt: str) -> GraphEdge:
    return GraphEdge(
        source=src,
        target=tgt,
        type=rel_props.get("relation_type", rel_type),
        properties=rel_props,
    )


def _endpoint_id(node_raw: dict[str, Any], labels: list[str]) -> str:
    """Resolve the id field we expose to the frontend for a given node."""
    if "Entity" in labels:
        return node_raw.get("entity_id", "")
    if "Block" in labels:
        return node_raw.get("block_id", "")
    if "TopicKeyword" in labels:
        return node_raw.get("keyword", "")
    if "WebPage" in labels:
        return node_raw.get("url", "")
    return ""


def _match_node_by_id(session, node_id: str) -> tuple[dict[str, Any], list[str]] | None:
    """Find a node whose id (entity_id / block_id / keyword / url) matches node_id."""
    query = """
    MATCH (n)
    WHERE n.entity_id = $id OR n.block_id = $id
       OR n.keyword = $id OR n.url = $id
    RETURN n, labels(n) AS labels
    LIMIT 1
    """
    row = session.run(query, id=node_id).single()
    if not row:
        return None
    return dict(row["n"]), list(row["labels"])


# ── Endpoints ────────────────────────────────────────────────────────

@router.get("/graph/stats", response_model=GraphStatsResponse)
def graph_stats():
    store = Neo4jStore()
    try:
        stats = store.get_graph_stats()
        return GraphStatsResponse(**stats)
    finally:
        store.close()


@router.post("/graph/data", response_model=GraphDataResponse)
def graph_data(req: GraphDataRequest):
    store = Neo4jStore()
    try:
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

        if req.center_entity:
            # Case-insensitive substring on entity_name or any alias, plus an
            # exact match on entity_id. Front-end users type free-form names
            # ("msc dsa", "Data Science"), so we lowercase both sides; the
            # alias scan lets "Data Science and Analytics" match an entity
            # whose canonical name is "MSc in Data Science and Analytics".
            query = """
            MATCH (e:Entity)
            WHERE toLower(e.entity_name) CONTAINS toLower($name)
               OR e.entity_id = $name
               OR any(a IN coalesce(e.aliases, []) WHERE toLower(a) CONTAINS toLower($name))
            WITH e LIMIT 1
            OPTIONAL MATCH (e)-[r:RELATES_TO]-(n:Entity)
            WITH e, collect({node: n, rel: r, type: type(r)}) AS rels
            OPTIONAL MATCH (e)-[:EXTRACTED_FROM]->(b:Block)
            WITH e, rels, collect(b) AS blocks
            OPTIONAL MATCH (e)-[:HAS_TOPIC]->(t:TopicKeyword)
            RETURN e, rels, blocks, collect(t) AS topics
            """
            with store._driver.session() as session:
                result = session.run(query, name=req.center_entity).single()
                if result:
                    e = dict(result["e"])
                    nodes.append(GraphNode(
                        id=e["entity_id"], label=e["entity_name"],
                        type="Entity", properties=e,
                    ))
                    for rel_data in result["rels"]:
                        n = rel_data.get("node")
                        r = rel_data.get("rel")
                        if n and r:
                            nd = dict(n)
                            nodes.append(GraphNode(
                                id=nd["entity_id"], label=nd["entity_name"],
                                type="Entity", properties=nd,
                            ))
                            edges.append(GraphEdge(
                                source=e["entity_id"], target=nd["entity_id"],
                                type=dict(r).get("relation_type", "RELATES_TO"),
                                properties=dict(r),
                            ))
                    for b in result["blocks"][:5]:
                        bd = dict(b)
                        nodes.append(GraphNode(
                            id=bd["block_id"], label=bd.get("heading_context", "Block")[:50],
                            type="Block", properties=bd,
                        ))
                        edges.append(GraphEdge(
                            source=e["entity_id"], target=bd["block_id"],
                            type="EXTRACTED_FROM",
                        ))
                    for t in result["topics"]:
                        td = dict(t)
                        nodes.append(GraphNode(
                            id=td["keyword"], label=td["keyword"],
                            type="TopicKeyword", properties=td,
                        ))
                        edges.append(GraphEdge(
                            source=e["entity_id"], target=td["keyword"],
                            type="HAS_TOPIC",
                        ))
        else:
            query = """
            MATCH (e:Entity)-[r:RELATES_TO]->(n:Entity)
            RETURN e, r, n
            LIMIT $limit
            """
            with store._driver.session() as session:
                seen_ids: set[str] = set()
                for record in session.run(query, limit=req.max_nodes):
                    e = dict(record["e"])
                    n = dict(record["n"])
                    r = dict(record["r"])
                    if e["entity_id"] not in seen_ids:
                        nodes.append(GraphNode(
                            id=e["entity_id"], label=e["entity_name"],
                            type="Entity", properties=e,
                        ))
                        seen_ids.add(e["entity_id"])
                    if n["entity_id"] not in seen_ids:
                        nodes.append(GraphNode(
                            id=n["entity_id"], label=n["entity_name"],
                            type="Entity", properties=n,
                        ))
                        seen_ids.add(n["entity_id"])
                    edges.append(GraphEdge(
                        source=e["entity_id"], target=n["entity_id"],
                        type=r.get("relation_type", "RELATES_TO"),
                        properties=r,
                    ))

        return GraphDataResponse(nodes=nodes, edges=edges)
    finally:
        store.close()


@router.get("/graph/neighbors/{node_id:path}", response_model=GraphDataResponse)
def graph_neighbors(
    node_id: str,
    hops: int = Query(1, ge=1, le=2),
    limit: int = Query(30, ge=1, le=200),
):
    """Return the center node plus its neighbors within `hops` hops."""
    store = Neo4jStore()
    try:
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        seen_nodes: set[str] = set()
        seen_edges: set[tuple[str, str, str]] = set()

        def add_node(raw: dict[str, Any], labels: list[str]):
            gn = _node_from_record(raw, labels)
            if not gn or not gn.id or gn.id in seen_nodes:
                return
            seen_nodes.add(gn.id)
            nodes.append(gn)

        def add_edge(src_raw, src_labels, tgt_raw, tgt_labels, rel):
            s_id = _endpoint_id(src_raw, src_labels)
            t_id = _endpoint_id(tgt_raw, tgt_labels)
            if not s_id or not t_id:
                return
            rel_props = dict(rel)
            rel_type = rel.type if hasattr(rel, "type") else rel_props.get("relation_type", "")
            key = (s_id, t_id, rel_type)
            if key in seen_edges:
                return
            seen_edges.add(key)
            edges.append(_edge_from_rel(rel_props, rel_type, s_id, t_id))

        with store._driver.session() as session:
            center = _match_node_by_id(session, node_id)
            if not center:
                return GraphDataResponse(nodes=[], edges=[])
            center_raw, center_labels = center
            add_node(center_raw, center_labels)

            # cypher variable-length literal — must format inline (driver doesn't bind *1..n)
            cy = f"""
            MATCH (c)
            WHERE c.entity_id = $id OR c.block_id = $id
               OR c.keyword = $id OR c.url = $id
            MATCH p = (c)-[*1..{int(hops)}]-(m)
            WITH p, relationships(p) AS rels, nodes(p) AS ns
            LIMIT $limit
            RETURN rels, ns, [n IN ns | labels(n)] AS node_labels
            """
            result = session.run(cy, id=node_id, limit=limit)
            for record in result:
                ns = record["ns"]
                node_labels = record["node_labels"]
                rels = record["rels"]
                raw_ns = [dict(n) for n in ns]
                for raw, lbls in zip(raw_ns, node_labels, strict=False):
                    add_node(raw, list(lbls))
                # rels are ordered along the path: rels[i] connects ns[i] and ns[i+1]
                for i, r in enumerate(rels):
                    add_edge(
                        raw_ns[i], list(node_labels[i]),
                        raw_ns[i + 1], list(node_labels[i + 1]),
                        r,
                    )

        return GraphDataResponse(nodes=nodes, edges=edges)
    finally:
        store.close()


@router.get("/graph/layered_slice", response_model=GraphDataResponse)
def graph_layered_slice(
    anchor: str = Query(..., description="Entity name (substring or alias) or WebPage URL"),
    max_nodes: int = Query(80, ge=10, le=200),
):
    """Return a three-layer KG slice anchored on a single high-signal entity.

    The slice is *one* WebPage at the top, that page's Blocks in the middle,
    and the Entities (+ a couple of Topics each) those blocks extracted at the
    bottom — exactly the system's WebPage→Block→Entity backbone made visible.

    Every node carries a `layer` field in `properties` so the canvas can pin
    it to the right horizontal band: "page", "block", "entity", "topic".
    Edges within the slice are returned regardless of type; the frontend
    decides which to render bold (cross-layer) vs. dimmed (same-layer).
    """
    store = Neo4jStore()
    try:
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

        with store._driver.session() as session:
            # Step 1: find one canonical WebPage for the anchor.
            #
            # If `anchor` already looks like a URL we trust it directly.
            # Otherwise we resolve via entity_name / alias and pick the page
            # whose blocks contributed the most extractions for that entity —
            # i.e. the page where this entity is most densely discussed, which
            # is a much better "home page" than first-match.
            page_url: str | None = None
            if anchor.startswith(("http://", "https://")):
                row = session.run(
                    "MATCH (w:WebPage {url: $url}) RETURN w.url AS url LIMIT 1",
                    url=anchor,
                ).single()
                if row:
                    page_url = row["url"]
            else:
                row = session.run(
                    """
                    MATCH (e:Entity)
                    WHERE toLower(e.entity_name) CONTAINS toLower($name)
                       OR e.entity_id = $name
                       OR any(a IN coalesce(e.aliases, []) WHERE toLower(a) CONTAINS toLower($name))
                    WITH e LIMIT 1
                    MATCH (e)-[:EXTRACTED_FROM]->(b:Block)<-[:CONTAINS]-(w:WebPage)
                    WITH w, count(b) AS block_hits
                    ORDER BY block_hits DESC
                    RETURN w.url AS url
                    LIMIT 1
                    """,
                    name=anchor,
                ).single()
                if row:
                    page_url = row["url"]

            if not page_url:
                return GraphDataResponse(nodes=[], edges=[])

            # Step 2: pull page + its blocks + extracted entities + a couple
            # of topics per entity. One round trip; cap downstream in Python.
            slice_query = """
            MATCH (w:WebPage {url: $url})
            OPTIONAL MATCH (w)-[:CONTAINS]->(b:Block)
            WITH w, collect(DISTINCT b) AS blocks
            UNWIND (CASE WHEN size(blocks) = 0 THEN [null] ELSE blocks END) AS b
            OPTIONAL MATCH (e:Entity)-[:EXTRACTED_FROM]->(b)
            WITH w, blocks, b, collect(DISTINCT e) AS ents
            UNWIND (CASE WHEN size(ents) = 0 THEN [null] ELSE ents END) AS e
            OPTIONAL MATCH (e)-[:HAS_TOPIC]->(t:TopicKeyword)
            WITH w, blocks, b, e, collect(DISTINCT t)[..2] AS topics
            RETURN w AS page,
                   blocks,
                   b AS block,
                   e AS entity,
                   topics
            """
            page_raw: dict[str, Any] | None = None
            blocks_seen: dict[str, dict[str, Any]] = {}
            entities_seen: dict[str, dict[str, Any]] = {}
            topics_seen: dict[str, dict[str, Any]] = {}
            # entity_id -> set of block_ids it was extracted from (this slice)
            ent_to_blocks: dict[str, set[str]] = {}
            # entity_id -> [topic_keyword, ...]
            ent_to_topics: dict[str, list[str]] = {}

            for row in session.run(slice_query, url=page_url):
                if page_raw is None and row["page"] is not None:
                    page_raw = dict(row["page"])
                blk = row["block"]
                if blk is not None:
                    bd = dict(blk)
                    bid = bd.get("block_id", "")
                    if bid:
                        blocks_seen[bid] = bd
                ent = row["entity"]
                if ent is not None and blk is not None:
                    ed = dict(ent)
                    eid = ed.get("entity_id", "")
                    bid = dict(blk).get("block_id", "")
                    if eid and bid:
                        entities_seen[eid] = ed
                        ent_to_blocks.setdefault(eid, set()).add(bid)
                        for t in row["topics"] or []:
                            td = dict(t)
                            kw = td.get("keyword", "")
                            if kw:
                                topics_seen[kw] = td
                                ent_to_topics.setdefault(eid, [])
                                if kw not in ent_to_topics[eid]:
                                    ent_to_topics[eid].append(kw)

            if not page_raw:
                return GraphDataResponse(nodes=[], edges=[])

            # Step 3: trim entities (and their topics) by connection count
            # if we exceed max_nodes. Page=1, all blocks stay, then entities
            # by descending #block-hits, topics ride along with their entity.
            page_count = 1
            block_count = len(blocks_seen)
            budget = max_nodes - page_count - block_count
            ent_ranked = sorted(
                entities_seen.keys(),
                key=lambda eid: (-len(ent_to_blocks.get(eid, ())), eid),
            )
            kept_ents: list[str] = []
            kept_topics: set[str] = set()
            for eid in ent_ranked:
                topic_cost = len(ent_to_topics.get(eid, []))
                if budget - 1 - topic_cost < 0:
                    break
                kept_ents.append(eid)
                for kw in ent_to_topics.get(eid, []):
                    kept_topics.add(kw)
                budget -= 1 + topic_cost

            # Step 4: assemble nodes.
            page_url_val = page_raw.get("url", page_url)
            page_props = {**page_raw, "layer": "page"}
            nodes.append(GraphNode(
                id=page_url_val,
                label=page_raw.get("title") or page_url_val,
                type="WebPage",
                properties=page_props,
            ))

            for bid, bd in blocks_seen.items():
                bd2 = {**bd, "layer": "block"}
                # Block labels in a slice should be *discriminative*: every
                # block on the same page shares the same heading_context, so
                # using that as the label makes them indistinguishable. Show
                # a content preview instead, falling back to the deepest
                # heading segment if content is missing.
                content = (bd.get("content") or "").strip()
                heading = (bd.get("heading_context") or "").strip()
                deepest_heading = heading.rsplit(" > ", 1)[-1] if heading else ""
                label_src = content or deepest_heading or "Block"
                nodes.append(GraphNode(
                    id=bid,
                    label=label_src[:60],
                    type="Block",
                    properties=bd2,
                ))
                edges.append(GraphEdge(
                    source=page_url_val,
                    target=bid,
                    type="CONTAINS",
                ))

            for eid in kept_ents:
                ed = entities_seen[eid]
                ed2 = {**ed, "layer": "entity"}
                nodes.append(GraphNode(
                    id=eid,
                    label=ed.get("entity_name", eid),
                    type="Entity",
                    properties=ed2,
                ))
                for bid in ent_to_blocks.get(eid, ()):
                    if bid in blocks_seen:
                        edges.append(GraphEdge(
                            source=eid,
                            target=bid,
                            type="EXTRACTED_FROM",
                        ))
                for kw in ent_to_topics.get(eid, []):
                    if kw in kept_topics:
                        edges.append(GraphEdge(
                            source=eid,
                            target=kw,
                            type="HAS_TOPIC",
                        ))

            for kw in kept_topics:
                td = topics_seen[kw]
                td2 = {**td, "layer": "topic"}
                nodes.append(GraphNode(
                    id=kw,
                    label=kw,
                    type="TopicKeyword",
                    properties=td2,
                ))

            # Step 5: pull same-layer edges (LINKS_TO, RELATES_TO) but only
            # when *both* endpoints already landed in the slice. We render
            # them dim on the frontend; the layered story is cross-layer.
            ent_ids = set(kept_ents)
            if ent_ids:
                rel_rows = session.run(
                    """
                    MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
                    WHERE a.entity_id IN $ids AND b.entity_id IN $ids
                    RETURN a.entity_id AS src, b.entity_id AS tgt, r AS rel
                    """,
                    ids=list(ent_ids),
                )
                for rec in rel_rows:
                    r = dict(rec["rel"])
                    edges.append(GraphEdge(
                        source=rec["src"],
                        target=rec["tgt"],
                        type=r.get("relation_type", "RELATES_TO"),
                        properties=r,
                    ))

        return GraphDataResponse(nodes=nodes, edges=edges)
    finally:
        store.close()


@router.get("/graph/path", response_model=GraphDataResponse)
def graph_path(
    source: str = Query(...),
    target: str = Query(...),
    max_depth: int = Query(4, ge=1, le=6),
):
    """Return the shortest path between two nodes (any label) as a GraphData."""
    if source == target:
        raise HTTPException(status_code=400, detail="source and target must differ")

    store = Neo4jStore()
    try:
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        seen_nodes: set[str] = set()

        with store._driver.session() as session:
            cy = f"""
            MATCH (a), (b)
            WHERE (a.entity_id = $src OR a.block_id = $src OR a.keyword = $src OR a.url = $src)
              AND (b.entity_id = $tgt OR b.block_id = $tgt OR b.keyword = $tgt OR b.url = $tgt)
            WITH a, b LIMIT 1
            MATCH p = shortestPath((a)-[*..{int(max_depth)}]-(b))
            RETURN nodes(p) AS ns,
                   relationships(p) AS rels,
                   [n IN nodes(p) | labels(n)] AS node_labels
            """
            record = session.run(cy, src=source, tgt=target).single()
            if not record:
                return GraphDataResponse(nodes=[], edges=[])

            ns = record["ns"]
            node_labels = record["node_labels"]
            rels = record["rels"]
            raw_ns = [dict(n) for n in ns]

            for raw, lbls in zip(raw_ns, node_labels, strict=False):
                gn = _node_from_record(raw, list(lbls))
                if not gn or not gn.id or gn.id in seen_nodes:
                    continue
                seen_nodes.add(gn.id)
                nodes.append(gn)

            for i, r in enumerate(rels):
                s_raw, s_lbl = raw_ns[i], list(node_labels[i])
                t_raw, t_lbl = raw_ns[i + 1], list(node_labels[i + 1])
                s_id = _endpoint_id(s_raw, s_lbl)
                t_id = _endpoint_id(t_raw, t_lbl)
                if not s_id or not t_id:
                    continue
                rel_props = dict(r)
                rel_type = r.type if hasattr(r, "type") else rel_props.get("relation_type", "")
                edges.append(_edge_from_rel(rel_props, rel_type, s_id, t_id))

        return GraphDataResponse(nodes=nodes, edges=edges)
    finally:
        store.close()
