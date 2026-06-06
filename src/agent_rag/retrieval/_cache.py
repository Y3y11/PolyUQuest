"""In-process LRU cache for router decisions.

Caches the result of LLM-based query classification keyed by a normalized
form of the query string. Cache is per-process; restart flushes it.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from threading import Lock
from typing import Any

from agent_rag.config import thresholds_config

_router_cfg = thresholds_config.get("retrieval", {}).get("router", {})
_CACHE_ENABLED = bool(_router_cfg.get("cache_enabled", True))
_CACHE_MAXSIZE = int(_router_cfg.get("cache_maxsize", 512))

_WHITESPACE = re.compile(r"\s+")


def normalize_query(query: str) -> str:
    return _WHITESPACE.sub(" ", query.strip().lower())


class _RouterCache:
    def __init__(self, maxsize: int):
        self._store: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._maxsize = maxsize
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        if not _CACHE_ENABLED:
            return None
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._hits += 1
                return dict(self._store[key])
            self._misses += 1
            return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        if not _CACHE_ENABLED:
            return
        with self._lock:
            self._store[key] = dict(value)
            self._store.move_to_end(key)
            if len(self._store) > self._maxsize:
                self._store.popitem(last=False)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self._hits, "misses": self._misses, "size": len(self._store)}

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0


router_cache = _RouterCache(_CACHE_MAXSIZE)
