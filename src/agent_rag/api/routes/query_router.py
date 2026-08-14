"""Query endpoint: auto-routed or forced mode retrieval, with SSE streaming."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from qdrant_client.http.exceptions import UnexpectedResponse

from agent_rag.api.schemas import BlockRef, PipelineStep, QueryRequest, QueryResponse
from agent_rag.config import llm_config, stage_model, thresholds_config
from agent_rag.llm.client import AsyncLLMClient, LLMClient
from agent_rag.retrieval._contextualize import contextualize_query
from agent_rag.retrieval._embedding import embed_query
from agent_rag.retrieval._rewriter import (
    reset_router_confidence,
    set_router_confidence,
)
from agent_rag.retrieval._suggestions import generate_followups
from agent_rag.retrieval.direct import retrieve_direct
from agent_rag.retrieval.hybrid import retrieve_hybrid
from agent_rag.retrieval.navigation import retrieve_navigation
from agent_rag.retrieval.reasoning import retrieve_reasoning
from agent_rag.retrieval.router import route_query
from agent_rag.security.auth import require_role
from agent_rag.security.models import Role
from agent_rag.storage.llm_cache import (
    chat_cache_key,
    get_cached,
    prompt_hash,
    set_cached,
)
from agent_rag.storage.neo4j_store import Neo4jStore
from agent_rag.storage.qdrant_store import QdrantStore

logger = structlog.get_logger(__name__)
router = APIRouter(dependencies=[Depends(require_role(Role.reader))])

_router_cfg = thresholds_config.get("retrieval", {}).get("router", {})
HYBRID_THRESHOLD = float(_router_cfg.get("hybrid_threshold", 0.6))

_MODE_TO_FN = {
    "mode_a": retrieve_direct,
    "mode_b": retrieve_navigation,
    "mode_c": retrieve_reasoning,
}

ALL_HYBRID_MODES = ["mode_a", "mode_b", "mode_c"]

_GENERATION_MODEL = stage_model("generation")
_GENERATION_MAX_TOKENS = int((llm_config.get("generation", {}) or {}).get("max_tokens", 2048))


def _sse_event(event: str, data: Any) -> str:
    """Format a Server-Sent Event line. ``data`` is JSON-encoded."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/query", response_model=QueryResponse)
