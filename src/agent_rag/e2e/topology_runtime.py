"""Deterministic runtime providers used only by topology E2E containers."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from agent_rag.agent.frontier import FrontierSelector
from agent_rag.agent.orchestrator import QueryDrivenAgent
from agent_rag.config import crawl_config, settings
from agent_rag.e2e.deterministic import (
    DeterministicAnswerComposer,
    DeterministicEmbedder,
    DeterministicKnowledgeExtractor,
    DeterministicLLM,
    DeterministicProfileEnricher,
)
from agent_rag.freshness import page_lifecycle_store
from agent_rag.freshness.worker import FreshnessWorker
from agent_rag.indexing.outbox import index_outbox
from agent_rag.knowledge import fact_version_store
from agent_rag.quality import PageQualityGate, page_quality_store
from agent_rag.storage.graph_vector_store import GraphVectorStore
from agent_rag.telemetry import telemetry_recorder
from agent_rag.tools.expand import ExpandTool
from agent_rag.tools.fetch import FetchedDocument, FetchTrustedPageTool
from agent_rag.tools.graph_patch import PublishPatchTool, StagePatchTool
from agent_rag.tools.observations import observation_store, patch_store
from agent_rag.tools.schemas import FetchInput, PublishPatchInput, PublishPatchOutput
from agent_rag.tools.search import SearchTool
from agent_rag.tools.snapshot import PageSnapshotTool
from agent_rag.versioning import page_version_store

_embedder = DeterministicEmbedder(settings.embedding_dim)
_extractor = DeterministicKnowledgeExtractor(settings.business_e2e_token)


def canonical_url() -> str:
    return f"http://e2e.test/access/{settings.business_e2e_token}/"


def _configure_crawl() -> None:
    crawl_config.update(
        {
            "domain_whitelist": ["e2e.test"],
            "seed_urls": [canonical_url()],
            "crawl_delay_seconds": 0,
        }
    )


def prepare_process_runtime() -> None:
    _configure_crawl()
    store = GraphVectorStore()
    try:
        store.init_all()
    finally:
        store.close()


class MappedOriginFetcher:
    """Map a canonical test domain to the isolated Docker fixture origin."""

    def __init__(self, origin: str):
        self.origin = origin.rstrip("/")

    async def __call__(self, tool_input: FetchInput) -> FetchedDocument:
        requested_url = str(tool_input.url)
        parsed = urlsplit(requested_url)
        if parsed.hostname != "e2e.test":
            raise ValueError("Topology fixture fetcher only accepts e2e.test")
        mapped_url = f"{self.origin}{parsed.path}"
        if parsed.query:
            mapped_url = f"{mapped_url}?{parsed.query}"
        headers = {"Accept": "text/html,application/xhtml+xml"}
        if tool_input.if_none_match:
            headers["If-None-Match"] = tool_input.if_none_match
        if tool_input.if_modified_since:
            headers["If-Modified-Since"] = tool_input.if_modified_since
        async with httpx.AsyncClient(
            timeout=tool_input.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(mapped_url, headers=headers)
        if response.status_code == 304:
            return FetchedDocument(
                requested_url=requested_url,
                final_url=requested_url,
                status_code=304,
                html="",
                headers=dict(response.headers),
                not_modified=True,
            )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type:
            raise ValueError(f"Unsupported fixture content type: {content_type}")
        if len(response.content) > tool_input.max_bytes:
            raise ValueError("Fixture response exceeds the configured byte limit")
        return FetchedDocument(
            requested_url=requested_url,
            final_url=requested_url,
            status_code=response.status_code,
            html=response.text,
            headers=dict(response.headers),
        )


class _DelayOncePublisher:
    def __init__(self, delegate: PublishPatchTool):
        self.delegate = delegate

    def _delay_once(self) -> None:
        delay = settings.business_e2e_claim_delay_seconds
        if delay <= 0:
            return
        marker = Path(settings.business_e2e_claim_marker)
        if not marker.is_absolute():
            marker = Path(__file__).resolve().parents[3] / marker
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                marker,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            return
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"delay_seconds={delay}\n")
        time.sleep(delay)

    def run(self, tool_input: PublishPatchInput) -> PublishPatchOutput:
        self._delay_once()
        return self.delegate.run(tool_input)


def build_publish_patch_tool() -> Any:
    _configure_crawl()
    delegate = PublishPatchTool(
        observations=observation_store,
        patches=patch_store,
        embedder=_embedder.embed_many,
        lifecycle_store=page_lifecycle_store,
        version_store=page_version_store,
        extractor=_extractor,
        fact_store=fact_version_store,
    )
    return _DelayOncePublisher(delegate)


def build_query_agent() -> QueryDrivenAgent:
    _configure_crawl()
    fetch_tool = FetchTrustedPageTool(
        fetcher=MappedOriginFetcher(settings.business_e2e_fixture_origin),
        store=observation_store,
    )
    return QueryDrivenAgent(
        search_tool=SearchTool(
            llm_factory=DeterministicLLM,
            embedder=_embedder.embed_query,
            lifecycle_store=page_lifecycle_store,
        ),
        expand_tool=ExpandTool(),
        fetch_tool=fetch_tool,
        stage_patch_tool=StagePatchTool(observation_store, patch_store),
        publish_patch_tool=build_publish_patch_tool(),
        profile_enricher=DeterministicProfileEnricher(),
        frontier_selector=FrontierSelector(llm_factory=DeterministicLLM),
        composer=DeterministicAnswerComposer(),
        observations=observation_store,
        snapshot_tool=PageSnapshotTool(),
        indexing_outbox=index_outbox,
        quality_gate=PageQualityGate(),
        quality_store=page_quality_store,
        lifecycle_store=page_lifecycle_store,
        telemetry=telemetry_recorder,
    )


def build_freshness_worker() -> FreshnessWorker:
    _configure_crawl()
    return FreshnessWorker(
        fetch_tool=FetchTrustedPageTool(
            fetcher=MappedOriginFetcher(settings.business_e2e_fixture_origin),
            store=observation_store,
        )
    )
