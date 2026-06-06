"""Unified LLM client supporting DeepSeek, Qwen, and SiliconFlow via OpenAI-compatible API."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

import structlog
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, OpenAI, RateLimitError

from agent_rag.config import llm_config, settings
from agent_rag.storage.llm_cache import get_chat_cached, set_chat_cached

logger = structlog.get_logger(__name__)

_PROVIDER_REGISTRY: dict[str, tuple[str, str]] = {
    "deepseek": ("deepseek_api_key", "deepseek_base_url"),
    "qwen": ("qwen_api_key", "qwen_base_url"),
    "siliconflow": ("siliconflow_api_key", "siliconflow_base_url"),
}


# Module-level token usage counter, used by evaluate.py to take a snapshot
# before each variant run and diff afterwards. Every successful chat() call
# (sync or async) appends to this; callers that need their own accounting
# should call snapshot_usage() before and diff_usage() after.
_USAGE_TOTALS: dict[str, int] = {
    "input_tokens": 0,
    "output_tokens": 0,
    "llm_calls": 0,
}


# Per-stage breakdown. When a chat() call happens inside a `with cost_stage(name):`
# block, its tokens are *also* attributed to that stage in _STAGE_TOTALS, keyed
# by stage name. The list of known stages is enumerated below so evaluate.py
# can iterate deterministically; unknown stage names still work but won't show
# up in the canonical table unless added here.
KNOWN_STAGES: tuple[str, ...] = (
    "router",
    "rewriter",
    "reasoning_extract",
    "answer",
    "other",
)
_STAGE_TOTALS: dict[str, dict[str, int]] = {
    s: {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0} for s in KNOWN_STAGES
}
_current_stage: ContextVar[str] = ContextVar("_current_stage", default="other")


@contextmanager
def cost_stage(name: str) -> Iterator[None]:
    """Tag any chat() calls inside this block as belonging to `name`.

    Nesting is allowed — the innermost active stage wins. Unknown names create
    a new bucket lazily so callers aren't forced to update KNOWN_STAGES before
    experimenting, but only stages listed in KNOWN_STAGES are guaranteed to be
    reported in canonical order by evaluate.py.
    """
    if name not in _STAGE_TOTALS:
        _STAGE_TOTALS[name] = {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0}
    token = _current_stage.set(name)
    try:
        yield
    finally:
        _current_stage.reset(token)


def snapshot_usage() -> dict[str, int]:
    """Return a shallow copy of the current cumulative usage counters."""
    return dict(_USAGE_TOTALS)


def diff_usage(prev: dict[str, int]) -> dict[str, int]:
    """Compute current - prev for each counter."""
    return {k: _USAGE_TOTALS[k] - prev.get(k, 0) for k in _USAGE_TOTALS}


def snapshot_stage_usage() -> dict[str, dict[str, int]]:
    """Snapshot per-stage cumulative counters (deep copy of int values)."""
    return {stage: dict(vals) for stage, vals in _STAGE_TOTALS.items()}


def diff_stage_usage(
    prev: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    """current - prev for each stage. Stages present in current but not in prev
    are reported as their full current values; stages absent in current default
    to zeros (impossible in practice since stages only grow)."""
    out: dict[str, dict[str, int]] = {}
    for stage, vals in _STAGE_TOTALS.items():
        before = prev.get(stage, {})
        out[stage] = {k: vals.get(k, 0) - int(before.get(k, 0)) for k in vals}
    return out


def _record_usage(resp: Any) -> None:
    usage = getattr(resp, "usage", None)
    stage = _current_stage.get()
    bucket = _STAGE_TOTALS.setdefault(
        stage, {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0}
    )
    if usage is None:
        _USAGE_TOTALS["llm_calls"] += 1
        bucket["llm_calls"] += 1
        return
    in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
    out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
    _USAGE_TOTALS["input_tokens"] += in_tok
    _USAGE_TOTALS["output_tokens"] += out_tok
    _USAGE_TOTALS["llm_calls"] += 1
    bucket["input_tokens"] += in_tok
    bucket["output_tokens"] += out_tok
    bucket["llm_calls"] += 1


def _record_usage_dict(usage: dict[str, int] | None) -> None:
    """Replay a cached usage record into the current stage bucket.

    Cache hits would otherwise vanish from the cost table — but for a paper
    measurement we want representative per-query cost, not "cost given a warm
    cache". Storing usage in the cache and replaying it here makes the
    breakdown deterministic regardless of cache state.
    """
    stage = _current_stage.get()
    bucket = _STAGE_TOTALS.setdefault(
        stage, {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0}
    )
    if not usage:
        _USAGE_TOTALS["llm_calls"] += 1
        bucket["llm_calls"] += 1
        return
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    _USAGE_TOTALS["input_tokens"] += in_tok
    _USAGE_TOTALS["output_tokens"] += out_tok
    _USAGE_TOTALS["llm_calls"] += 1
    bucket["input_tokens"] += in_tok
    bucket["output_tokens"] += out_tok
    bucket["llm_calls"] += 1


def _extract_usage_from_response(resp: Any) -> dict[str, int]:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {}
    return {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


def _resolve_credentials(provider: str) -> tuple[str, str, str]:
    """Return (api_key, base_url, model) for a given provider name."""
    provider_cfg = llm_config.get("providers", {}).get(provider, {})

    if provider in _PROVIDER_REGISTRY:
        key_attr, url_attr = _PROVIDER_REGISTRY[provider]
        api_key = getattr(settings, key_attr, "")
        base_url = getattr(settings, url_attr, "")
    else:
        raise ValueError(
            f"Unknown LLM provider: {provider}. "
            f"Available: {', '.join(_PROVIDER_REGISTRY)}"
        )

    default_models = {
        "deepseek": "deepseek-chat",
        "qwen": "qwen-max",
        "siliconflow": "Pro/deepseek-ai/DeepSeek-V3",
    }
    model = provider_cfg.get("model", default_models.get(provider, "deepseek-chat"))
    return api_key, base_url, model


def _build_client(
    provider: str | None = None, model: str | None = None
) -> tuple[OpenAI, str]:
    provider = provider or settings.llm_provider
    api_key, base_url, default_model = _resolve_credentials(provider)
    # Bump SDK-level retries: a 6h+ resolve batch can't afford to die on one
    # transient `httpx.ConnectError` (happened on 2026-04-27 after 52 min in).
    client = OpenAI(api_key=api_key, base_url=base_url, max_retries=5, timeout=120.0)
    return client, model or default_model


def _build_async_client(
    provider: str | None = None, model: str | None = None
) -> tuple[AsyncOpenAI, str]:
    provider = provider or settings.llm_provider
    api_key, base_url, default_model = _resolve_credentials(provider)
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=5, timeout=120.0)
    return client, model or default_model


class LLMClient:
    """Synchronous LLM client."""

    def __init__(self, provider: str | None = None, model: str | None = None):
        self._provider = provider or settings.llm_provider
        self._client, self._model = _build_client(provider, model)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, str] | None = None,
        use_cache: bool = False,
        **kwargs: Any,
    ) -> str:
        params: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        if temperature is not None:
            params["temperature"] = temperature
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if response_format is not None:
            params["response_format"] = response_format
        params.update(kwargs)

        # Cache lookup. The same {model, messages, temperature, response_format,
        # max_tokens} tuple deterministically maps to a cached response, so
        # callers that want true sampling diversity (temperature > 0 + retries)
        # must opt out via use_cache=False.
        if use_cache:
            cached = get_chat_cached(self._provider, params)
            if cached is not None:
                content, usage = cached
                logger.debug("llm_cache_hit", model=params.get("model", self._model))
                _record_usage_dict(usage)
                return content

        resp = self._client.chat.completions.create(**params)
        content = resp.choices[0].message.content or ""
        _record_usage(resp)
        logger.debug(
            "llm_call",
            model=params.get("model", self._model),
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
        )
        if use_cache and content:
            set_chat_cached(
                self._provider, params, content, _extract_usage_from_response(resp)
            )
        return content


class AsyncLLMClient:
    """Async LLM client for use in FastAPI endpoints."""

    def __init__(self, provider: str | None = None, model: str | None = None):
        self._provider = provider or settings.llm_provider
        self._client, self._model = _build_async_client(provider, model)

    async def close(self):
        await self._client.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, str] | None = None,
        use_cache: bool = False,
        **kwargs: Any,
    ) -> str:
        params: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        if temperature is not None:
            params["temperature"] = temperature
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if response_format is not None:
            params["response_format"] = response_format
        params.update(kwargs)

        if use_cache:
            cached = get_chat_cached(self._provider, params)
            if cached is not None:
                content, usage = cached
                logger.debug("llm_cache_hit", model=params.get("model", self._model))
                _record_usage_dict(usage)
                return content

        resp = await self._client.chat.completions.create(**params)
        content = resp.choices[0].message.content or ""
        _record_usage(resp)
        logger.debug(
            "llm_call",
            model=params.get("model", self._model),
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
        )
        if use_cache and content:
            set_chat_cached(
                self._provider, params, content, _extract_usage_from_response(resp)
            )
        return content

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ):
        """Yield content chunks for SSE streaming."""
        params: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
        }
        if temperature is not None:
            params["temperature"] = temperature
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        params.update(kwargs)

        stream = await self._client.chat.completions.create(**params)
        async for chunk in stream:
            # Some providers (SiliconFlow / DeepSeek) emit a final chunk with an
            # empty `choices` list carrying only usage metadata. Indexing [0]
            # there throws "list index out of range" and aborts the stream.
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = choices[0].delta.content
            if delta:
                yield delta