def handle_query(req: QueryRequest):
    neo4j = None
    qdrant = None
    llm = None
    try:
        neo4j = Neo4jStore()
        qdrant = QdrantStore()
        llm = LLMClient()
        # Routing step with timing
        ts = time.perf_counter()
        routing = route_query(req.query, llm=llm)
        routing_ms = int((time.perf_counter() - ts) * 1000)

        # Forced-mode override from request bypasses router entirely.
        # ``mode="hybrid"`` is a sentinel asking us to run all three modes in
        # parallel (different from router-triggered 2-mode hybrid below).
        forced_mode = req.mode
        force_full_hybrid = forced_mode == "hybrid"
        mode = (None if force_full_hybrid else forced_mode) or routing["mode"]
        confidence = float(routing.get("confidence", 1.0))
        alt_mode = routing.get("alt_mode")

        hybrid_triggered = force_full_hybrid or (
            forced_mode is None
            and alt_mode
            and confidence < HYBRID_THRESHOLD
            and mode != alt_mode
        )

        routing_trace = PipelineStep(
            step="routing",
            label="Query Routing",
            duration_ms=routing_ms,
            data={
                "mode": mode,
                "alt_mode": alt_mode,
                "confidence": round(confidence, 3),
                "source": routing.get("source", "default"),
                "rule_matched": routing.get("rule_matched"),
                "reasoning": routing.get("reasoning", ""),
                "hybrid_triggered": bool(hybrid_triggered),
                "forced_mode": forced_mode,
            },
        )

        query_emb = embed_query(req.query)

        history = req.history
        conf_tok = set_router_confidence(confidence)
        try:
            if hybrid_triggered:
                hybrid_modes = (
                    ALL_HYBRID_MODES if force_full_hybrid else [mode, alt_mode]
                )
                result = retrieve_hybrid(
                    req.query, query_emb, neo4j, qdrant, llm,
                    modes=hybrid_modes,
                    history=history,
                )
            else:
                fn = _MODE_TO_FN.get(mode, retrieve_direct)
                result = fn(
                    req.query, query_emb, neo4j, qdrant, llm,
                    history=history,
                )
        finally:
            reset_router_confidence(conf_tok)

        blocks = [
            BlockRef(
                block_id=b.get("block_id", ""),
                content=b.get("content", ""),
                heading_context=b.get("heading_context", ""),
                source_url=b.get("source_url", ""),
                source_title=b.get("source_title", ""),
                score=b.get("score", 0.0),
            )
            for b in result.get("blocks", [])
        ]

        mode_trace = [PipelineStep(**step) for step in result.get("trace", [])]
        pipeline_trace = [routing_trace] + mode_trace

        return QueryResponse(
            answer=result["answer"],
            mode=result.get("mode", mode),
            routing_reasoning=routing.get("reasoning", ""),
            blocks=blocks,
            elapsed_seconds=result.get("elapsed_seconds", 0),
            sub_queries=result.get("sub_queries"),
            keywords_extracted=result.get("keywords_extracted"),
            entities_expanded=result.get("entities_expanded"),
            pipeline_trace=pipeline_trace,
        )
    except UnexpectedResponse as exc:
        code = exc.status_code or 0
        if code in (502, 503, 504):
            logger.error("qdrant_unavailable", status_code=code, reason=str(exc)[:200])
            raise HTTPException(
                status_code=503,
                detail=(
                    "Qdrant vector service returned a temporary error (HTTP "
                    f"{code}). Ensure the Qdrant container is running and not "
                    "overloaded, then retry."
                ),
            ) from exc
        raise
    finally:
        if neo4j is not None:
            neo4j.close()
        if qdrant is not None:
            qdrant.close()
        if llm is not None:
            llm.close()


# --- Streaming endpoint -----------------------------------------------------
#
# The synchronous `/query` path returns a single JSON blob after the answer is
# generated. For UI feedback we stream a sequence of SSE events:
#
#   routing      — router decision + chosen mode
#   retrieval    — pipeline step traces (one event per step, as they finish)
#   blocks       — selected block list
#   token        — incremental answer token
#   done         — final wall-clock + LLM cache hit/miss flag
#
# Retrieval still runs synchronously inside an executor (Neo4j/Qdrant clients
# are sync), but the retrieval finishing → first-token-emitted gap is what
# users actually feel, so we emit traces incrementally.


