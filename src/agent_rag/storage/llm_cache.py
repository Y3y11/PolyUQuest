"""SQLite-based LLM output cache keyed by (block_id, prompt_hash).

WAL + thread-local persistent connection for safe concurrent access from
asyncio.to_thread / threaded extraction pipelines.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "cache" / "llm_cache.sqlite"
_tls = threading.local()


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        return conn
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_cache (
            block_id TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            response TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (block_id, prompt_hash)
        )
    """)
    conn.commit()
    _tls.conn = conn
    return conn


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


def get_cached(block_id: str, p_hash: str) -> str | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT response FROM llm_cache WHERE block_id=? AND prompt_hash=?",
        (block_id, p_hash),
    ).fetchone()
    return row[0] if row else None


def set_cached(block_id: str, p_hash: str, response: str):
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO llm_cache (block_id, prompt_hash, response, created_at) VALUES (?,?,?,?)",
        (block_id, p_hash, response, time.time()),
    )
    conn.commit()


# --- Chat-completion cache namespace ----------------------------------------
#
# Reuses the (block_id, prompt_hash) table by repurposing block_id as a
# namespace tag ("chat:<provider>:<model>"). prompt_hash covers messages +
# temperature + response_format + max_tokens, so distinct chat parameters
# never collide.

_CHAT_HIT_LOCK = threading.Lock()
_CHAT_HITS = 0
_CHAT_MISSES = 0


def _serialize_chat_params(params: dict[str, Any]) -> str:
    """Stable JSON serialization of cache-relevant chat() parameters."""
    keep = {
        "model": params.get("model"),
        "messages": params.get("messages"),
        "temperature": params.get("temperature"),
        "response_format": params.get("response_format"),
        "max_tokens": params.get("max_tokens"),
    }
    return json.dumps(keep, sort_keys=True, ensure_ascii=False)


def chat_cache_key(provider: str, model: str) -> str:
    return f"chat:{provider}:{model}"


def get_chat_cached(
    provider: str, params: dict[str, Any]
) -> tuple[str, dict[str, int] | None] | None:
    """Return (content, usage) for a cache hit, or None for a miss.

    Old-format entries (plain string content with no usage) come back as
    ``(content, None)`` — callers should treat ``usage is None`` as "no
    recorded token count" rather than zero.
    """
    global _CHAT_HITS, _CHAT_MISSES
    key = chat_cache_key(provider, str(params.get("model", "")))
    p_hash = prompt_hash(_serialize_chat_params(params))
    raw = get_cached(key, p_hash)
    with _CHAT_HIT_LOCK:
        if raw is not None:
            _CHAT_HITS += 1
        else:
            _CHAT_MISSES += 1
    if raw is None:
        return None
    # Envelope format: {"v": 1, "content": "...", "usage": {"input_tokens": ...}}.
    # Back-compat: anything not parseable as that envelope is treated as plain
    # text from the pre-usage-cache era.
    if raw.startswith("{") and '"v"' in raw[:20]:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and obj.get("v") == 1 and "content" in obj:
                usage = obj.get("usage")
                if not isinstance(usage, dict):
                    usage = None
                return obj["content"], usage
        except (json.JSONDecodeError, ValueError):
            pass
    return raw, None


def set_chat_cached(
    provider: str,
    params: dict[str, Any],
    response: str,
    usage: dict[str, int] | None = None,
) -> None:
    if not response:
        return
    key = chat_cache_key(provider, str(params.get("model", "")))
    p_hash = prompt_hash(_serialize_chat_params(params))
    envelope = {"v": 1, "content": response, "usage": usage or {}}
    set_cached(key, p_hash, json.dumps(envelope, ensure_ascii=False))


def chat_cache_stats() -> dict[str, int]:
    with _CHAT_HIT_LOCK:
        return {"hits": _CHAT_HITS, "misses": _CHAT_MISSES}
