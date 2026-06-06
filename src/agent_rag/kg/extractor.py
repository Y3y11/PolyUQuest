"""LLM-based entity/relation joint extraction with caching and validation."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Literal

import json_repair
import structlog
from jinja2 import Template
from pydantic import BaseModel, field_validator

from agent_rag.config import llm_config
from agent_rag.llm.client import AsyncLLMClient, LLMClient
from agent_rag.storage.llm_cache import get_cached, prompt_hash, set_cached

logger = structlog.get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "llm" / "prompts" / "extraction.j2"
_TEMPLATE = Template(_PROMPT_PATH.read_text(encoding="utf-8"))

_PAGE_PROMPT_PATH = Path(__file__).parent.parent / "llm" / "prompts" / "extraction_page.j2"
_PAGE_TEMPLATE = Template(_PAGE_PROMPT_PATH.read_text(encoding="utf-8"))

ENTITY_TYPES = (
    "PERSON", "PROGRAMME", "COURSE", "DEPARTMENT",
    "RESEARCH_AREA", "FACILITY", "POLICY", "SCHOLARSHIP", "EVENT",
)


class ExtractedEntity(BaseModel):
    name: str
    type: Literal[
        "PERSON", "PROGRAMME", "COURSE", "DEPARTMENT",
        "RESEARCH_AREA", "FACILITY", "POLICY", "SCHOLARSHIP", "EVENT",
    ]
    description: str

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, v: str) -> str:
        v = v.upper().strip()
        if v in ENTITY_TYPES:
            return v
        for et in ENTITY_TYPES:
            if et in v:
                return et
        return "RESEARCH_AREA"


class ExtractedRelation(BaseModel):
    source: str
    target: str
    relation_type: str
    description: str
    strength: int = 5
    keywords: list[str] = []

    @field_validator("strength", mode="before")
    @classmethod
    def clamp_strength(cls, v: Any) -> int:
        try:
            v = int(v)
        except (ValueError, TypeError):
            v = 5
        return max(1, min(10, v))


class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity] = []
    relations: list[ExtractedRelation] = []


class ExtractedEntityPage(ExtractedEntity):
    """Page-level entity — includes `source_block_refs` pointing to the supporting blocks."""
    source_block_refs: list[str] = []


class ExtractedRelationPage(ExtractedRelation):
    """Page-level relation — includes `source_block_refs`."""
    source_block_refs: list[str] = []


class PageExtractionResult(BaseModel):
    """Extraction output for a single page — entities/relations with block provenance."""
    entities: list[ExtractedEntityPage] = []
    relations: list[ExtractedRelationPage] = []


def _entity_id(name: str) -> str:
    return hashlib.md5(name.strip().lower().encode()).hexdigest()


def extract_from_block(
    block_id: str,
    block_content: str,
    page_title: str = "",
    heading_context: str = "",
    url: str = "",
    existing_entities: list[dict[str, str]] | None = None,
    llm: LLMClient | None = None,
) -> ExtractionResult:
    """Extract entities and relations from a single Block's text content."""

    if not block_content.strip():
        return ExtractionResult()

    prompt = _TEMPLATE.render(
        block_content=block_content,
        page_title=page_title,
        heading_context=heading_context,
        url=url,
        existing_entities=existing_entities or [],
    )

    p_hash = prompt_hash(prompt)
    cached = get_cached(block_id, p_hash)
    if cached:
        logger.debug("extraction_cache_hit", block_id=block_id)
        raw = cached
    else:
        if llm is None:
            llm = LLMClient()
        ext_cfg = llm_config.get("extraction", {})
        max_retries = ext_cfg.get("max_retries", 2)
        temp = ext_cfg.get("temperature", 0.0)

        raw = ""
        for attempt in range(max_retries + 1):
            try:
                raw = llm.chat(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temp + (0.1 * attempt),
                    response_format={"type": "json_object"},
                )
                break
            except Exception as exc:
                logger.warning("extraction_llm_error", block_id=block_id, attempt=attempt, error=str(exc))
                if attempt == max_retries:
                    return ExtractionResult()

        set_cached(block_id, p_hash, raw)

    try:
        data = json_repair.loads(raw)
    except Exception:
        logger.warning("extraction_json_parse_failed", block_id=block_id)
        return ExtractionResult()

    try:
        result = ExtractionResult.model_validate(data)
    except Exception as exc:
        logger.warning("extraction_validation_failed", block_id=block_id, error=str(exc))
        entities = []
        for e in data.get("entities", []):
            try:
                entities.append(ExtractedEntity.model_validate(e))
            except Exception:
                pass
        relations = []
        for r in data.get("relations", []):
            try:
                relations.append(ExtractedRelation.model_validate(r))
            except Exception:
                pass
        result = ExtractionResult(entities=entities, relations=relations)

    logger.info(
        "extraction_complete",
        block_id=block_id,
        entities=len(result.entities),
        relations=len(result.relations),
    )
    return result