def _build_retrieval_result(
    req: QueryRequest,
    retrieval_query: str,
) -> tuple[dict[str, Any], list[PipelineStep], str, str | None, str]:
    """Run routing + retrieval (with skip_answer=True). Returns
    (result_dict, pipeline_trace, mode, alt_mode, prompt_for_streaming).

    ``req.query`` is the raw user question (drives routing + final answer
    prompt). ``retrieval_query`` is the contextualized standalone search
    query used for embedding and downstream retrieval text use; equal to
    ``req.query`` on first turn or when contextualization is skipped.
    """
    neo4j = Neo4jStore()
    qdrant = QdrantStore()
    llm = LLMClient()
    try:
        ts = time.perf_counter()
        # Router always sees the raw user question — its classifier prompt is
        # tuned for natural phrasings; feeding a rewritten standalone query
        # would muddy mode selection.
        routing = route_query(req.query, llm=llm)
        routing_ms = int((time.perf_counter() - ts) * 1000)

        forced_mode = req.mode
        force_full_hybrid = forced_mode == "hybrid"
        mode = (None if force_full_hybrid else forced_mode) or routing["mode"]
        confidence = float(routing.get("confidence", 1.0))
        alt_mode = routing.get("alt_mode")

        hybrid_triggered = force_full_hybrid or (
            forced_mode is None
            and alt_mode
            and confidence < HYBRID_THRESHOLD
            and mode != alt_mode
        )

        routing_step = PipelineStep(
            step="routing",
            label="Query Routing",
            duration_ms=routing_ms,
            data={
                "mode": mode,
                "alt_mode": alt_mode,
                "confidence": round(confidence, 3),
                "source": routing.get("source", "default"),
                "rule_matched": routing.get("rule_matched"),
                "reasoning": routing.get("reasoning", ""),
                "hybrid_triggered": bool(hybrid_triggered),
                "forced_mode": forced_mode,
                "contextualized": retrieval_query != req.query,
            },
        )

        # Embedding + retrieval text input use the contextualized query so
        # "And the deadline?" with prior turn about MSc DSA can land on the
        # admissions page rather than random "deadline" pages.
        query_emb = embed_query(retrieval_query)
        history = req.history

        conf_tok = set_router_confidence(confidence)
        try:
            if hybrid_triggered:
                hybrid_modes = (
                    ALL_HYBRID_MODES if force_full_hybrid else [mode, alt_mode]
                )
                result = retrieve_hybrid(
                    retrieval_query, query_emb, neo4j, qdrant, llm,
                    modes=hybrid_modes,
                    skip_answer=True,
                    history=history,
                    answer_query=req.query,
                )
                mode_label = result.get("mode", "hybrid")
            else:
                fn = _MODE_TO_FN.get(mode, retrieve_direct)
                result = fn(
                    retrieval_query, query_emb, neo4j, qdrant, llm,
                    skip_answer=True,
                    history=history,
                    answer_query=req.query,
                )
                mode_label = result.get("mode", mode)
        finally:
            reset_router_confidence(conf_tok)

        mode_steps = [PipelineStep(**step) for step in result.get("trace", [])]
        pipeline_trace = [routing_step] + mode_steps
        prompt = result.get("answer_prompt", "")
        return result, pipeline_trace, mode_label, alt_mode, prompt
    finally:
        neo4j.close()
        qdrant.close()
        llm.close()


