"""Controlled single-page fetch tool for trusted institutional domains."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx

from agent_rag.config import crawl_config
from agent_rag.crawler.crawler import (
    _extract_links,
    _extract_metadata_from_html,
    error_page_reason,
)
from agent_rag.html_processing.block_tree import blocks_to_dicts, build_block_tree, filter_blocks
from agent_rag.html_processing.cleaner import clean_html
from agent_rag.tools._ranking import (
    constraint_coverage,
    frontier_score,
    lexical_score,
    normalize_url,
    trusted_url,
)
from agent_rag.tools.observations import ObservationRecord, ObservationStore, observation_store
from agent_rag.tools.schemas import (
    EvidenceGain,
    FetchInput,
    FetchMetadata,
    FetchOutput,
    FrontierSeed,
    ToolTraceStep,
)


@dataclass(slots=True)
class FetchedDocument:
    requested_url: str
    final_url: str
    status_code: int
    html: str
    headers: dict[str, str]
    not_modified: bool = False


async def _assert_public_resolution(url: str) -> None:
    """Reject loopback/private/link-local DNS targets before each request."""
    host = urlsplit(url).hostname or ""
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        records = await asyncio.to_thread(socket.getaddrinfo, host, None)
        addresses = list({ipaddress.ip_address(record[4][0]) for record in records})
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError(f"URL resolves to a non-public address: {host}")


async def fetch_httpx_document(tool_input: FetchInput) -> FetchedDocument:
    whitelist = list(crawl_config.get("domain_whitelist", []))
    requested_url = normalize_url(str(tool_input.url))
    current_url = requested_url
    headers = {
        "User-Agent": crawl_config.get("user_agent", "AgentRAG-PolyU/0.1"),
        "Accept": "text/html,application/xhtml+xml",
    }
    if tool_input.if_none_match:
        headers["If-None-Match"] = tool_input.if_none_match
    if tool_input.if_modified_since:
        headers["If-Modified-Since"] = tool_input.if_modified_since

    timeout = httpx.Timeout(tool_input.timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(6):
            if not trusted_url(current_url, whitelist):
                raise ValueError(f"URL is outside the trusted-domain allowlist: {current_url}")
            await _assert_public_resolution(current_url)
            async with client.stream("GET", current_url, headers=headers) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise httpx.HTTPStatusError(
                            "Redirect response has no Location header",
                            request=response.request,
                            response=response,
                        )
                    current_url = normalize_url(urljoin(current_url, location))
                    continue
                if response.status_code == 304:
                    return FetchedDocument(
                        requested_url=requested_url,
                        final_url=current_url,
                        status_code=304,
                        html="",
                        headers=dict(response.headers),
                        not_modified=True,
                    )
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "html" not in content_type:
                    raise ValueError(f"Unsupported content type: {content_type or 'unknown'}")
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > tool_input.max_bytes:
                    raise ValueError("Response exceeds the configured byte limit")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > tool_input.max_bytes:
                        raise ValueError("Response exceeds the configured byte limit")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                html = b"".join(chunks).decode(encoding, errors="replace")
                return FetchedDocument(
                    requested_url=requested_url,
                    final_url=current_url,
                    status_code=response.status_code,
                    html=html,
                    headers=dict(response.headers),
                )
    raise ValueError("Too many redirects while fetching trusted page")


class FetchTrustedPageTool:
    name = "web.fetch_trusted_page"

    def __init__(
        self,
        fetcher: Callable[[FetchInput], Awaitable[FetchedDocument]] = fetch_httpx_document,
        store: ObservationStore = observation_store,
    ):
        self._fetcher = fetcher
        self._store = store

    async def run(self, tool_input: FetchInput) -> FetchOutput:
        started = time.perf_counter()
        requested_url = str(tool_input.url)
        if not trusted_url(requested_url):
            raise ValueError(f"URL is outside the trusted-domain allowlist: {requested_url}")
        document = await self._fetcher(tool_input)
        fetched_at = datetime.now(UTC).isoformat()
        if document.not_modified:
            metadata = FetchMetadata(
                requested_url=document.requested_url,
                final_url=document.final_url,
                fetched_at=fetched_at,
                content_hash="",
                etag=document.headers.get("etag"),
                last_modified=document.headers.get("last-modified"),
                status_code=304,
            )
            return FetchOutput(
                observation_id=f"fetch-{uuid.uuid4().hex}",
                metadata=metadata,
                not_modified=True,
                trace=[
                    ToolTraceStep(
                        step="fetch_trusted_page",
                        label="Fetch trusted page",
                        duration_ms=int((time.perf_counter() - started) * 1000),
                        data={"status_code": 304},
                    )
                ],
            )

        content_hash = hashlib.sha256(document.html.encode("utf-8")).hexdigest()
        meta = _extract_metadata_from_html(document.html, document.final_url)
        denial_reason = error_page_reason(meta, document.html)
        if denial_reason:
            raise ValueError(f"Fetched page is not usable: {denial_reason}")

        cleaned = clean_html(document.html)
        block_dicts = blocks_to_dicts(
            filter_blocks(
                build_block_tree(
                    cleaned,
                    document.final_url,
                    page_title=meta.get("title", ""),
                )
            )
        )
        scored_blocks = sorted(
            (
                (
                    lexical_score(
                        tool_input.query,
                        f"{block.get('heading_context', '')} {block.get('content', '')}",
                    )
                    + 0.35
                    * constraint_coverage(
                        tool_input.query_profile,
                        (
                            f"{document.final_url} {meta.get('title', '')} "
                            f"{block.get('heading_context', '')} {block.get('content', '')}"
                        ),
                    ),
                    block,
                )
                for block in block_dicts
            ),
            key=lambda item: (-item[0], item[1].get("block_id", "")),
        )
        relevant = [(score, block) for score, block in scored_blocks if score > 0][:8]
        if not relevant and scored_blocks:
            # Keep a small observation for fallback/inspection, but report zero
            # evidence gain so the controller does not mistake it for support.
            selected_blocks = [block for _, block in scored_blocks[:3]]
        else:
            selected_blocks = [block for _, block in relevant]

        whitelist = list(crawl_config.get("domain_whitelist", []))
        links = _extract_links(document.html, document.final_url, whitelist)[:100]
        discovered = [
            FrontierSeed(
                url=normalize_url(link["url"]),
                parent_url=document.final_url,
                anchor_text=link.get("anchor_text", ""),
                edge_type="OBSERVED_LINK",
                already_indexed=False,
                score=frontier_score(
                    tool_input.query,
                    link["url"],
                    " ".join(
                        (
                            meta.get("title", ""),
                            meta.get("department", ""),
                            link.get("anchor_text", ""),
                        )
                    ),
                    tool_input.query_profile,
                ),
                supports_sub_goals=[tool_input.sub_goal_id],
            )
            for link in links
        ]
        discovered.sort(key=lambda item: (-item.score, item.url))

        observation_id = f"fetch-{uuid.uuid4().hex}"
        meta.update(
            {
                "url": document.final_url,
                "fetched_at": fetched_at,
                "crawled_at": fetched_at,
                "content_hash": content_hash,
                "etag": document.headers.get("etag"),
                "last_modified": document.headers.get("last-modified"),
                "source_type": "agent_fetch",
                "agent_run_id": tool_input.run_id,
            }
        )
        self._store.put(
            ObservationRecord(
                observation_id=observation_id,
                run_id=tool_input.run_id,
                raw_html=document.html,
                metadata=meta,
                blocks=block_dicts,
                discovered_links=[item.model_dump() for item in discovered],
            )
        )
        coverage = max((score for score, _ in relevant), default=0.0)
        metadata = FetchMetadata(
            requested_url=document.requested_url,
            final_url=document.final_url,
            title=meta.get("title", ""),
            meta_description=meta.get("meta_description", ""),
            department=meta.get("department", ""),
            page_type=meta.get("page_type", "other"),
            fetched_at=fetched_at,
            content_hash=content_hash,
            etag=meta.get("etag"),
            last_modified=meta.get("last_modified"),
            status_code=document.status_code,
        )
        return FetchOutput(
            observation_id=observation_id,
            metadata=metadata,
            block_refs=[block.get("block_id", "") for block in selected_blocks],
            # Keep enough same-score navigation links for the bounded frontier
            # selector. Department/intranet menus often contain dozens of
            # siblings; truncating at 20 silently dropped valid procedure hubs.
            discovered_links=discovered[:50],
            evidence_gain=EvidenceGain(
                relevant_blocks=len(relevant),
                total_blocks=len(block_dicts),
                lexical_coverage=round(coverage, 4),
            ),
            trace=[
                ToolTraceStep(
                    step="fetch_trusted_page",
                    label="Fetch and structure trusted page",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    data={
                        "status_code": document.status_code,
                        "blocks": len(block_dicts),
                        "relevant_blocks": len(relevant),
                        "links": len(discovered),
                    },
                )
            ],
        )
