"""Qdrant vector store: 5 collections for blocks, entities, relations, topic_keywords, webpages."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextvars import ContextVar, Token
from typing import Any, TypeVar

import structlog
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)

from agent_rag.config import settings

logger = structlog.get_logger(__name__)

T = TypeVar("T")


# Ablation hard-stop: when set, any search / retrieve_vectors / search_batch
# call against a listed collection raises ForbiddenCollectionError. Used by the
# `no_entity_layer` (V3) eval variant to guarantee Entity / Relation /
# TopicKeyword data is physically unreachable during the run — a positive
# correctness signal (no quiet fallback into entity space).
_forbidden_collections: ContextVar[frozenset[str]] = ContextVar(
    "qdrant_forbidden_collections", default=frozenset()
)


class ForbiddenCollectionError(RuntimeError):
    """Raised when an ablation guard blocks a query against a banned collection."""


def set_forbidden_collections(names: frozenset[str] | set[str]) -> Token[frozenset[str]]:
    return _forbidden_collections.set(frozenset(names))


def reset_forbidden_collections(token: Token[frozenset[str]]) -> None:
    _forbidden_collections.reset(token)


def _check_forbidden(collection: str) -> None:
    banned = _forbidden_collections.get()
    if collection in banned:
        raise ForbiddenCollectionError(
            f"collection {collection!r} is banned in the current ablation context"
        )

_TRANSIENT_STATUSES = frozenset({502, 503, 504})


def _retry_transient(
    fn: Callable[[], T],
    *,
    operation: str,
    max_attempts: int = 4,
    base_delay_s: float = 0.4,
) -> T:
    """Retry on Qdrant HTTP gateway / overload errors."""
    last: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except UnexpectedResponse as exc:
            last = exc
            code = exc.status_code
            if code not in _TRANSIENT_STATUSES or attempt >= max_attempts - 1:
                raise
            delay = base_delay_s * (2**attempt)
            logger.warning(
                "qdrant_transient_error",
                operation=operation,
                status_code=code,
                attempt=attempt + 1,
                max_attempts=max_attempts,
                retry_after_s=round(delay, 2),
            )
            time.sleep(delay)
    assert last is not None
    raise last

COLLECTIONS = {
    "blocks": "Block text embeddings",
    "entities": "Entity name+description embeddings",
    "relations": "Relation description+keywords embeddings",
    "topic_keywords": "TopicKeyword embeddings",
    "webpages": "WebPage title+meta_description embeddings",
}


class QdrantStore:
    def __init__(self):
        self._client = QdrantClient(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            timeout=60,
            check_compatibility=False,
            # httpx defaults to trust_env=True, which applies Windows/system proxy rules and
            # can return 502 for loopback Qdrant while curl still works.
            trust_env=False,
        )
        self._dim = settings.embedding_dim

    def init_collections(self):
        """Create all 5 collections if they don't exist, and ensure payload
        index on `last_seen_build_id` exists for orphan detection."""
        existing = {c.name for c in self._client.get_collections().collections}
        for name in COLLECTIONS:
            if name not in existing:
                self._client.create_collection(
                    collection_name=name,
                    vectors_config=VectorParams(size=self._dim, distance=Distance.COSINE),
                )
                logger.info("qdrant_collection_created", name=name)
            else:
                logger.info("qdrant_collection_exists", name=name)
            self._ensure_build_id_index(name)

    def _ensure_build_id_index(self, collection: str) -> None:
        """Idempotent payload index for filter-based orphan scans."""
        try:
            self._client.create_payload_index(
                collection_name=collection,
                field_name="last_seen_build_id",
                field_schema="keyword",
            )
        except Exception as exc:
            # Qdrant raises on duplicate index — already-exists is fine to swallow.
            msg = str(exc).lower()
            if "already" in msg or "exists" in msg:
                return
            logger.warning("qdrant_payload_index_failed", collection=collection, error=str(exc))

    def upsert_points(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
        chunk_size: int = 500,
    ):
        """Upsert points in chunks to stay under Qdrant's 32MB payload limit.

        With 768-d float32 vectors (~3KB) plus payload (often >5KB once we
        embed long block content), a single upsert of ~5K points already
        exceeds the default 32MB cap. Chunking at ~500 keeps every batch
        well under the limit and is fast enough that overhead is negligible.
        """
        if not ids:
            return
        if not (len(ids) == len(vectors) == len(payloads)):
            raise ValueError(
                f"upsert_points length mismatch: ids={len(ids)}, "
                f"vectors={len(vectors)}, payloads={len(payloads)}"
            )
        for start in range(0, len(ids), chunk_size):
            end = start + chunk_size
            chunk_points = [
                PointStruct(id=self._str_to_int_id(id_), vector=vec, payload=pay)
                for id_, vec, pay in zip(
                    ids[start:end],
                    vectors[start:end],
                    payloads[start:end],
                    strict=True,
                )
            ]
            self._client.upsert(collection_name=collection, points=chunk_points)

    @staticmethod
    def _build_filter(filters: dict[str, Any] | None):
        """Convert a plain {key: value} dict to a Qdrant Filter (or None)."""
        if not filters:
            return None
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        return Filter(
            must=[FieldCondition(key=k, match=MatchValue(value=v)) for k, v in filters.items()]
        )

    def search(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        _check_forbidden(collection)
        qdrant_filter = self._build_filter(filters)

        def _query():
            return self._client.query_points(
                collection_name=collection,
                query=query_vector,
                limit=top_k,
                query_filter=qdrant_filter,
            ).points

        results = _retry_transient(_query, operation=f"query_points:{collection}")
        return [
            {
                "id": str(r.id),
                "score": r.score,
                "payload": r.payload or {},
            }
            for r in results
        ]

    def retrieve_vectors(
        self, collection: str, ids: list[str]
    ) -> dict[str, list[float]]:
        """Fetch raw vectors for the given string ids in a single HTTP call.

        Returns {id_str: vector}. Missing ids are simply absent from the dict.
        Useful for "score these specific candidates against a query" patterns
        where filtered ANN with top_k=1 per id would N+1 the network.
        """
        _check_forbidden(collection)
        if not ids:
            return {}
        int_ids = [self._str_to_int_id(i) for i in ids]
        # Build reverse map so we can return by original string id.
        int_to_str = dict(zip(int_ids, ids, strict=True))

        def _retrieve():
            return self._client.retrieve(
                collection_name=collection,
                ids=int_ids,
                with_vectors=True,
                with_payload=False,
            )

        records = _retry_transient(_retrieve, operation=f"retrieve:{collection}")
        out: dict[str, list[float]] = {}
        for rec in records:
            sid = int_to_str.get(int(rec.id))
            if sid is None:
                continue
            vec = rec.vector
            if isinstance(vec, dict):
                # Named-vector collections return {name: vector}; we use default.
                vec = next(iter(vec.values()))
            if vec is not None:
                out[sid] = list(vec)
        return out

    def retrieve_payloads(
        self, collection: str, ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Fetch payloads for exact point IDs in one HTTP round-trip."""
        _check_forbidden(collection)
        if not ids:
            return {}
        int_ids = [self._str_to_int_id(item) for item in ids]
        int_to_str = dict(zip(int_ids, ids, strict=True))

        def _retrieve():
            return self._client.retrieve(
                collection_name=collection,
                ids=int_ids,
                with_vectors=False,
                with_payload=True,
            )

        records = _retry_transient(
            _retrieve, operation=f"retrieve_payloads:{collection}"
        )
        return {
            int_to_str[int(record.id)]: dict(record.payload or {})
            for record in records
            if int(record.id) in int_to_str
        }

    def search_batch(
        self, requests: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        """Batch vector search: one HTTP round-trip per collection.

        Each request dict: {"collection": str, "query": list[float],
                            "top_k": int, "filters": dict|None}
        Returns a list aligned with requests; each element is the hit list.
        """
        if not requests:
            return []

        from collections import defaultdict

        from qdrant_client.models import QueryRequest

        # query_batch_points takes a single collection_name — group by collection.
        col_to_indices: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(requests):
            col_to_indices[r["collection"]].append(i)

        for col in col_to_indices:
            _check_forbidden(col)

        results: list[list[dict[str, Any]]] = [[] for _ in requests]

        for col, indices in col_to_indices.items():
            col_reqs = [
                QueryRequest(
                    query=requests[i]["query"],
                    limit=requests[i].get("top_k", 10),
                    with_payload=True,
                    filter=self._build_filter(requests[i].get("filters")),
                )
                for i in indices
            ]

            def _batch(_col=col, _reqs=col_reqs):
                return self._client.query_batch_points(
                    collection_name=_col, requests=_reqs
                )

            batch_results = _retry_transient(
                _batch, operation=f"query_batch_points:{col}"
            )
            for orig_idx, group in zip(indices, batch_results, strict=True):
                results[orig_idx] = [
                    {"id": str(p.id), "score": p.score, "payload": p.payload or {}}
                    for p in group.points
                ]

        return results

    def delete_points(self, collection: str, ids: list[str]):
        if not ids:
            return
        int_ids = [self._str_to_int_id(i) for i in ids]
        self._client.delete(
            collection_name=collection,
            points_selector=int_ids,
        )

    def update_payloads(
        self, collection: str, payloads_by_id: dict[str, dict[str, Any]]
    ) -> int:
        """Update payload metadata without rewriting or regenerating vectors."""
        for point_id, payload in payloads_by_id.items():
            self._client.set_payload(
                collection_name=collection,
                payload=payload,
                points=[self._str_to_int_id(point_id)],
            )
        return len(payloads_by_id)

    def touch_payload(self, collection: str, ids: list[str], build_id: str) -> int:
        """Update only `last_seen_build_id` for the given string ids, no vector touch.

        Returns the number of points targeted (best-effort — Qdrant returns
        operation status, not affected count).

        Resilient to missing IDs: bulk ``set_payload`` raises 404 if any single
        id is absent, which can happen when the pages JSONL and Qdrant
        collection have a pre-existing skew (e.g. a page added to Neo4j whose
        embedding write failed silently in an earlier run). When a 404 is
        hit, we fall back to a per-id loop, skipping the missing ones so the
        rest still get stamped.
        """
        if not ids:
            return 0
        int_ids = [self._str_to_int_id(i) for i in ids]
        try:
            self._client.set_payload(
                collection_name=collection,
                payload={"last_seen_build_id": build_id},
                points=int_ids,
            )
            return len(int_ids)
        except Exception as e:
            msg = str(e)
            if "Not found" not in msg and "404" not in msg:
                raise
            # Per-id fallback. Slow path, only hit when batch contains stragglers.
            touched = 0
            missing = 0
            for pid in int_ids:
                try:
                    self._client.set_payload(
                        collection_name=collection,
                        payload={"last_seen_build_id": build_id},
                        points=[pid],
                    )
                    touched += 1
                except Exception as inner:
                    if "Not found" in str(inner) or "404" in str(inner):
                        missing += 1
                        continue
                    raise
            if missing:
                logger.warning(
                    "touch_payload_missing_ids",
                    collection=collection,
                    touched=touched,
                    missing=missing,
                )
            return touched

    def find_orphan_ids(self, collection: str, current_build_id: str) -> list[int]:
        """Scroll the collection with a must_not filter to collect orphan point ids
        (whose last_seen_build_id is not current). Returns integer Qdrant point ids.
        """
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            MatchValue,
        )

        filt = Filter(
            must_not=[
                FieldCondition(
                    key="last_seen_build_id", match=MatchValue(value=current_build_id)
                ),
                FieldCondition(key="source_type", match=MatchValue(value="agent_fetch")),
            ],
        )
        orphans: list[int] = []
        offset = None
        while True:
            results, offset = self._client.scroll(
                collection_name=collection,
                scroll_filter=filt,
                limit=1000,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            orphans.extend(int(r.id) for r in results)
            if offset is None:
                break
        return orphans

    def delete_orphans_by_build_id(self, collection: str, current_build_id: str) -> int:
        """Filter-based delete: drop points whose last_seen_build_id != current."""
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            FilterSelector,
            MatchValue,
        )

        # Qdrant doesn't expose an "affected count" via filter delete, so we
        # scroll first to surface the size, then issue the bulk delete.
        ids = self.find_orphan_ids(collection, current_build_id)
        if not ids:
            return 0
        filt = Filter(
            must_not=[
                FieldCondition(
                    key="last_seen_build_id", match=MatchValue(value=current_build_id)
                ),
                FieldCondition(key="source_type", match=MatchValue(value="agent_fetch")),
            ],
        )
        self._client.delete(
            collection_name=collection,
            points_selector=FilterSelector(filter=filt),
        )
        return len(ids)

    def get_all_ids(self, collection: str) -> set[str]:
        """Scroll through all points and return payload-based IDs."""
        return set(self.get_all_payloads(collection))

    def get_all_payloads(self, collection: str) -> dict[str, dict[str, Any]]:
        """Return identity-keyed payloads for a complete collection scan."""
        _check_forbidden(collection)
        identity_fields = {
            "webpages": ("url",),
            "blocks": ("block_id",),
            "entities": ("entity_id",),
            "relations": ("fact_key", "relation_id"),
            "topic_keywords": ("keyword",),
        }
        payloads: dict[str, dict[str, Any]] = {}
        offset = None
        while True:
            results, offset = self._client.scroll(
                collection_name=collection,
                limit=1000,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for r in results:
                payload = r.payload or {}
                identity = ""
                for key in identity_fields[collection]:
                    if key in payload:
                        identity = str(payload[key])
                        break
                if (
                    not identity
                    and collection == "relations"
                    and payload.get("source_id")
                    and payload.get("target_id")
                    and payload.get("relation_type")
                ):
                    from agent_rag.kg.profiler import relation_id

                    identity = relation_id(
                        str(payload["source_id"]),
                        str(payload["target_id"]),
                        str(payload["relation_type"]),
                    )
                if identity:
                    payloads[identity] = dict(payload)
            if offset is None:
                break
        return payloads

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _str_to_int_id(s: str) -> int:
        """Convert any string ID to a stable integer for Qdrant point ID."""
        import hashlib
        digest = hashlib.md5(s.encode()).hexdigest()
        return int(digest[:16], 16)
