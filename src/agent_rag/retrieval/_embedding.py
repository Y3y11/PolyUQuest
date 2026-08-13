"""Shared embedding utility — supports local, SiliconFlow API, and generic OpenAI-compatible API.

Async-first design: `embed_texts_async` runs batches concurrently via AsyncOpenAI;
sync `embed_texts` is a thin wrapper that runs the async path on a fresh event loop
when called outside async context.

Cache: SQLite with WAL + thread-local persistent connection — safe under both
threading concurrency and asyncio.to_thread dispatch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import structlog
from openai import AsyncOpenAI, OpenAI

from agent_rag.config import settings

logger = structlog.get_logger(__name__)

_model = None
_model_lock = threading.Lock()
_api_client: OpenAI | None = None
_async_api_client: AsyncOpenAI | None = None

# ── Persistent embedding cache (WAL + thread-local) ───────────

_CACHE_DB = Path(__file__).resolve().parents[3] / "data" / "cache" / "embeddings.sqlite"
_tls = threading.local()


def _embedding_cache_key(text: str) -> str:
    payload = f"{settings.embedding_provider}|{settings.embedding_model}|{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_cache_conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with WAL enabled."""
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        return conn
    _CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_CACHE_DB), check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS embeddings "
        "(key TEXT PRIMARY KEY, vector TEXT NOT NULL)"
    )
    conn.commit()
    _tls.conn = conn
    return conn


def _cache_get_many(keys: list[str]) -> dict[str, list[float]]:
    if not keys:
        return {}
    conn = _get_cache_conn()
    out: dict[str, list[float]] = {}
    chunk = 500
    for i in range(0, len(keys), chunk):
        sub = keys[i : i + chunk]
        placeholders = ",".join("?" * len(sub))
        rows = conn.execute(
            f"SELECT key, vector FROM embeddings WHERE key IN ({placeholders})",
            sub,
        ).fetchall()
        for k, v in rows:
            try:
                out[k] = json.loads(v)
            except Exception:
                continue
    return out


def _cache_put_many(items: dict[str, list[float]]) -> None:
    if not items:
        return
    conn = _get_cache_conn()
    conn.executemany(
        "INSERT OR REPLACE INTO embeddings (key, vector) VALUES (?, ?)",
        [(k, json.dumps(v)) for k, v in items.items()],
    )
    conn.commit()


def _get_local_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                logger.info("loading_local_embedding_model", model=settings.embedding_model)
                from sentence_transformers import SentenceTransformer
                try:
                    # Prefer the local Hugging Face cache. Recent transformers
                    # versions otherwise perform a metadata HEAD request even
                    # when all files are already cached.
                    _model = SentenceTransformer(
                        settings.embedding_model, local_files_only=True
                    )
                except Exception:
                    logger.info(
                        "local_embedding_cache_miss",
                        model=settings.embedding_model,
                        fallback="download",
                    )
                    _model = SentenceTransformer(settings.embedding_model)
    return _model


def warmup() -> None:
    """Load and execute the embedding backend before the first user query."""
    if settings.embedding_provider == "local":
        embed_texts(["retrieval warmup"], use_cache=False)


def _resolve_api_credentials() -> tuple[str, str]:
    if settings.embedding_provider == "siliconflow":
        api_key = settings.siliconflow_api_key
        base_url = settings.siliconflow_base_url
    else:
        api_key = settings.embedding_api_key
        base_url = settings.embedding_base_url
    if not api_key:
        raise ValueError(
            f"Embedding provider '{settings.embedding_provider}' requires an API key. "
            f"Set SILICONFLOW_API_KEY or EMBEDDING_API_KEY in .env"
        )
    return api_key, base_url


def _get_api_client() -> OpenAI:
    global _api_client
    if _api_client is None:
        api_key, base_url = _resolve_api_credentials()
        logger.info(
            "init_embedding_api_client",
            provider=settings.embedding_provider,
            model=settings.embedding_model,
            base_url=base_url,
        )
        _api_client = OpenAI(api_key=api_key, base_url=base_url)
    return _api_client


def _get_async_api_client() -> AsyncOpenAI:
    global _async_api_client
    if _async_api_client is None:
        api_key, base_url = _resolve_api_credentials()
        logger.info(
            "init_async_embedding_api_client",
            provider=settings.embedding_provider,
            model=settings.embedding_model,
            base_url=base_url,
        )
        _async_api_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return _async_api_client


_MAX_TOKENS_PER_TEXT = 512
_MAX_TOKENS_PER_BATCH = 8000
_MAX_BATCH_SIZE = 64


def _truncate_text(text: str, max_tokens: int = _MAX_TOKENS_PER_TEXT) -> str:
    """Rough truncation: ~4 chars per token for English, ~1.5 for CJK."""
    if len(text) <= max_tokens * 2:
        return text
    return text[: max_tokens * 3]


