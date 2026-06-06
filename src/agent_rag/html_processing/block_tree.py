"""Block Tree builder: bottom-up DOM merging with structure-aware chunking."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from selectolax.parser import HTMLParser, Node

from agent_rag.config import thresholds_config

_bt = thresholds_config.get("block_tree", {})
DEFAULT_MAX_WORDS = _bt.get("max_words", 150)
DEFAULT_MIN_WORDS = _bt.get("min_words", 20)

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_NO_SPLIT_TAGS = {"table", "thead", "tbody", "tr", "ul", "ol", "dl", "pre", "code"}


@dataclass
class Block:
    block_id: str
    content: str
    html_tag_path: str
    heading_context: str
    token_count: int
    depth: int
    parent_block_id: str | None = None
    child_index: int = 0
    children: list[Block] = field(default_factory=list)
    url: str = ""


def _word_count(text: str) -> int:
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
    eng = len(text.split())
    return cjk + eng


def _text_of(node: Node) -> str:
    return (node.text(strip=True, separator=" ") or "").strip()


def _children_with_segments(node: Node) -> list[tuple[Node, str]]:
    """Iterate direct children paired with their stable path segment.

    Segment format (deterministic, includes [nth-of-type] for every node without
    a unique id):
      - tag#id              -> id present and unique among siblings
      - tag#id[n]           -> id duplicated among siblings (rare; HTML-invalid but real)
      - tag[n]              -> no id; n is 0-based index among same-tag siblings

    Why we can't use a bottom-up `_tag_path(node)` helper: selectolax's
    `Node.__eq__` compares by content/structure, and `Node` objects do NOT
    have stable Python identity (each property access yields a fresh wrapper),
    so neither `==` nor `is` reliably distinguishes content-equal siblings.
    Building paths top-down — emitting each child's segment as we iterate
    its parent's children — sidesteps the identity problem entirely.
    """
    children = [c for c in node.iter() if c.tag not in ("-text", "-comment", "-undef")]

    # Pre-pass: count id occurrences so we can detect duplicates.
    id_counts: dict[str, int] = {}
    for c in children:
        cid = c.attributes.get("id", "") if c.attributes else ""
        if cid:
            id_counts[cid] = id_counts.get(cid, 0) + 1

    out: list[tuple[Node, str]] = []
    seen_per_tag: dict[str, int] = {}
    for c in children:
        tag = c.tag
        sib_idx = seen_per_tag.get(tag, 0)
        seen_per_tag[tag] = sib_idx + 1
        cid = c.attributes.get("id", "") if c.attributes else ""
        if cid and id_counts.get(cid, 0) == 1:
            seg = f"{tag}#{cid}"
        elif cid:
            seg = f"{tag}#{cid}[{sib_idx}]"
        else:
            seg = f"{tag}[{sib_idx}]"
        out.append((c, seg))
    return out


def _headings_before_branch(root: Node, branch_child: Node | None) -> list[str]:
    """Return h-tag texts found inside `root`'s subtree in DOM order, stopping
    when we reach the child whose subtree contains the target node.

    `branch_child` is the direct child of `root` whose subtree leads down to
    the target node. We collect h-tags from all of `root`'s descendants that
    come before that child in DOM order (or inside earlier children's subtrees).
    Pass `branch_child=None` to collect from the entire subtree (when `root`
    IS the target node and we want headings nested directly inside it).

    Selectolax's Node.iter() is non-recursive, so we walk in DOM order with
    a stack seeded by reverse-order children.
    """
    found: list[str] = []
    # Collect direct children in DOM order; recurse into each until we hit
    # branch_child (then stop, because everything from there on is "after us"
    # or "below us along our own branch").
    for child in root.iter():
        if child == branch_child:
            break
        # DFS this child's subtree in DOM order.
        stack: list[Node] = [child]
        while stack:
            cur = stack.pop(0)  # FIFO for DOM order; small subtrees, OK
            if cur.tag in _HEADING_TAGS:
                txt = _text_of(cur)
                if txt:
                    found.append(txt)
            for c in cur.iter():
                stack.append(c)
    return found


def _collect_heading_path(node: Node, page_title: str = "") -> str:
    """Walk up DOM ancestors, collecting heading texts from each ancestor's
    subtree that come *before* the branch leading to `node`.

    Was: only inspected the *direct children* of each ancestor for h-tags, so
    wrapper structures like <div class="page-header"><h1>...</h1></div> were
    missed — leading to ~70% empty heading_context on PolyU pages.

    Now: at each ancestor (and at `node` itself), DFS-scans the subtree
    portion that appears *before* the branch we came up through, collecting
    h-tags in DOM order. This catches wrapper-h1, nested-section-h2, and
    `<div><h2>...</h2><p>content</p></div>` patterns alike.

    Falls back to `page_title` when no heading is found anywhere — better
    than empty string for the LLM extraction prompt.
    """
    # Collect from outermost ancestor down to `node` itself.
    # `prev_branch` is the child of `current` whose subtree contains `node`.
    chain: list[tuple[Node, Node | None]] = []
    current: Node | None = node
    prev_branch: Node | None = None
    while current is not None:
        chain.append((current, prev_branch))
        prev_branch = current
        current = current.parent
    # chain is [(node, None), (parent, node), (gparent, parent), ...]
    # We want outermost-first: reverse it.
    chain.reverse()

    headings: list[str] = []
    for ancestor, branch_child in chain:
        if ancestor.tag in _HEADING_TAGS:
            txt = _text_of(ancestor)
            if txt:
                headings.append(txt)
        headings.extend(_headings_before_branch(ancestor, branch_child))

    # Dedup while preserving order (some pages repeat heading text across
    # wrapper levels).
    seen: list[str] = []
    for h in headings:
        if h not in seen:
            seen.append(h)
    if seen:
        return " > ".join(seen)
    return page_title


def _make_block_id(url: str, dom_path: str) -> str:
    return hashlib.md5(f"{url}|{dom_path}".encode()).hexdigest()


def _should_not_split(node: Node) -> bool:
    return node.tag in _NO_SPLIT_TAGS


def _build_blocks_recursive(
    node: Node,
    url: str,
    depth: int,
    max_words: int,
    min_words: int,
    parent_id: str | None,
    child_idx: int,
    dom_path: str,
    page_title: str = "",
) -> list[Block]:
    """Bottom-up merge: if all descendant text fits in max_words, produce one block.

    `dom_path` is the canonical path for `node`, computed top-down by the caller.
    `page_title` is used as heading_context fallback when no h-tag is found
    anywhere up the ancestor chain.
    """
    text = _text_of(node)
    wc = _word_count(text)

    if not text:
        return []

    bid = _make_block_id(url, dom_path)

    if _should_not_split(node) or wc <= max_words:
        if wc < min_words:
            return []
        heading = _collect_heading_path(node, page_title=page_title)
        return [Block(
            block_id=bid,
            content=text,
            html_tag_path=dom_path,
            heading_context=heading,
            token_count=wc,
            depth=depth,
            parent_block_id=parent_id,
            child_index=child_idx,
            url=url,
        )]

    child_blocks: list[Block] = []
    idx = 0
    for child, seg in _children_with_segments(node):
        child_path = f"{dom_path}>{seg}" if dom_path else seg
        sub = _build_blocks_recursive(
            child, url, depth + 1, max_words, min_words, bid, idx, child_path,
            page_title=page_title,
        )
        child_blocks.extend(sub)
        idx += 1

    if not child_blocks:
        heading = _collect_heading_path(node, page_title=page_title)
        return [Block(
            block_id=bid,
            content=text,
            html_tag_path=dom_path,
            heading_context=heading,
            token_count=wc,
            depth=depth,
            parent_block_id=parent_id,
            child_index=child_idx,
            url=url,
        )]

    parent_block = Block(
        block_id=bid,
        content="",
        html_tag_path=dom_path,
        heading_context=_collect_heading_path(node, page_title=page_title),
        token_count=0,
        depth=depth,
        parent_block_id=parent_id,
        child_index=child_idx,
        children=child_blocks,
        url=url,
    )
    return [parent_block]


def _entry_path(node: Node) -> str:
    """Compute the canonical path for the entry node by walking up its ancestors.

    Ancestors at the entry are typically a single chain (one body, one html),
    so the simple bottom-up walk here is safe — the multi-sibling problem
    that motivated the top-down rewrite only happens *below* the entry node,
    where _children_with_segments takes over.
    """
    parts: list[str] = []
    current: Node | None = node
    while current is not None:
        tag = current.tag
        if tag in ("-text", "-comment", "-undef"):
            current = current.parent
            continue
        node_id = current.attributes.get("id", "") if current.attributes else ""
        # Entry-chain ancestors are single-child by construction (html > body > ...)
        # so [0] is correct unless the ancestor has an id.
        ident = f"#{node_id}" if node_id else "[0]"
        parts.append(f"{tag}{ident}")
        current = current.parent
    parts.reverse()
    return ">".join(parts)


def build_block_tree(
    cleaned_html: str,
    url: str,
    max_words: int = DEFAULT_MAX_WORDS,
    min_words: int = DEFAULT_MIN_WORDS,
    page_title: str = "",
) -> list[Block]:
    """Parse cleaned HTML and return a flat list of leaf Blocks with hierarchy metadata.

    `page_title` is used as heading_context fallback for blocks whose ancestor
    chain has no h-tag (PolyU pages often render the page heading in a wrapper
    structure that the DOM walk can miss; passing the WebPage title keeps the
    extraction prompt from seeing an empty heading).
    """
    tree = HTMLParser(cleaned_html)
    body = tree.css_first("body")
    if body is None:
        body = tree.root
    if body is None:
        return []

    entry_path = _entry_path(body)
    raw_tree = _build_blocks_recursive(
        body, url, 0, max_words, min_words, None, 0, entry_path,
        page_title=page_title,
    )

    leaves: list[Block] = []

    def _collect_leaves(blocks: list[Block]) -> None:
        for b in blocks:
            if b.children:
                for child in b.children:
                    child.parent_block_id = b.block_id
                _collect_leaves(b.children)
            else:
                leaves.append(b)

    _collect_leaves(raw_tree)
    return leaves


_BOILERPLATE_TAGS = {"header", "footer", "nav"}

_BOILERPLATE_PATTERNS = [
    re.compile(r"(?i)we use cookies?\b.*\bprivacy\b"),
    re.compile(r"(?i)your browser is not the latest version"),
    re.compile(r"(?i)copyright\s*©"),
    re.compile(r"(?i)privacy policy\s*(statement)?.*terms of use"),
]

_LINK_LIST_RATIO_THRESHOLD = 0.6


def _is_boilerplate(block: Block) -> bool:
    """Return True if the block is navigation, footer chrome, or cookie banners."""
    path_lower = block.html_tag_path.lower()
    for tag in _BOILERPLATE_TAGS:
        if f">{tag}" in path_lower or f">{tag}#" in path_lower:
            return True

    for pat in _BOILERPLATE_PATTERNS:
        if pat.search(block.content):
            return True

    tokens = block.content.split()
    if not tokens:
        return True

    menu_signals = sum(
        1 for t in tokens
        if t.lower() in ("open", "close", "menu", "back", "/")
    )
    if menu_signals > 4 and menu_signals / len(tokens) > 0.15:
        return True

    return False


def filter_blocks(blocks: list[Block]) -> list[Block]:
    """Remove navigation, footer, cookie-banner and other boilerplate blocks."""
    return [b for b in blocks if not _is_boilerplate(b)]


def blocks_to_dicts(blocks: list[Block]) -> list[dict[str, Any]]:
    """Convert Block dataclass list to serializable dicts."""
    return [
        {
            "block_id": b.block_id,
            "content": b.content,
            "html_tag_path": b.html_tag_path,
            "heading_context": b.heading_context,
            "token_count": b.token_count,
            "depth": b.depth,
            "parent_block_id": b.parent_block_id,
            "child_index": b.child_index,
            "url": b.url,
        }
        for b in blocks
    ]
