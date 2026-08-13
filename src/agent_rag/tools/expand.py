"""Graph-frontier expansion tool over WebPage and Entity relations."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from agent_rag.config import crawl_config
from agent_rag.storage.neo4j_store import Neo4jStore
from agent_rag.tools._ranking import (
    frontier_score,
    lexical_score,
    normalize_url,
    trusted_url,
)
from agent_rag.tools.schemas import ExpandInput, ExpandOutput, FrontierSeed, ToolTraceStep


def _candidate_score(tool_input: ExpandInput, candidate: dict[str, Any]) -> float:
    page = candidate.get("page", {})
    haystack = " ".join(
        [candidate.get("anchor", ""), page.get("title", ""), page.get("meta_description", "")]
    )
    return frontier_score(
        tool_input.query,
        page.get("url", ""),
        haystack,
        tool_input.query_profile,
    )


class ExpandTool:
    name = "polyuquest.expand"

    def __init__(self, neo4j_factory: Callable[[], Neo4jStore] = Neo4jStore):
        self._neo4j_factory = neo4j_factory

    def run(self, tool_input: ExpandInput) -> ExpandOutput:
        started = time.perf_counter()
        neo4j = self._neo4j_factory()
        try:
            source_urls = list(dict.fromkeys(tool_input.source_urls))
            if tool_input.source_block_ids:
                pages = neo4j.get_webpages_for_blocks_batch(tool_input.source_block_ids)
                source_urls.extend(
                    page.get("url", "") for page in pages.values() if page.get("url")
                )

            entity_blocks: dict[str, list[dict[str, Any]]] = {}
            if tool_input.entity_ids:
                entity_blocks = neo4j.get_blocks_for_entities_batch(tool_input.entity_ids)
                entity_block_ids = [
                    block.get("block_id", "")
                    for blocks in entity_blocks.values()
                    for block in blocks
                    if block.get("block_id")
                ]
                pages = neo4j.get_webpages_for_blocks_batch(entity_block_ids)
                source_urls.extend(
                    page.get("url", "") for page in pages.values() if page.get("url")
                )

            source_urls = list(dict.fromkeys(url for url in source_urls if trusted_url(url)))
            linked = neo4j.get_linked_pages_batch(source_urls) if source_urls else {}
            candidates: dict[str, FrontierSeed] = {}
            for parent_url, links in linked.items():
                for item in links:
                    page = item.get("page") or {}
                    raw_url = page.get("url", "")
                    if not trusted_url(raw_url):
                        continue
                    url = normalize_url(raw_url)
                    seed = FrontierSeed(
                        url=url,
                        parent_url=parent_url,
                        anchor_text=item.get("anchor", ""),
                        title=page.get("title", ""),
                        edge_type=item.get("link_type", "LINKS_TO") or "LINKS_TO",
                        supports_sub_goals=[tool_input.sub_goal_id],
                        already_indexed=bool(page.get("last_crawled")),
                        last_fetched_at=page.get("last_crawled"),
                        score=_candidate_score(tool_input, item),
                    )
                    old = candidates.get(url)
                    if old is None or seed.score > old.score:
                        candidates[url] = seed

            # Entity evidence pages are useful frontier targets even if there is no
            # outgoing LINKS_TO edge from the original evidence block.
            for entity_id, blocks in entity_blocks.items():
                for block in blocks:
                    raw_url = block.get("url", "")
                    if not trusted_url(raw_url):
                        continue
                    url = normalize_url(raw_url)
                    score = lexical_score(
                        tool_input.query,
                        (
                            f"{entity_id} {block.get('heading_context', '')} "
                            f"{block.get('content', '')}"
                        ),
                    )
                    seed = FrontierSeed(
                        url=url,
                        title=block.get("heading_context", ""),
                        edge_type="ENTITY_EVIDENCE",
                        supports_sub_goals=[tool_input.sub_goal_id],
                        already_indexed=True,
                        score=score,
                    )
                    old = candidates.get(url)
                    if old is None or seed.score > old.score:
                        candidates[url] = seed

            # A cold-start miss has no evidence edge to follow. Keep discovery
            # bounded to the crawler's trusted seed set instead of silently
            # widening to an external search engine.
            if not candidates and not source_urls and not tool_input.entity_ids:
                seed_labels = crawl_config.get("seed_labels", {}) or {}
                site_identity = str(crawl_config.get("site_identity", ""))
                for raw_url in crawl_config.get("seed_urls", []):
                    if not trusted_url(raw_url):
                        continue
                    url = normalize_url(raw_url)
                    seed_label = str(seed_labels.get(raw_url) or seed_labels.get(url) or url)
                    title = " ".join(part for part in (site_identity, seed_label) if part)
                    candidates[url] = FrontierSeed(
                        url=url,
                        title=title,
                        edge_type="TRUSTED_SEED",
                        graph_distance=1,
                        supports_sub_goals=[tool_input.sub_goal_id],
                        already_indexed=False,
                        score=frontier_score(
                            tool_input.query,
                            url,
                            title,
                            tool_input.query_profile,
                        ),
                    )

            ranked = sorted(
                candidates.values(), key=lambda item: (-item.score, item.url)
            )[: tool_input.max_candidates]
            duration_ms = int((time.perf_counter() - started) * 1000)
            return ExpandOutput(
                candidates=ranked,
                trace=[
                    ToolTraceStep(
                        step="expand_graph_frontier",
                        label="Expand graph frontier",
                        duration_ms=duration_ms,
                        data={
                            "source_urls": len(source_urls),
                            "entity_ids": len(tool_input.entity_ids),
                            "candidates": len(ranked),
                        },
                    )
                ],
            )
        finally:
            neo4j.close()