def _split_into_safe_batches(
    texts: list[str],
    max_batch_tokens: int = _MAX_TOKENS_PER_BATCH,
    max_batch_size: int = _MAX_BATCH_SIZE,
) -> list[list[str]]:
    """Split texts into batches respecting both token and item-count limits."""
    batches: list[list[str]] = []
    current: list[str] = []
    current_est = 0
    for t in texts:
        est = max(len(t) // 3, 1)
        if current and (current_est + est > max_batch_tokens or len(current) >= max_batch_size):
            batches.append(current)
            current = []
            current_est = 0
        current.append(t)
        current_est += est
    if current:
        batches.append(current)
    return batches


def _embed_via_api(texts: list[str]) -> list[list[float]]:
    """Sync OpenAI-compatible embeddings (kept as fallback for sync wrapper)."""
    client = _get_api_client()
    truncated = [_truncate_text(t) for t in texts]
    batches = _split_into_safe_batches(truncated)
    all_embeddings: list[list[float]] = []
    for batch in batches:
        resp = client.embeddings.create(
            model=settings.embedding_model,
            input=batch,
            encoding_format="float",
        )
        all_embeddings.extend(item.embedding for item in resp.data)
    return all_embeddings


async def _embed_via_api_async(texts: list[str]) -> list[list[float]]:
    """Async embeddings — runs all batches concurrently under a semaphore."""
    client = _get_async_api_client()
    truncated = [_truncate_text(t) for t in texts]
    batches = _split_into_safe_batches(truncated)
    if not batches:
        return []

    concurrency = max(1, int(getattr(settings, "embedding_concurrency", 8)))
    max_retries = max(0, int(getattr(settings, "embedding_max_retries", 2)))
    sem = asyncio.Semaphore(concurrency)

    async def _call_one(batch: list[str], idx: int) -> tuple[int, list[list[float]]]:
        async with sem:
            for attempt in range(max_retries + 1):
                try:
                    resp = await client.embeddings.create(
                        model=settings.embedding_model,
                        input=batch,
                        encoding_format="float",
                    )
                    return idx, [item.embedding for item in resp.data]
                except Exception as exc:
                    logger.warning(
                        "embed_batch_error",
                        batch_idx=idx, attempt=attempt, size=len(batch), error=str(exc),
                    )
                    if attempt == max_retries:
                        raise
                    await asyncio.sleep(1.0 * (attempt + 1))
        return idx, []

    tasks = [_call_one(b, i) for i, b in enumerate(batches)]
    results = await asyncio.gather(*tasks)
    results.sort(key=lambda x: x[0])
    flat: list[list[float]] = []
    for _, vs in results:
        flat.extend(vs)
    return flat


def _embed_raw(texts: list[str]) -> list[list[float]]:
    if settings.embedding_provider == "local":
        model = _get_local_model()
        embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [e.tolist() for e in embeddings]
    return _embed_via_api(texts)


async def _embed_raw_async(texts: list[str]) -> list[list[float]]:
    if settings.embedding_provider == "local":
        model = _get_local_model()
        return await asyncio.to_thread(
            lambda: [
                e.tolist()
                for e in model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
            ]
        )
    return await _embed_via_api_async(texts)


def _prepare_cache_lookup(
    texts: list[str],
) -> tuple[list[str], list[str], dict[str, str]]:
    """Returns (keys_in_input_order, truncated_in_input_order, unique_key_to_text)."""
    truncated = [_truncate_text(t) for t in texts]
    keys = [_embedding_cache_key(t) for t in truncated]
    unique_key_to_text: dict[str, str] = {}
    for key, text in zip(keys, truncated, strict=True):
        unique_key_to_text.setdefault(key, text)
    return keys, truncated, unique_key_to_text


def embed_texts(texts: list[str], use_cache: bool = True) -> list[list[float]]:
    """Embed texts with disk cache + de-dup. Sync entry point.

    If called from within a running event loop, falls back to the sync API path
    (still de-duped + cached) to avoid `asyncio.run()` failure. Otherwise drives
    the async concurrent path via `asyncio.run`.
    """
    if not texts:
        return []

    if not use_cache:
        try:
            asyncio.get_running_loop()
            return _embed_raw(texts)
        except RuntimeError:
            return asyncio.run(_embed_raw_async(texts))

    keys, _truncated, unique_key_to_text = _prepare_cache_lookup(texts)
    cached = _cache_get_many(list(unique_key_to_text.keys()))
    to_compute_keys = [k for k in unique_key_to_text if k not in cached]
    to_compute_texts = [unique_key_to_text[k] for k in to_compute_keys]

    fresh: dict[str, list[float]] = {}
    if to_compute_texts:
        try:
            asyncio.get_running_loop()
            vectors = _embed_raw(to_compute_texts)
        except RuntimeError:
            vectors = asyncio.run(_embed_raw_async(to_compute_texts))
        for k, v in zip(to_compute_keys, vectors, strict=True):
            fresh[k] = v
        _cache_put_many(fresh)

    resolved: dict[str, list[float]] = {**cached, **fresh}
    return [resolved[k] for k in keys]


async def embed_texts_async(texts: list[str], use_cache: bool = True) -> list[list[float]]:
    """Async embed — concurrent batches + de-dup + cache. Use this from offline pipeline."""
    if not texts:
        return []

    if not use_cache:
        return await _embed_raw_async(texts)

    keys, _truncated, unique_key_to_text = _prepare_cache_lookup(texts)
    cached = await asyncio.to_thread(_cache_get_many, list(unique_key_to_text.keys()))
    to_compute_keys = [k for k in unique_key_to_text if k not in cached]
    to_compute_texts = [unique_key_to_text[k] for k in to_compute_keys]

    fresh: dict[str, list[float]] = {}
    if to_compute_texts:
        vectors = await _embed_raw_async(to_compute_texts)
        for k, v in zip(to_compute_keys, vectors, strict=True):
            fresh[k] = v
        await asyncio.to_thread(_cache_put_many, fresh)

    resolved: dict[str, list[float]] = {**cached, **fresh}
    return [resolved[k] for k in keys]


def embed_query(text: str, expand: bool = True) -> list[float]:
    """Embed a single query string for retrieval.

    With ``expand=True`` (default), PolyU acronyms in the query are expanded
    via :mod:`agent_rag.retrieval._alias_expander` before embedding. This is a
    pure query-side rewrite; offline document embeddings are unaffected.

    Pass ``expand=False`` to bypass expansion (diagnostics, A/B comparisons).
    """
    if expand:
        from agent_rag.retrieval._alias_expander import expand_query as _expand
        text = _expand(text)
    return embed_texts([text])[0]
