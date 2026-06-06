"""PolyU website crawler — supports httpx, Firecrawl scrape, and Firecrawl crawl backends.

Fetcher modes (set via crawl.yaml `fetcher` or --fetcher CLI):
  httpx             Original httpx BFS.  Fast, no JS rendering.
  firecrawl-scrape  Our BFS + Firecrawl scrape per-page.  JS rendered, we control depth.
  firecrawl-crawl   Firecrawl server-side crawl.  Fastest, fully managed BFS + JS.
  firecrawl-map     Firecrawl map (URL discovery only, no content).
  jina              Jina Reader (r.jina.ai) — JS rendered, reads from data/frontier.jsonl
                    instead of doing BFS.  Use after Firecrawl quota is exhausted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from selectolax.parser import HTMLParser

from agent_rag.config import crawl_config, settings
from agent_rag.crawler.url_filter import filter_reason, should_crawl

logger = structlog.get_logger(__name__)

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "raw_html"
META_DIR = Path(__file__).resolve().parents[3] / "data" / "metadata"


# ── URL / metadata helpers ──────────────────────────────────────────


def _matches_whitelist(url: str, whitelist: list[str]) -> bool:
    host = urlparse(url).hostname or ""
    for pattern in whitelist:
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if host.endswith(suffix) or host == suffix[1:]:
                return True
        elif host == pattern:
            return True
    return False


def _classify_page_type(url: str) -> str:
    rules = crawl_config.get("page_type_rules", {})
    for ptype, rule in rules.items():
        for pat in rule.get("url_patterns", []):
            if pat in url:
                return ptype
    return "other"


def _extract_department(url: str) -> str:
    path = urlparse(url).path.lower()
    dept_match = re.search(r"/([a-z]{2,6})/", path)
    if dept_match:
        return dept_match.group(1).upper()
    return "unknown"


def _extract_metadata_from_html(html: str, url: str) -> dict[str, Any]:
    tree = HTMLParser(html)
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""
    meta_desc = ""
    for meta in tree.css("meta"):
        if meta.attributes.get("name", "").lower() == "description":
            meta_desc = meta.attributes.get("content", "")
            break
    return {
        "url": url,
        "title": title,
        "meta_description": meta_desc,
        "department": _extract_department(url),
        "page_type": _classify_page_type(url),
        "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _html_text_preview(html: str, limit: int = 2000) -> str:
    if not html:
        return ""
    try:
        tree = HTMLParser(html)
        body = tree.body or tree.root
        return body.text(separator=" ", strip=True)[:limit] if body else ""
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)[:limit]


def error_page_reason(meta: dict[str, Any], html: str = "") -> str | None:
    """Return a reason when a fetched page is an error/denial placeholder.

    This is intentionally stricter than a generic ``"error"`` keyword match:
    research publications can legitimately contain words like "error" in their
    titles, so only canonical error-page phrases and status templates are used.
    """
    title = (meta.get("title") or "").strip()
    markdown = (meta.get("markdown") or "").strip()
    preview = f"{title}\n{markdown[:2000]}\n{_html_text_preview(html)}".lower()
    title_l = title.lower()

    if title_l.startswith("access denied") or "access denied" in preview and "403" in preview:
        return "access_denied"
    if "we could not find what you were looking for" in preview:
        return "not_found_template"
    if title_l.startswith("!error"):
        return "error_title"
    if title_l.startswith("error 400") or "## http error 400" in preview:
        return "http_400"
    if title_l in {"404", "404 not found", "page not found"}:
        return "not_found"
    if "page not found" in preview and len(markdown or _html_text_preview(html)) < 5000:
        return "not_found"
    return None


def _extract_links(html: str, base_url: str, whitelist: list[str]) -> list[dict[str, str]]:
    tree = HTMLParser(html)
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        abs_url = urljoin(base_url, href)
        abs_url = abs_url.split("#")[0].split("?")[0]
        if abs_url in seen:
            continue
        if not _matches_whitelist(abs_url, whitelist):
            continue
        if not should_crawl(abs_url):
            continue
        seen.add(abs_url)
        anchor = a.text(strip=True) or ""
        links.append({"url": abs_url, "anchor_text": anchor[:200]})
    return links


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _save_page(
    url: str, html: str, meta: dict[str, Any], whitelist: list[str],
) -> dict[str, Any]:
    uid = _url_hash(url)
    html_path = DATA_DIR / f"{uid}.html"
    html_path.write_text(html, encoding="utf-8")
    links = _extract_links(html, url, whitelist) if html else []
    meta["outgoing_links"] = links
    meta["html_path"] = str(html_path)
    meta_path = META_DIR / f"{uid}.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


# ── Firecrawl client ────────────────────────────────────────────────


def _get_firecrawl():
    from firecrawl import FirecrawlApp
    key = settings.firecrawl_api_key
    if not key:
        raise ValueError("FIRECRAWL_API_KEY not set. Get one at https://www.firecrawl.dev/")
    fc_config = crawl_config.get("firecrawl", {})
    return FirecrawlApp(
        api_key=key,
        timeout=fc_config.get("request_timeout_seconds", 90),
        max_retries=fc_config.get("max_retries", 2),
    )


def _doc_to_meta(doc, url: str | None = None) -> tuple[str, dict[str, Any]]:
    """Convert a Firecrawl Document to (html, meta_dict)."""
    html = doc.html or ""
    markdown = doc.markdown or ""
    fm = doc.metadata
    resolved_url = url
    title = ""
    description = ""
    if fm:
        resolved_url = resolved_url or fm.source_url or fm.url or url or ""
        title = fm.title or fm.og_title or ""
        description = fm.description or fm.og_description or ""
    resolved_url = resolved_url or ""
    meta = {
        "url": resolved_url,
        "title": title,
        "meta_description": description,
        "department": _extract_department(resolved_url),
        "page_type": _classify_page_type(resolved_url),
        "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "markdown": markdown,
    }
    return html, meta


# ── Mode 1: firecrawl-crawl  (server-side, fastest) ────────────────


def _crawl_firecrawl_crawl(
    fc, seed_urls: list[str], max_pages: int, max_depth: int,
    whitelist: list[str], include_paths: list[str] | None,
    exclude_paths: list[str] | None,
    per_seed_limit: int | None = None,
    poll_interval: int = 10,
    max_wait_seconds: int = 1800,
) -> list[dict[str, Any]]:
    """Use Firecrawl's /crawl endpoint — server does BFS + JS rendering.

    Uses the async ``start_crawl`` + ``get_crawl_status`` polling pattern so
    that progress is logged every ``poll_interval`` seconds instead of the
    SDK's silent blocking ``crawl()``.  Pages are collected once per seed;
    pagination is handled by following ``job.next`` via ``get_crawl_status_page``.

    Args:
        per_seed_limit: If given, each seed is capped at this many pages
            (prevents the first large seed from eating the whole budget).
            If None, seeds share ``max_pages`` in order.
        poll_interval: Seconds between status polls.
        max_wait_seconds: Hard ceiling per seed.  On hit, the in-flight
            crawl job is cancelled server-side and partial results are kept.
    """
    from firecrawl.v2.types import ScrapeOptions

    results: list[dict[str, Any]] = []
    remaining = max_pages

    for seed_idx, seed in enumerate(seed_urls, start=1):
        if remaining <= 0:
            break

        seed_limit = min(per_seed_limit or remaining, remaining)
        logger.info(
            "firecrawl_crawl_start",
            seed=seed, limit=seed_limit,
            seed_progress=f"{seed_idx}/{len(seed_urls)}",
        )

        # 1. Kick off job
        try:
            resp = fc.start_crawl(
                seed,
                include_paths=include_paths,
                exclude_paths=exclude_paths,
                max_discovery_depth=max_depth,
                limit=seed_limit,
                allow_subdomains=True,
                deduplicate_similar_urls=True,
                scrape_options=ScrapeOptions(
                    formats=["html", "markdown"],
                    only_main_content=True,
                ),
            )
        except Exception as exc:
            logger.error("firecrawl_crawl_start_failed", seed=seed, error=str(exc))
            continue

        job_id = resp.id
        logger.info("firecrawl_crawl_job_started", seed=seed, job_id=job_id)

        # 2. Poll loop — status-only via raw HTTP with ?skip=999999.
        #    The SDK's get_crawl_status (and the raw endpoint without skip)
        #    returns ALL completed docs on every call, making each poll take
        #    30s+ for a 10-page job.  Using skip=N past the end returns a
        #    129-byte metadata-only response in ~1s — the status-only channel
        #    Firecrawl's API otherwise lacks.
        status_url = f"https://api.firecrawl.dev/v2/crawl/{job_id}?skip=999999"
        status_headers = {"Authorization": f"Bearer {settings.firecrawl_api_key}"}

        start_ts = time.time()
        last_completed = -1
        final_status: dict | None = None
        try:
            while True:
                elapsed = time.time() - start_ts
                if elapsed > max_wait_seconds:
                    logger.warning(
                        "firecrawl_crawl_timeout", seed=seed, job_id=job_id,
                        elapsed=int(elapsed),
                    )
                    try:
                        fc.cancel_crawl(job_id)
                    except Exception as exc:
                        logger.warning(
                            "firecrawl_cancel_failed", job_id=job_id, error=str(exc),
                        )
                    break

                try:
                    r = httpx.get(status_url, headers=status_headers, timeout=30.0)
                    r.raise_for_status()
                    status = r.json()
                except Exception as exc:
                    logger.warning(
                        "firecrawl_poll_failed", job_id=job_id, error=str(exc),
                    )
                    time.sleep(poll_interval)
                    continue

                completed = status.get("completed", 0)
                total = status.get("total", 0)
                credits_used = status.get("creditsUsed", 0)
                state = status.get("status", "unknown")

                if completed != last_completed:
                    logger.info(
                        "firecrawl_crawl_progress",
                        seed=seed, job_id=job_id, status=state,
                        completed=completed, total=total,
                        credits=credits_used, elapsed=int(elapsed),
                    )
                    last_completed = completed

                if state in ("completed", "failed", "cancelled"):
                    final_status = status
                    break

                time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.warning("firecrawl_interrupted_cancelling", seed=seed, job_id=job_id)
            try:
                fc.cancel_crawl(job_id)
            except Exception:
                pass
            raise

        # 3. Collect docs — now pull the full payload via SDK (auto-paginates).
        seed_docs: list[Any] = []
        if final_status is not None and final_status.get("status") == "completed":
            try:
                full_job = fc.get_crawl_status(job_id)
            except Exception as exc:
                logger.warning(
                    "firecrawl_final_pull_failed", job_id=job_id, error=str(exc),
                )
                full_job = None

            if full_job is not None:
                seed_docs.extend(full_job.data or [])
                next_url = full_job.next
                while next_url:
                    try:
                        page = fc.get_crawl_status_page(next_url)
                    except Exception as exc:
                        logger.warning(
                            "firecrawl_page_failed", job_id=job_id, error=str(exc),
                        )
                        break
                    seed_docs.extend(page.data or [])
                    next_url = page.next

            logger.info(
                "firecrawl_crawl_done", seed=seed, job_id=job_id,
                pages=len(seed_docs),
                credits=final_status.get("creditsUsed", 0),
                status=final_status.get("status"),
            )

        # 4. Persist + client-side filter
        kept = 0
        for doc in seed_docs:
            html, meta = _doc_to_meta(doc)
            url = meta["url"]
            if not url:
                continue
            reason = filter_reason(url)
            if reason:
                logger.info("firecrawl_crawl_filtered", url=url, reason=reason)
                continue
            error_reason = error_page_reason(meta, html)
            if error_reason:
                logger.info("firecrawl_crawl_error_page_filtered", url=url, reason=error_reason)
                continue
            meta = _save_page(url, html, meta, whitelist)
            results.append(meta)
            kept += 1

        logger.info(
            "firecrawl_crawl_seed_summary",
            seed=seed, fetched=len(seed_docs), kept=kept,
        )
        remaining -= len(seed_docs)

    return results


# ── Mode 2: firecrawl-scrape  (our BFS + Firecrawl per-page) ───────


def _crawl_firecrawl_scrape(
    fc, seed_urls: list[str], max_pages: int, max_depth: int,
    whitelist: list[str], delay: float,
) -> list[dict[str, Any]]:
    """Our BFS logic, using Firecrawl scrape() per URL for JS rendering."""
    fc_config = crawl_config.get("firecrawl", {})
    scrape_timeout_ms = fc_config.get("scrape_timeout_ms", 60000)
    queue: list[tuple[str, int]] = [(u, 0) for u in seed_urls]
    visited: set[str] = set()
    results: list[dict[str, Any]] = []

    while queue and len(results) < max_pages:
        url, depth = queue.pop(0)
        url = url.split("#")[0].split("?")[0]
        if url in visited or depth > max_depth or not _matches_whitelist(url, whitelist):
            continue
        visited.add(url)
        logger.info("firecrawl_scrape", url=url, depth=depth, done=len(results))

        try:
            doc = fc.scrape(
                url,
                formats=["html", "markdown"],
                only_main_content=True,
                timeout=scrape_timeout_ms,
            )
        except Exception as exc:
            logger.warning("firecrawl_scrape_failed", url=url, error=str(exc))
            continue

        html, meta = _doc_to_meta(doc, url)
        if not html and not meta.get("markdown"):
            logger.warning("firecrawl_empty", url=url)
            continue

        error_reason = error_page_reason(meta, html)
        if error_reason:
            logger.info("firecrawl_scrape_error_page_filtered", url=url, reason=error_reason)
            continue

        meta = _save_page(url, html, meta, whitelist)
        results.append(meta)

        for link in meta.get("outgoing_links", []):
            link_url = link.get("url", "")
            if link_url and link_url not in visited:
                queue.append((link_url, depth + 1))

        if delay > 0:
            time.sleep(delay)

    return results


# ── Mode 3: firecrawl-map  (URL discovery only) ────────────────────


def _crawl_firecrawl_map(
    fc, seed_urls: list[str], max_pages: int,
) -> list[dict[str, Any]]:
    """Use Firecrawl /map to discover URLs (no content fetched)."""
    all_urls: list[str] = []
    drop_counts: dict[str, int] = {}
    for seed in seed_urls:
        logger.info("firecrawl_map", seed=seed)
        try:
            result = fc.map(seed, limit=max_pages, include_subdomains=False)
            links = result.links or []
            for link in links:
                url = link.url if hasattr(link, "url") else str(link)
                if not url or url in all_urls:
                    continue
                reason = filter_reason(url)
                if reason:
                    drop_counts[reason] = drop_counts.get(reason, 0) + 1
                    continue
                all_urls.append(url)
        except Exception as exc:
            logger.error("firecrawl_map_failed", seed=seed, error=str(exc))

    logger.info("firecrawl_map_done", total_urls=len(all_urls), dropped=drop_counts)

    results: list[dict[str, Any]] = []
    for url in all_urls[:max_pages]:
        meta = {
            "url": url, "title": "", "meta_description": "",
            "department": _extract_department(url),
            "page_type": _classify_page_type(url),
            "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "outgoing_links": [], "html_path": "",
        }
        results.append(meta)
    return results


# ── Mode 4: httpx  (original, no JS) ───────────────────────────────


async def _crawl_httpx(
    seed_urls: list[str], max_pages: int, max_depth: int,
    whitelist: list[str], delay: float,
) -> list[dict[str, Any]]:
    queue: list[tuple[str, int]] = [(u, 0) for u in seed_urls]
    visited: set[str] = set()
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True,
        headers={"User-Agent": crawl_config.get("user_agent", "AgentRAG-PolyU/0.1")},
    ) as client:
        while queue and len(results) < max_pages:
            url, depth = queue.pop(0)
            url = url.split("#")[0].split("?")[0]
            if url in visited or depth > max_depth or not _matches_whitelist(url, whitelist):
                continue
            visited.add(url)
            logger.info("crawling", url=url, depth=depth, done=len(results), fetcher="httpx")

            try:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text
            except Exception as exc:
                logger.warning("crawl_failed", url=url, error=str(exc))
                continue

            if len(html.strip()) < 200:
                logger.warning("empty_page", url=url, size=len(html))

            meta = _extract_metadata_from_html(html, url)
            error_reason = error_page_reason(meta, html)
            if error_reason:
                logger.info("httpx_error_page_filtered", url=url, reason=error_reason)
                continue
            meta = _save_page(url, html, meta, whitelist)
            results.append(meta)

            for link in meta.get("outgoing_links", []):
                if link["url"] not in visited:
                    queue.append((link["url"], depth + 1))

            await asyncio.sleep(delay)

    return results


# ── Mode 5: jina  (Jina Reader, JS rendered, frontier-driven) ─────


JINA_READER_URL = "https://r.jina.ai/"
DATA_ROOT = Path(__file__).resolve().parents[3] / "data"
FRONTIER_PATH = DATA_ROOT / "frontier.jsonl"


def _load_frontier(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(
            f"Frontier file not found: {path}. "
            "Run `python -m scripts.build_frontier` first."
        )
    urls: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                urls.append(json.loads(line)["url"])
            except (json.JSONDecodeError, KeyError):
                continue
    return urls


def _existing_urls() -> set[str]:
    """All URLs already persisted under data/metadata — used for resume dedup."""
    crawled: set[str] = set()
    if not META_DIR.exists():
        return crawled
    for path in META_DIR.glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        url = (meta.get("url") or "").split("#")[0].split("?")[0]
        if url:
            crawled.add(url)
    return crawled


def _resolve_data_path(value: str | Path, default_name: str) -> Path:
    """Resolve a config-provided path against the project data dir."""
    p = Path(value) if value else DATA_ROOT / default_name
    if not p.is_absolute():
        p = (DATA_ROOT.parent / p).resolve()
    return p


class JinaProgress:
    """Disk-backed progress tracker for the jina fetcher.

    Records sets of URLs in three states (succeeded / failed / skipped) plus
    summary stats (started/last_url/last_flush_at).  Flushed atomically every
    ``flush_every`` operations and on ``close()`` so that a crash leaves a
    consistent file.  On startup, all three sets are loaded back so a resume
    skips work that's already been done.
    """

    def __init__(self, path: Path, flush_every: int = 10):
        self.path = path
        self.flush_every = max(1, int(flush_every))
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.succeeded: set[str] = set()
        self.failed: set[str] = set()
        self.skipped: set[str] = set()
        self.last_url: str = ""
        self.last_flush_at: str = ""
        self._dirty_count = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("jina_progress_load_failed", path=str(self.path), error=str(exc))
            return
        self.succeeded = set(data.get("succeeded", []))
        self.failed = set(data.get("failed", []))
        self.skipped = set(data.get("skipped", []))
        # Carry forward original start; mark this run's resume time separately.
        self.started_at = data.get("started_at", self.started_at)
        self.last_url = data.get("last_url", "")

    def already_seen(self, url: str, retry_failed: bool = False) -> bool:
        if url in self.succeeded or url in self.skipped:
            return True
        if url in self.failed and not retry_failed:
            return True
        return False

    def mark(self, url: str, status: str) -> None:
        # Move between sets so retries don't leave stale entries.
        self.succeeded.discard(url)
        self.failed.discard(url)
        self.skipped.discard(url)
        if status == "succeeded":
            self.succeeded.add(url)
        elif status == "failed":
            self.failed.add(url)
        elif status == "skipped":
            self.skipped.add(url)
        self.last_url = url
        self._dirty_count += 1
        if self._dirty_count >= self.flush_every:
            self.flush()

    def stats(self, frontier_total: int, pending_total: int) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "last_flush_at": self.last_flush_at,
            "frontier_total": frontier_total,
            "pending_total": pending_total,
            "succeeded": len(self.succeeded),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "remaining": max(pending_total - len(self.succeeded) - len(self.skipped), 0),
            "last_url": self.last_url,
        }

    def flush(self, frontier_total: int = 0, pending_total: int = 0) -> None:
        self.last_flush_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload = {
            **self.stats(frontier_total, pending_total),
            "succeeded": sorted(self.succeeded),
            "failed": sorted(self.failed),
            "skipped": sorted(self.skipped),
        }
        # Atomic write: temp file + replace.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        self._dirty_count = 0

    def close(self, frontier_total: int = 0, pending_total: int = 0) -> None:
        self.flush(frontier_total, pending_total)


def _jina_request(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    max_retries: int,
    backoff: float,
) -> tuple[str | None, str | None]:
    """Single URL fetch with retry.  Returns (html, error_str)."""
    last_error = "unknown"
    for attempt in range(max_retries + 1):
        try:
            resp = client.get(JINA_READER_URL + url, headers=headers)
        except httpx.HTTPError as exc:
            last_error = f"network:{type(exc).__name__}:{exc}"
            if attempt < max_retries:
                time.sleep(backoff * (attempt + 1))
                continue
            return None, last_error

        status = resp.status_code
        # Retry on 5xx / 408 / 429.  4xx otherwise = permanent fail.
        if status >= 500 or status in (408, 429):
            last_error = f"http_{status}"
            if attempt < max_retries:
                # 429: try Retry-After if present.
                if status == 429:
                    try:
                        wait = float(resp.headers.get("retry-after", backoff * (attempt + 1)))
                    except ValueError:
                        wait = backoff * (attempt + 1)
                else:
                    wait = backoff * (attempt + 1)
                time.sleep(wait)
                continue
            return None, last_error
        if status >= 400:
            return None, f"http_{status}"

        return resp.text, None

    return None, last_error


def _crawl_jina(
    seed_urls: list[str],  # ignored — jina mode is frontier-driven
    max_pages: int,
    whitelist: list[str],
    delay: float,
) -> list[dict[str, Any]]:
    """Fetch URLs from data/frontier.jsonl via Jina Reader (r.jina.ai).

    Resumable: progress is checkpointed to ``data/jina_progress.json`` and
    URLs already marked succeeded/skipped/failed are skipped on rerun.
    Failures are also appended to ``data/jina_failures.jsonl`` for inspection.
    Honors all knobs under ``crawl.yaml`` ``jina:`` (concurrency, retries,
    engine, timeouts, min_html_size, …).
    """
    api_key = settings.jina_api_key
    if not api_key:
        raise ValueError("JINA_API_KEY not set. Get one at https://jina.ai/reader/")

    cfg = crawl_config.get("jina", {}) or {}
    concurrency = max(1, int(cfg.get("concurrency", 1)))
    timeout = float(cfg.get("request_timeout_seconds", 90))
    max_retries = int(cfg.get("max_retries", 2))
    backoff = float(cfg.get("retry_backoff_seconds", 5.0))
    min_html_size = int(cfg.get("min_html_size", 200))
    engine = (cfg.get("engine") or "").strip()
    with_links_summary = bool(cfg.get("with_links_summary", False))
    flush_every = int(cfg.get("flush_every", 10))
    retry_failed = bool(cfg.get("retry_failed_on_resume", False))

    progress_path = _resolve_data_path(cfg.get("progress_path"), "jina_progress.json")
    failure_log_path = _resolve_data_path(cfg.get("failure_log_path"), "jina_failures.jsonl")

    frontier = _load_frontier(FRONTIER_PATH)
    crawled = _existing_urls()
    progress = JinaProgress(progress_path, flush_every=flush_every)

    # Build pending list — exclude anything already on disk OR recorded in
    # progress as terminal (succeeded/skipped, plus failed unless retrying).
    pending: list[str] = []
    skipped_existing = 0
    skipped_progress = 0
    for url in frontier:
        clean = url.split("#")[0].split("?")[0]
        if clean in crawled:
            skipped_existing += 1
            continue
        if progress.already_seen(clean, retry_failed=retry_failed):
            skipped_progress += 1
            continue
        pending.append(clean)

    logger.info(
        "jina_frontier_loaded",
        frontier_total=len(frontier),
        already_on_disk=skipped_existing,
        already_in_progress=skipped_progress,
        pending=len(pending),
        succeeded_so_far=len(progress.succeeded),
        failed_so_far=len(progress.failed),
        skipped_so_far=len(progress.skipped),
    )

    if not pending:
        logger.info("jina_nothing_to_do")
        progress.close(len(frontier), 0)
        return []

    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Return-Format": "html",
    }
    if engine:
        headers["X-Engine"] = engine
    if with_links_summary:
        headers["X-With-Links-Summary"] = "true"

    failure_log_path.parent.mkdir(parents=True, exist_ok=True)
    failure_fp = failure_log_path.open("a", encoding="utf-8")

    results: list[dict[str, Any]] = []
    pending_total = len(pending)
    work_budget = min(max_pages, pending_total)

    def _record_failure(url: str, reason: str) -> None:
        failure_fp.write(
            json.dumps({"url": url, "reason": reason, "ts": time.time()}, ensure_ascii=False) + "\n"
        )
        failure_fp.flush()
        progress.mark(url, "failed")

    def _process(client: httpx.Client, url: str) -> dict[str, Any] | None:
        if not _matches_whitelist(url, whitelist):
            progress.mark(url, "skipped")
            return None
        reason = filter_reason(url)
        if reason:
            logger.info("jina_filtered", url=url, reason=reason)
            progress.mark(url, "skipped")
            return None

        html, err = _jina_request(client, url, headers, max_retries, backoff)
        if err is not None:
            logger.warning("jina_fetch_failed", url=url, error=err)
            _record_failure(url, err)
            return None

        if not html or len(html.strip()) < min_html_size:
            logger.warning("jina_empty", url=url, size=len(html or ""))
            _record_failure(url, "empty_html")
            return None

        meta = _extract_metadata_from_html(html, url)
        error_reason = error_page_reason(meta, html)
        if error_reason:
            logger.info("jina_error_page_filtered", url=url, reason=error_reason)
            progress.mark(url, "skipped")
            return None

        meta = _save_page(url, html, meta, whitelist)
        progress.mark(url, "succeeded")
        return meta

    try:
        if concurrency == 1:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                for idx, url in enumerate(pending):
                    if len(results) >= max_pages:
                        break
                    logger.info(
                        "jina_fetch",
                        url=url, done=len(results),
                        progress=f"{idx + 1}/{pending_total}",
                        ok=len(progress.succeeded),
                        failed=len(progress.failed),
                        skipped=len(progress.skipped),
                    )
                    meta = _process(client, url)
                    if meta:
                        results.append(meta)
                    if delay > 0:
                        time.sleep(delay)
        else:
            # Concurrent variant: fan out via a thread pool.  httpx.Client is
            # thread-safe per the docs as long as each thread owns its own
            # request lifecycle, which we satisfy here.
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with httpx.Client(
                timeout=timeout, follow_redirects=True,
                limits=httpx.Limits(max_connections=concurrency * 2),
            ) as client:
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    inflight: dict[Any, str] = {}
                    cursor = 0

                    def _submit_next() -> bool:
                        nonlocal cursor
                        while cursor < pending_total and len(inflight) < concurrency:
                            if len(results) + len(inflight) >= work_budget:
                                return False
                            url = pending[cursor]
                            cursor += 1
                            fut = pool.submit(_process, client, url)
                            inflight[fut] = url
                        return bool(inflight)

                    while _submit_next():
                        for fut in as_completed(list(inflight.keys())):
                            url = inflight.pop(fut)
                            try:
                                meta = fut.result()
                            except Exception as exc:
                                logger.warning("jina_worker_crashed", url=url, error=str(exc))
                                _record_failure(url, f"worker_exception:{exc}")
                                meta = None
                            if meta:
                                results.append(meta)
                            done_total = len(progress.succeeded) + len(progress.failed) + len(progress.skipped)
                            logger.info(
                                "jina_progress",
                                done=len(results),
                                seen=done_total,
                                ok=len(progress.succeeded),
                                failed=len(progress.failed),
                                skipped=len(progress.skipped),
                                remaining=max(pending_total - done_total, 0),
                                last_url=url,
                            )
                            if len(results) >= max_pages:
                                break
                            break  # re-enter outer loop to submit more
                        if len(results) >= max_pages:
                            break
    except KeyboardInterrupt:
        logger.warning("jina_interrupted_flushing_progress")
        raise
    finally:
        failure_fp.close()
        progress.close(len(frontier), pending_total)

    logger.info(
        "jina_done",
        fetched=len(results),
        ok=len(progress.succeeded),
        failed=len(progress.failed),
        skipped=len(progress.skipped),
        progress_path=str(progress_path),
        failure_log=str(failure_log_path),
    )
    return results


# ── Priority-URL backfill ────────────────────────────────────────────


def _backfill_priority_urls(
    results: list[dict[str, Any]],
    fetcher: str,
    whitelist: list[str],
    max_depth: int,
) -> list[dict[str, Any]]:
    """Scrape programme subpages that the main crawl missed.

    Scans outgoing_links of already-crawled pages and fetches any that
    match ``priority_url_patterns`` from crawl.yaml but were not crawled.
    """
    patterns = crawl_config.get("priority_url_patterns", [])
    if not patterns:
        return results

    crawled_urls = {r["url"] for r in results}
    compiled = [re.compile(p) for p in patterns]

    missing: list[str] = []
    for page in results:
        for link in page.get("outgoing_links", []):
            url = link.get("url", "")
            if url and url not in crawled_urls:
                if any(rx.search(url) for rx in compiled):
                    missing.append(url)
                    crawled_urls.add(url)

    if not missing:
        return results

    logger.info("backfill_start", missing_priority_pages=len(missing))

    if fetcher in ("firecrawl-crawl", "firecrawl-scrape"):
        fc = _get_firecrawl()
        fc_config = crawl_config.get("firecrawl", {})
        scrape_timeout_ms = fc_config.get("scrape_timeout_ms", 60000)
        for url in missing:
            try:
                doc = fc.scrape(
                    url,
                    formats=["html", "markdown"],
                    only_main_content=True,
                    timeout=scrape_timeout_ms,
                )
                html, meta = _doc_to_meta(doc, url)
                if html or meta.get("markdown"):
                    error_reason = error_page_reason(meta, html)
                    if error_reason:
                        logger.info("backfill_error_page_filtered", url=url, reason=error_reason)
                        continue
                    meta = _save_page(url, html, meta, whitelist)
                    results.append(meta)
                    logger.info("backfill_ok", url=url)
                else:
                    logger.warning("backfill_empty", url=url)
            except Exception as exc:
                logger.warning("backfill_failed", url=url, error=str(exc))
    else:
        import asyncio as _aio

        async def _fetch(url: str) -> dict[str, Any] | None:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=True,
                headers={"User-Agent": crawl_config.get("user_agent", "AgentRAG-PolyU/0.1")},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text
                meta = _extract_metadata_from_html(html, url)
                error_reason = error_page_reason(meta, html)
                if error_reason:
                    logger.info("backfill_error_page_filtered", url=url, reason=error_reason)
                    return None
                return _save_page(url, html, meta, whitelist)

        for url in missing:
            try:
                meta = _aio.run(_fetch(url))
                if meta:
                    results.append(meta)
                    logger.info("backfill_ok", url=url)
            except Exception as exc:
                logger.warning("backfill_failed", url=url, error=str(exc))

    logger.info("backfill_complete", added=len(missing))
    return results


# ── Public API ──────────────────────────────────────────────────────


async def crawl_site(
    seed_urls: list[str] | None = None,
    max_pages: int | None = None,
    max_depth: int | None = None,
    fetcher: str | None = None,
) -> list[dict[str, Any]]:
    """BFS crawl starting from seed URLs.

    fetcher choices:
      httpx             — fast, no JS
      firecrawl-scrape  — our BFS + Firecrawl per-page JS rendering
      firecrawl-crawl   — server-side crawl (recommended)
      firecrawl-map     — URL discovery only, no content
    """
    seed_urls = seed_urls or crawl_config.get("seed_urls", [])
    max_pages = max_pages or crawl_config.get("max_pages", 500)
    max_depth = max_depth or crawl_config.get("max_depth", 3)
    whitelist = crawl_config.get("domain_whitelist", ["*.polyu.edu.hk"])
    delay = settings.crawl_delay_seconds
    fetcher = fetcher or crawl_config.get("fetcher", "httpx")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    META_DIR.mkdir(parents=True, exist_ok=True)

    fc_config = crawl_config.get("firecrawl", {})
    include_paths = fc_config.get("include_paths")
    exclude_paths = fc_config.get("exclude_paths")
    per_seed_limit = fc_config.get("per_seed_limit")
    poll_interval = fc_config.get("poll_interval", 10)
    max_wait_seconds = fc_config.get("max_wait_seconds", 1800)

    logger.info(
        "crawl_start", fetcher=fetcher, seeds=len(seed_urls),
        max_pages=max_pages, per_seed_limit=per_seed_limit,
    )

    if fetcher == "firecrawl-crawl":
        fc = _get_firecrawl()
        results = _crawl_firecrawl_crawl(
            fc, seed_urls, max_pages, max_depth,
            whitelist, include_paths, exclude_paths,
            per_seed_limit=per_seed_limit,
            poll_interval=poll_interval,
            max_wait_seconds=max_wait_seconds,
        )
    elif fetcher == "firecrawl-scrape":
        fc = _get_firecrawl()
        results = _crawl_firecrawl_scrape(
            fc, seed_urls, max_pages, max_depth, whitelist, delay,
        )
    elif fetcher == "firecrawl-map":
        fc = _get_firecrawl()
        results = _crawl_firecrawl_map(fc, seed_urls, max_pages)
    elif fetcher == "jina":
        results = _crawl_jina(seed_urls, max_pages, whitelist, delay)
    else:
        results = await _crawl_httpx(seed_urls, max_pages, max_depth, whitelist, delay)

    logger.info("crawl_complete", fetcher=fetcher, total_pages=len(results))
    return results


def run_crawl(**kwargs: Any) -> list[dict[str, Any]]:
    """Synchronous wrapper."""
    return asyncio.run(crawl_site(**kwargs))
