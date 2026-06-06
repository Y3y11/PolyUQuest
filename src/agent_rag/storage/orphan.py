"""Orphan detection and cleanup across Neo4j + Qdrant.

An "orphan" is any node or edge whose `last_seen_build_id` is not the
current build_id — i.e. the current run did not touch it, so it represents a
page or entity that has been retired since a previous build.

Usage:
    orphans = find_orphans(store, current_build_id)
    print_orphan_report(orphans)
    if cleanup_mode == "delete":
        deleted = delete_orphans(store, current_build_id)
        print_deletion_summary(deleted)
"""

from __future__ import annotations

from typing import Any

import structlog

from agent_rag.storage.graph_vector_store import GraphVectorStore

logger = structlog.get_logger(__name__)

_QDRANT_COLLECTIONS = ("webpages", "blocks", "entities", "relations", "topic_keywords")


def find_orphans(store: GraphVectorStore, current_build_id: str) -> dict[str, Any]:
    """Collect orphan ids from Neo4j and Qdrant.

    Neo4j: per-class id lists (edges return single-item count placeholder).
    Qdrant: per-collection lists of integer point ids.
    """
    neo = store.neo4j.find_orphans(current_build_id)
    qdrant: dict[str, list[int]] = {
        col: store.qdrant.find_orphan_ids(col, current_build_id)
        for col in _QDRANT_COLLECTIONS
    }
    return {"neo4j": neo, "qdrant": qdrant}


def delete_orphans(store: GraphVectorStore, current_build_id: str) -> dict[str, Any]:
    """Cascade-delete orphans. Returns counts per class."""
    neo_counts = store.neo4j.delete_orphans(current_build_id)
    qd_counts = {
        col: store.qdrant.delete_orphans_by_build_id(col, current_build_id)
        for col in _QDRANT_COLLECTIONS
    }
    return {"neo4j": neo_counts, "qdrant": qd_counts}


def print_orphan_report(orphans: dict[str, Any]) -> None:
    """Render a human-readable summary of find_orphans output to stdout."""
    print("\n=== Orphan Report ===")
    print("Neo4j:")
    for cls, ids in orphans["neo4j"].items():
        if cls in ("RELATES_TO", "LINKS_TO"):
            count = int(ids[0]) if ids else 0
            print(f"  {cls:<14} {count}")
        else:
            sample = ", ".join(ids[:3]) + (" …" if len(ids) > 3 else "")
            print(f"  {cls:<14} {len(ids):>6}  {sample}")
    print("Qdrant:")
    for col, ids in orphans["qdrant"].items():
        print(f"  {col:<14} {len(ids):>6}")
    print()


def print_deletion_summary(deleted: dict[str, Any]) -> None:
    print("\n=== Orphan Cleanup ===")
    print("Neo4j (DETACH DELETE):")
    for cls, n in deleted["neo4j"].items():
        print(f"  {cls:<14} {n:>6}")
    print("Qdrant (filter delete):")
    for col, n in deleted["qdrant"].items():
        print(f"  {col:<14} {n:>6}")
    print()


def has_orphans(orphans: dict[str, Any]) -> bool:
    """Quick check: any orphans across either store?"""
    for cls, ids in orphans["neo4j"].items():
        if cls in ("RELATES_TO", "LINKS_TO"):
            if int(ids[0]) > 0 if ids else False:
                return True
        elif ids:
            return True
    if any(orphans["qdrant"].values()):
        return True
    return False
