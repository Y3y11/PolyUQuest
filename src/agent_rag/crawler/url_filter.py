"""URL-level filter for the PolyU crawler.

Applies three classes of rules derived from the site-map analysis of
``docs/www.polyu.edu.hk.2026-04-19T11_59_05.155Z.json`` (4997 URLs → 881
after filtering):

  1. Language mirrors    — drop ``/tc/*`` and ``/sc/*`` (duplicates of EN)
  2. Dated sections      — ``/media/media-releases/YYYY/*``, ``/events/YYYY/*``,
                           ``/recent-focus/YYYYMMDD_*`` kept only if YYYY ≥ min_year
  3. Assets + boilerplate — static files (.pdf etc.) and utility pages

Rules have module-level defaults; ``configs/crawl.yaml`` → ``url_filter:`` may
override any subset.

Public API:
    should_crawl(url) -> bool
    filter_urls(urls) -> list[str]
    filter_reason(url) -> str | None   # None when kept; drop reason otherwise
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable
from urllib.parse import urlparse

from agent_rag.config import crawl_config

DEFAULT_DROP_LANG_PREFIXES: tuple[str, ...] = ("tc", "sc")
DEFAULT_DROP_EXTENSIONS: tuple[str, ...] = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".zip",
    ".jpg", ".jpeg", ".png", ".gif", ".mp4",
)
DEFAULT_DROP_EXACT_PATHS: frozenset[str] = frozenset({
    "/notfound.html",
    "/sitemap",
    "/accessibility",
    "/terms-of-use",
    "/privacy-policy-statement",
    "/photo-gallery",
    "/atoz",
    "/about-polyu/photo-gallery",
    "/social-media",
})
DEFAULT_DROP_HOST_SUFFIXES: tuple[str, ...] = ()
DEFAULT_DROP_PATH_PREFIXES: tuple[str, ...] = ()
DEFAULT_DATED_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"^/media/media-releases/(\d{4})/"), 1),
    (re.compile(r"^/events/(\d{4})/"), 1),
    (re.compile(r"^/recent-focus/(\d{4})\d{4}"), 1),  # YYYYMMDD prefix only
)
DEFAULT_MIN_YEAR: int = 2024


def _load_rules() -> dict:
    cfg = crawl_config.get("url_filter", {}) or {}
    if not cfg.get("enabled", True):
        return {"enabled": False}

    lang_prefixes = tuple(cfg.get("drop_language_prefixes", DEFAULT_DROP_LANG_PREFIXES))
    extensions = tuple(e.lower() for e in cfg.get("drop_extensions", DEFAULT_DROP_EXTENSIONS))
    exact_paths = frozenset(cfg.get("drop_exact_paths", DEFAULT_DROP_EXACT_PATHS))
    host_suffixes = tuple(
        h.lower() for h in cfg.get("drop_host_suffixes", DEFAULT_DROP_HOST_SUFFIXES)
    )
    path_prefixes = tuple(
        p.rstrip("/").lower() for p in cfg.get("drop_path_prefixes", DEFAULT_DROP_PATH_PREFIXES)
    )
    host_path_prefixes = tuple(
        (
            item["host_suffix"].lower(),
            item["path_prefix"].rstrip("/").lower(),
        )
        for item in cfg.get("drop_host_path_prefixes", [])
    )

    raw_patterns = cfg.get("dated_path_patterns")
    if raw_patterns:
        dated: list[tuple[re.Pattern[str], int]] = []
        for item in raw_patterns:
            dated.append((re.compile(item["regex"]), int(item.get("group", 1))))
        dated_patterns = tuple(dated)
    else:
        dated_patterns = DEFAULT_DATED_PATTERNS

    return {
        "enabled": True,
        "lang_prefixes": lang_prefixes,
        "extensions": extensions,
        "exact_paths": exact_paths,
        "host_suffixes": host_suffixes,
        "path_prefixes": path_prefixes,
        "host_path_prefixes": host_path_prefixes,
        "dated_patterns": dated_patterns,
        "min_year": int(cfg.get("min_year", DEFAULT_MIN_YEAR)),
        "drop_years_before_min_anywhere": bool(
            cfg.get("drop_years_before_min_anywhere", False)
        ),
    }


_RULES_CACHE: dict | None = None


def _rules() -> dict:
    global _RULES_CACHE
    if _RULES_CACHE is None:
        _RULES_CACHE = _load_rules()
    return _RULES_CACHE


def reset_rules_cache() -> None:
    """Force reload of rules from config (used in tests)."""
    global _RULES_CACHE
    _RULES_CACHE = None
    _normalize_path.cache_clear()
    filter_reason.cache_clear()


@lru_cache(maxsize=4096)
def _normalize_path(url: str) -> str:
    """Lower-case, strip trailing slash (except root)."""
    path = urlparse(url).path.lower()
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


@lru_cache(maxsize=4096)
def filter_reason(url: str) -> str | None:
    """Return drop reason, or None if the URL should be kept."""
    rules = _rules()
    if not rules.get("enabled"):
        return None

    path = _normalize_path(url)
    host = (urlparse(url).hostname or "").lower()

    for suffix in rules["host_suffixes"]:
        if host == suffix or host.endswith(f".{suffix}"):
            return "blocked_host"

    for suffix, prefix in rules["host_path_prefixes"]:
        if host == suffix or host.endswith(f".{suffix}"):
            if path == prefix or path.startswith(f"{prefix}/"):
                return "blocked_path"

    seg1 = path.lstrip("/").split("/", 1)[0]
    if seg1 in rules["lang_prefixes"]:
        return "lang_mirror"

    for ext in rules["extensions"]:
        if path.endswith(ext):
            return "asset"

    if path in rules["exact_paths"]:
        return "boilerplate"

    for prefix in rules["path_prefixes"]:
        if path == prefix or path.startswith(f"{prefix}/"):
            return "blocked_path"

    min_year = rules["min_year"]
    for pat, group in rules["dated_patterns"]:
        m = pat.match(path)
        if m:
            try:
                year = int(m.group(group))
            except (ValueError, IndexError):
                continue
            if year < min_year:
                return "pre_min_year"
            break

    if rules["drop_years_before_min_anywhere"]:
        for year_text in re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", path):
            if int(year_text) < min_year:
                return "pre_min_year"

    return None


def should_crawl(url: str) -> bool:
    return filter_reason(url) is None


def filter_urls(urls: Iterable[str]) -> list[str]:
    return [u for u in urls if should_crawl(u)]