# ── Page-level extraction ──────────────────────────────────────

def _page_cache_key(url: str, blocks: list[dict[str, Any]]) -> str:
    """Stable cache key for a page: combines URL + ordered block_ids."""
    joined = "|".join(b.get("block_id", "") for b in blocks)
    return hashlib.md5(f"{url}|{joined}".encode()).hexdigest()


def _build_page_prompt(
    url: str,
    page_title: str,
    page_type: str,
    blocks: list[dict[str, Any]],
    existing_entities: list[dict[str, str]] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Render the page-level prompt. Returns (prompt, ref_mapping) where ref_mapping
    maps each block_ref (B1, B2, ...) to the actual block_id."""
    ref_blocks = []
    ref_map = []
    for i, b in enumerate(blocks, start=1):
        ref = f"B{i}"
        content = (b.get("content") or "").strip()
        if not content:
            continue
        ref_blocks.append({
            "ref": ref,
            "content": content,
            "heading_context": b.get("heading_context", ""),
        })
        ref_map.append({"ref": ref, "block_id": b["block_id"]})

    prompt = _PAGE_TEMPLATE.render(
        page_title=page_title or "",
        url=url,
        page_type=page_type or "",
        blocks=ref_blocks,
        existing_entities=existing_entities or [],
    )
    return prompt, ref_map


def _parse_page_response(raw: str, ref_to_bid: dict[str, str]) -> PageExtractionResult:
    try:
        data = json_repair.loads(raw)
    except Exception:
        return PageExtractionResult()

    def _resolve_refs(refs: list[Any]) -> list[str]:
        out: list[str] = []
        for r in refs or []:
            bid = ref_to_bid.get(str(r).strip().upper())
            if bid:
                out.append(bid)
        return out

    entities: list[ExtractedEntityPage] = []
    for e in (data.get("entities") or []):
        try:
            obj = ExtractedEntityPage.model_validate(e)
            obj.source_block_refs = _resolve_refs(e.get("source_block_refs", []))
            entities.append(obj)
        except Exception:
            continue

    relations: list[ExtractedRelationPage] = []
    for r in (data.get("relations") or []):
        try:
            obj = ExtractedRelationPage.model_validate(r)
            obj.source_block_refs = _resolve_refs(r.get("source_block_refs", []))
            relations.append(obj)
        except Exception:
            continue

    return PageExtractionResult(entities=entities, relations=relations)


def extract_from_page(
    url: str,
    blocks: list[dict[str, Any]],
    page_title: str = "",
    page_type: str = "",
    existing_entities: list[dict[str, str]] | None = None,
    llm: LLMClient | None = None,
) -> PageExtractionResult:
    """Single-call page-level extraction (synchronous). Uses llm_cache keyed by page signature."""
    if not blocks:
        return PageExtractionResult()

    prompt, ref_map = _build_page_prompt(
        url=url, page_title=page_title, page_type=page_type,
        blocks=blocks, existing_entities=existing_entities,
    )
    if not ref_map:
        return PageExtractionResult()

    ref_to_bid = {r["ref"]: r["block_id"] for r in ref_map}
    p_hash = prompt_hash(prompt)
    cache_key = _page_cache_key(url, blocks)
    cached = get_cached(cache_key, p_hash)
    if cached:
        return _parse_page_response(cached, ref_to_bid)

    if llm is None:
        llm = LLMClient()
    ext_cfg = llm_config.get("extraction", {})
    max_retries = ext_cfg.get("max_retries", 2)
    temp = ext_cfg.get("temperature", 0.0)

    raw = ""
    for attempt in range(max_retries + 1):
        try:
            raw = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=temp + (0.1 * attempt),
                response_format={"type": "json_object"},
            )
            break
        except Exception as exc:
            logger.warning("page_extraction_llm_error", url=url, attempt=attempt, error=str(exc))
            if attempt == max_retries:
                return PageExtractionResult()

    set_cached(cache_key, p_hash, raw)
    return _parse_page_response(raw, ref_to_bid)


async def _extract_page_async(
    url: str,
    blocks: list[dict[str, Any]],
    page_title: str,
    page_type: str,
    existing_entities: list[dict[str, str]],
    async_llm: AsyncLLMClient,
    sem: asyncio.Semaphore,
    max_retries: int,
    temperature: float,
) -> tuple[str, PageExtractionResult]:
    if not blocks:
        return url, PageExtractionResult()

    prompt, ref_map = _build_page_prompt(
        url=url, page_title=page_title, page_type=page_type,
        blocks=blocks, existing_entities=existing_entities,
    )
    if not ref_map:
        return url, PageExtractionResult()

    ref_to_bid = {r["ref"]: r["block_id"] for r in ref_map}
    p_hash = prompt_hash(prompt)
    cache_key = _page_cache_key(url, blocks)
    cached = get_cached(cache_key, p_hash)
    if cached:
        return url, _parse_page_response(cached, ref_to_bid)

    async with sem:
        raw = ""
        for attempt in range(max_retries + 1):
            try:
                raw = await async_llm.chat(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temperature + (0.1 * attempt),
                    response_format={"type": "json_object"},
                )
                break
            except Exception as exc:
                logger.warning(
                    "page_extraction_async_error",
                    url=url, attempt=attempt, error=str(exc),
                )
                if attempt == max_retries:
                    return url, PageExtractionResult()
                await asyncio.sleep(1.5 * (attempt + 1))

    set_cached(cache_key, p_hash, raw)
    return url, _parse_page_response(raw, ref_to_bid)


async def extract_pages_async(
    pages_blocks: list[dict[str, Any]],
    concurrency: int = 16,
    max_retries: int = 2,
    temperature: float = 0.0,
    progress_callback: Any = None,
) -> dict[str, PageExtractionResult]:
    """Extract KG from multiple pages concurrently.

    Args:
        pages_blocks: list of {url, title, page_type, blocks:[block_dict,...]}
        concurrency: max concurrent LLM requests
        progress_callback: optional fn(done, total) called after each page finishes

    Returns:
        dict mapping url -> PageExtractionResult
    """
    if not pages_blocks:
        return {}

    ext_cfg = llm_config.get("extraction", {})
    max_retries = ext_cfg.get("max_retries", max_retries)
    temperature = ext_cfg.get("temperature", temperature)
    model_override = ext_cfg.get("model")

    async_llm = AsyncLLMClient(model=model_override)
    sem = asyncio.Semaphore(concurrency)

    tasks = [
        _extract_page_async(
            url=p["url"],
            blocks=p.get("blocks", []),
            page_title=p.get("title", ""),
            page_type=p.get("page_type", ""),
            existing_entities=p.get("existing_entities", []),
            async_llm=async_llm,
            sem=sem,
            max_retries=max_retries,
            temperature=temperature,
        )
        for p in pages_blocks
    ]

    results: dict[str, PageExtractionResult] = {}
    total = len(tasks)
    done = 0
    for coro in asyncio.as_completed(tasks):
        url, res = await coro
        results[url] = res
        done += 1
        if progress_callback:
            try:
                progress_callback(done, total)
            except Exception:
                pass

    return results