async def _stream_query(req: QueryRequest) -> AsyncIterator[str]:
    t0 = time.time()
    suggestion_task: asyncio.Task[list[str]] | None = None
    try:
        # Step 0: contextualize the raw query for retrieval when there is
        # prior conversation. Skipped (zero LLM cost) on first turn. The raw
        # query is preserved for routing + final answer prompt.
        retrieval_query = await contextualize_query(req.query, req.history)

        try:
            loop = asyncio.get_running_loop()
            result, pipeline_trace, mode_label, alt_mode, prompt = await loop.run_in_executor(
                None, _build_retrieval_result, req, retrieval_query
            )
        except UnexpectedResponse as exc:
            yield _sse_event("error", {"detail": f"qdrant unavailable: {exc}"})
            return
        except Exception as exc:  # surface the failure to the client and stop
            logger.warning("stream_retrieval_failed", error=str(exc))
            yield _sse_event("error", {"detail": str(exc)})
            return

        yield _sse_event(
            "routing",
            {
                "mode": mode_label,
                "alt_mode": alt_mode,
                "reasoning": pipeline_trace[0].data.get("reasoning", ""),
                "confidence": pipeline_trace[0].data.get("confidence"),
            },
        )
        for step in pipeline_trace:
            yield _sse_event("retrieval", step.model_dump())
        retrieved_blocks = result.get("blocks", [])
        yield _sse_event(
            "blocks",
            [
                {
                    "block_id": b.get("block_id", ""),
                    "content": b.get("content", ""),
                    "heading_context": b.get("heading_context", ""),
                    "source_url": b.get("source_url", ""),
                    "source_title": b.get("source_title", ""),
                    "score": b.get("score", 0.0),
                }
                for b in retrieved_blocks
            ],
        )

        # Kick off the suggestion task in parallel with the main answer.
        # Best-effort: any failure → no `suggestions` event is emitted.
        # The task is cancelled in the outer finally on client disconnect/Stop
        # so we don't burn cheap-LLM tokens after the user moves on.
        if retrieved_blocks:
            suggestion_task = asyncio.create_task(
                generate_followups(req.query, retrieved_blocks)
            )

        if not prompt:
            yield _sse_event("done", {"elapsed_seconds": round(time.time() - t0, 2)})
            return

        # LLM streaming. Cache lookup mirrors LLMClient.chat(use_cache=True): same
        # provider+params hash → if hit, replay cached response in one event so
        # the client sees `token` traffic regardless of cache state.
        chat_kwargs: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": _GENERATION_MAX_TOKENS,
        }
        async with AsyncLLMClient(model=_GENERATION_MODEL) as aclient:
            cache_params = dict(chat_kwargs)
            cache_params["model"] = aclient._model
            cache_key = chat_cache_key(aclient._provider, aclient._model)
            # We can't reuse get_chat_cached's stable serializer without
            # re-importing; do it inline for clarity.
            from agent_rag.storage.llm_cache import _serialize_chat_params  # noqa: WPS437
            p_hash = prompt_hash(_serialize_chat_params(cache_params))
            cached = get_cached(cache_key, p_hash)

            if cached is not None:
                yield _sse_event("cache", {"hit": True})
                yield _sse_event("token", {"text": cached})
                async for evt in _maybe_emit_suggestions(suggestion_task):
                    yield evt
                suggestion_task = None  # already awaited
                yield _sse_event(
                    "done",
                    {
                        "elapsed_seconds": round(time.time() - t0, 2),
                        "cache_hit": True,
                        "answer": cached,
                    },
                )
                return

            yield _sse_event("cache", {"hit": False})
            full_chunks: list[str] = []
            try:
                async for delta in aclient.chat_stream(**chat_kwargs):
                    full_chunks.append(delta)
                    yield _sse_event("token", {"text": delta})
            except Exception as exc:
                logger.warning("stream_llm_failed", error=str(exc))
                yield _sse_event("error", {"detail": f"llm stream failed: {exc}"})
                return

            full_answer = "".join(full_chunks)
            if full_answer:
                set_cached(cache_key, p_hash, full_answer)

            async for evt in _maybe_emit_suggestions(suggestion_task):
                yield evt
            suggestion_task = None  # already awaited

            yield _sse_event(
                "done",
                {
                    "elapsed_seconds": round(time.time() - t0, 2),
                    "cache_hit": False,
                    "answer": full_answer,
                },
            )
    finally:
        # Cancel the suggestion task if the request died early (Stop button,
        # tab close, network failure). Without this, the cheap LLM call keeps
        # running as a zombie task after the user has moved on.
        if suggestion_task is not None and not suggestion_task.done():
            suggestion_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await suggestion_task


async def _maybe_emit_suggestions(
    task: asyncio.Task[list[str]] | None,
) -> AsyncIterator[str]:
    """Await the suggestion task with a hard ceiling and emit one SSE event.

    If the task hasn't resolved within ``wait`` seconds after the main answer
    finished, skip it (don't make the user wait). On any failure / empty
    result, emit nothing — the frontend treats absence as "no suggestions".
    """
    if task is None:
        return
    try:
        items = await asyncio.wait_for(task, timeout=2.5)
    except (TimeoutError, asyncio.CancelledError):
        return
    except Exception as exc:  # noqa: BLE001
        logger.debug("suggestion_task_failed", error=str(exc))
        return
    if items:
        yield _sse_event("suggestions", {"items": items})


@router.post("/query/stream")
async def handle_query_stream(req: QueryRequest):
    """SSE endpoint streaming routing → retrieval traces → answer tokens.

    Use the same body as /query. Recommended ``mode`` values:
    - omitted: auto-route via heuristic + LLM
    - "mode_a" | "mode_b" | "mode_c": force a single mode
    - "hybrid": run all three modes in parallel and merge
    """
    return StreamingResponse(
        _stream_query(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering for true streaming
        },
    )
