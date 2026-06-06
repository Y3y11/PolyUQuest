"""Shared helpers for building prompt context across retrieval modes."""

from __future__ import annotations

from typing import Any

from agent_rag.storage.neo4j_store import Neo4jStore


def collect_related_links(
    blocks: list[dict[str, Any]],
    neo4j: Neo4jStore,
    max_links: int = 15,
) -> list[dict[str, str]]:
    """Gather outgoing links from the source pages of retrieved blocks.

    Returns a deduplicated list of ``{"url": ..., "anchor": ...}`` dicts,
    excluding URLs already appearing as block sources.
    """
    source_urls = {b.get("source_url", "") for b in blocks if b.get("source_url")}
    seen: set[str] = set(source_urls)
    links: list[dict[str, str]] = []

    linked_map = neo4j.get_linked_pages_batch(list(source_urls))
    for url in source_urls:
        for linked in linked_map.get(url, []):
            page = linked.get("page", {})
            target_url = page.get("url", "")
            anchor = linked.get("anchor", "")
            if not target_url or target_url in seen:
                continue
            if not anchor:
                continue
            seen.add(target_url)
            links.append({"url": target_url, "anchor": anchor})
            if len(links) >= max_links:
                return links

    return links
