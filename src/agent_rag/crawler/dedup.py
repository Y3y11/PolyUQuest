"""Cross-URL content dedup for the offline pipeline.

The crawler intentionally fetches the PolyU site broadly, so the same content
ends up under many URLs: scheme variants (`http://` vs `https://`), trailing
slash variants, subdomain mirrors (`shtm.polyu.edu.hk` vs
`polyu.edu.hk/shtm/`), and cross-program duplicates (subject lists copied
verbatim across four MSc programs). On top of that, ~150 pages are pure
JS-rendered shells that parse to zero blocks, and ~30 are auth gates whose
single-block content is the same "JavaScript required + NetID sign-in" notice
under dozens of different paths.

Left alone, these surface as first-stage retrieval noise: the reranker keeps
picking the gold block AND its three near-identical mirrors, displacing the
second-best non-mirror evidence out of the top-K.

`build_dedup_map` consumes `01_pages.jsonl` (with `content_hash` already
stamped by `stage_blocks`) and a per-URL block count, and returns three sets:

- ``drop_urls``     : delete these pages outright (empty shells, auth gates)
- ``canonical_map`` : ``{mirror_url: canonical_url}`` for content mirrors
- ``alias_set``     : the keys of canonical_map, materialized for fast lookup

Canonical picker is deterministic and rule-based — same hash → same canonical
across runs. Picker order: ``https`` > ``http``; ``www.polyu.edu.hk`` >
subdomain; no trailing slash > trailing slash; shorter URL > longer; alpha.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlparse

# SHA256[:16] of the empty string — pages with zero blocks all collide here.
_EMPTY_CONTENT_HASH = "e3b0c44298fc1c14"


def _canonical_key(url: str) -> tuple:
    """Sort key for canonical picking — minimum wins.

    Tuple slots (lower = better):
      0. scheme bucket: 0 = https, 1 = http
      1. host bucket:   0 = www.polyu.edu.hk, 1 = anything else
      2. slash bucket:  0 = no trailing slash, 1 = trailing slash
      3. length:        shorter URL wins ties
      4. url:           lexicographic last-resort
    """
    parsed = urlparse(url)
    scheme = 0 if parsed.scheme == "https" else 1
    host = 0 if parsed.netloc == "www.polyu.edu.hk" else 1
    no_slash = 0 if not parsed.path.endswith("/") or parsed.path == "/" else 1
    return (scheme, host, no_slash, len(url), url)


def pick_canonical(urls: Iterable[str]) -> str:
    """Return the canonical URL from a content-equivalence group."""
    return min(urls, key=_canonical_key)


@dataclass
class DedupStats:
    total_pages: int = 0
    unique_hashes: int = 0
    empty_shells_dropped: int = 0
    large_group_dropped: int = 0
    mirror_groups: int = 0
    mirrors_collapsed: int = 0
    drop_group_examples: list[dict] = field(default_factory=list)
    mirror_group_examples: list[dict] = field(default_factory=list)


@dataclass
class DedupResult:
    drop_urls: set[str]
    canonical_map: dict[str, str]   # mirror_url -> canonical_url
    stats: DedupStats

    @property
    def alias_set(self) -> set[str]:
        return set(self.canonical_map.keys())

    def is_kept(self, url: str) -> bool:
        return url not in self.drop_urls and url not in self.canonical_map

    def resolve(self, url: str) -> str | None:
        """Return the URL a link should point at: canonical if mirror,
        None if dropped, url itself if kept."""
        if url in self.drop_urls:
            return None
        return self.canonical_map.get(url, url)


def build_dedup_map(
    pages: list[dict],
    blocks_per_url: dict[str, int],
    *,
    max_group_size: int = 20,
    drop_empty_content: bool = True,
) -> DedupResult:
    """Classify pages into drop / mirror / keep based on content_hash groups.

    Rules applied in order:

    1. **Empty content** (``drop_empty_content=True``): groups whose
       content_hash matches the empty-string SHA256 prefix, OR groups where
       every page has zero blocks → DROP all URLs in group.
    2. **Large group** (``group_size >= max_group_size``): treated as a
       structural error class (auth wall, "page not found", etc.) where the
       same content showing up under 20+ distinct paths is by itself enough
       signal that the content is uninformative → DROP all URLs.
    3. **Mirror group** (``2 <= group_size < max_group_size``): one URL
       wins via the canonical picker, the rest become aliases.
    4. **Singleton** (``group_size == 1``): kept as-is (not added to any map).
    """
    groups: dict[str, list[str]] = {}
    for p in pages:
        h = p.get("content_hash") or ""
        url = p.get("url") or ""
        if not url or not h:
            continue
        groups.setdefault(h, []).append(url)

    stats = DedupStats(total_pages=len(pages), unique_hashes=len(groups))
    drop_urls: set[str] = set()
    canonical_map: dict[str, str] = {}

    for h, urls in groups.items():
        size = len(urls)

        # Rule 1: empty content (SHA256 of "" prefix OR all 0 blocks).
        all_zero = all(blocks_per_url.get(u, 0) == 0 for u in urls)
        is_empty_hash = h.startswith(_EMPTY_CONTENT_HASH[:10])
        if drop_empty_content and (is_empty_hash or all_zero):
            drop_urls.update(urls)
            stats.empty_shells_dropped += size
            if len(stats.drop_group_examples) < 5:
                stats.drop_group_examples.append({
                    "reason": "empty_shell",
                    "hash": h,
                    "size": size,
                    "sample_urls": urls[:3],
                })
            continue

        # Rule 2: large group (auth wall / generic error page).
        if size >= max_group_size:
            drop_urls.update(urls)
            stats.large_group_dropped += size
            if len(stats.drop_group_examples) < 10:
                stats.drop_group_examples.append({
                    "reason": "large_group",
                    "hash": h,
                    "size": size,
                    "sample_urls": urls[:3],
                })
            continue

        # Rule 4: singleton (nothing to do).
        if size == 1:
            continue

        # Rule 3: mirror group.
        canonical = pick_canonical(urls)
        stats.mirror_groups += 1
        stats.mirrors_collapsed += size - 1
        for u in urls:
            if u != canonical:
                canonical_map[u] = canonical
        if len(stats.mirror_group_examples) < 10:
            stats.mirror_group_examples.append({
                "hash": h,
                "size": size,
                "canonical": canonical,
                "aliases": [u for u in urls if u != canonical],
            })

    return DedupResult(
        drop_urls=drop_urls,
        canonical_map=canonical_map,
        stats=stats,
    )


def build_block_remap(
    blocks: list[dict],
    dedup: DedupResult,
) -> dict[str, str]:
    """Build a ``{mirror_block_id: canonical_block_id}`` remap.

    Block IDs are derived from ``md5(url|dom_path)``, so a piece of content
    appearing on both a canonical and a mirror URL has two distinct IDs. After
    dedup, the mirror's block_id will not exist in the index; anything that
    references it (eval gold annotations, downstream tools) needs to point at
    the canonical's equivalent.

    Matching strategy: group blocks by URL, then for each mirror page, find
    a canonical block whose ``content`` field matches exactly. We verified
    100% match on V5/V4/V3 gold blocks because the cross-URL mirrors really
    do have byte-identical block content (selectolax extraction is
    deterministic for the same HTML body).

    Mirrors with no content match (e.g. blocks unique to the mirror despite
    matching content_hash at page level) are silently skipped — they were
    going to be dropped anyway.
    """
    blocks_by_url: dict[str, list[dict]] = {}
    for b in blocks:
        blocks_by_url.setdefault(b.get("url", ""), []).append(b)

    remap: dict[str, str] = {}
    for mirror_url, canonical_url in dedup.canonical_map.items():
        canon_blocks = blocks_by_url.get(canonical_url, [])
        if not canon_blocks:
            continue
        # Build content -> canonical_block_id index for this canonical page.
        canon_by_content: dict[str, str] = {}
        for cb in canon_blocks:
            content = cb.get("content", "")
            if content and content not in canon_by_content:
                canon_by_content[content] = cb.get("block_id", "")
        for mb in blocks_by_url.get(mirror_url, []):
            content = mb.get("content", "")
            canon_bid = canon_by_content.get(content)
            if canon_bid and canon_bid != mb.get("block_id", ""):
                remap[mb["block_id"]] = canon_bid
    return remap


@dataclass
class BlockContentDedupStats:
    n_groups: int = 0                      # groups with ≥ min_group_size distinct URLs
    n_blocks_collapsed: int = 0            # non-canonical blocks (entries in the remap)
    examples: list[dict] = field(default_factory=list)


def _block_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def build_block_content_dedup(
    blocks: list[dict],
    *,
    dedup: DedupResult | None = None,
    min_group_size: int = 2,
) -> tuple[dict[str, str], BlockContentDedupStats]:
    """Cross-URL block-content dedup.

    Some pages aren't URL mirrors of each other but still embed byte-identical
    blocks ("Contact Us", "Programme Director: X", boilerplate subject
    descriptions copied across MSc programmes). After URL-level mirror dedup,
    these still occupy N first-stage candidate slots for one piece of content
    and displace non-duplicate evidence.

    Returns ``({non_canonical_block_id: canonical_block_id}, stats)``.

    Operates on the post-URL-dedup block set: if ``dedup`` is given, blocks on
    dropped/mirror URLs are excluded before grouping, so the returned remap is
    safe to compose with the URL-mirror remap from ``build_block_remap`` via
    ``merge_block_remaps``.

    Canonical picker:
      1. Among URLs in the content cluster, pick the URL via ``pick_canonical``
         (https > http, www > sub, no-slash > slash, shortest, alpha).
      2. Among blocks on that URL with matching content, pick the one with
         lex-smallest ``html_tag_path`` for a deterministic tiebreak.

    Within-page duplicates (same content, same URL — vanishingly rare since
    ``block_tree`` already filters min_words=20) are not collapsed; only
    cross-URL duplicates are.
    """
    if dedup is not None:
        excluded_urls = dedup.drop_urls | set(dedup.canonical_map.keys())
        blocks = [b for b in blocks if b.get("url", "") not in excluded_urls]

    # Group by content_hash; track (url, block_id, html_tag_path) per entry.
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for b in blocks:
        content = b.get("content", "") or ""
        if not content:
            continue
        url = b.get("url", "") or ""
        bid = b.get("block_id", "") or ""
        path = b.get("html_tag_path", "") or ""
        if not url or not bid:
            continue
        h = _block_content_hash(content)
        groups.setdefault(h, []).append((url, bid, path))

    remap: dict[str, str] = {}
    stats = BlockContentDedupStats()

    for h, entries in groups.items():
        # Distinct URLs in this content group.
        urls_in_group = {u for u, _, _ in entries}
        if len(urls_in_group) < min_group_size:
            continue

        canon_url = pick_canonical(urls_in_group)
        # Among blocks on canon_url with this content, pick lex-smallest path.
        canon_candidates = sorted(
            ((p, bid) for u, bid, p in entries if u == canon_url),
        )
        if not canon_candidates:
            # Shouldn't happen — canon_url was picked from this set.
            continue
        canon_bid = canon_candidates[0][1]

        stats.n_groups += 1
        collapsed_this_group = 0
        for u, bid, _path in entries:
            if bid == canon_bid:
                continue
            # If two blocks on the same URL share content, only the first
            # (smallest dom path) survives — also collapse the second-on-page
            # to the canonical. This is correct: the second copy is a true
            # duplicate that should not occupy a candidate slot.
            remap[bid] = canon_bid
            collapsed_this_group += 1
        stats.n_blocks_collapsed += collapsed_this_group

        if len(stats.examples) < 10:
            # Snippet for human eyeball.
            snippet_src = next((b for b in blocks if b.get("block_id") == canon_bid), None)
            snippet = (snippet_src or {}).get("content", "")[:120]
            stats.examples.append({
                "hash": h,
                "group_size_urls": len(urls_in_group),
                "blocks_collapsed": collapsed_this_group,
                "canonical_block_id": canon_bid,
                "canonical_url": canon_url,
                "sample_other_urls": sorted(urls_in_group - {canon_url})[:3],
                "content_snippet": snippet,
            })

    return remap, stats


def merge_block_remaps(*remaps: dict[str, str]) -> dict[str, str]:
    """Compose multiple block remaps into a flat ``{block_id: final_canonical_id}``.

    Resolves chains transitively so a downstream consumer never has to walk
    more than one hop. Order matters when two remaps disagree on the
    canonical: later entries override earlier ones (so pass URL-mirror remap
    first, then content remap, since content dedup runs after URL dedup
    semantically).
    """
    merged: dict[str, str] = {}
    for r in remaps:
        for k, v in r.items():
            merged[k] = v

    out: dict[str, str] = {}
    for k in merged:
        seen = {k}
        cur = merged[k]
        while cur in merged and cur not in seen:
            seen.add(cur)
            cur = merged[cur]
        if cur != k:
            out[k] = cur
    return out


def rewrite_links(
    links: list[dict],
    dedup: DedupResult,
) -> list[dict]:
    """Rewrite a list of ``{from_url, to_url, ...}`` link rows through dedup.

    - Drop links whose source URL is dropped or a mirror (source page won't
      exist after dedup; emit-side links from canonical pages cover them).
    - Drop links whose target URL is dropped (target page won't exist).
    - Rewrite ``to_url`` to canonical when the target is a mirror.
    - Deduplicate on ``(from_url, to_url, link_type)`` after rewrite.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for link in links:
        src = link.get("from_url", "")
        tgt = link.get("to_url", "")
        if not src or not tgt:
            continue
        if src in dedup.drop_urls or src in dedup.canonical_map:
            continue
        new_tgt = dedup.resolve(tgt)
        if new_tgt is None:
            continue
        if new_tgt == src:
            # Self-loop after canonicalization — skip.
            continue
        key = (src, new_tgt, link.get("link_type", "hyperlink"))
        if key in seen:
            continue
        seen.add(key)
        out.append({**link, "to_url": new_tgt})
    return out
