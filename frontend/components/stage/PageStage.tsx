"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";
import { FileText, ExternalLink, ChevronDown, ChevronUp } from "lucide-react";
import type { BlockRef } from "@/lib/api";

// Split a breadcrumb-ish heading_context like "Programmes > MSc DSA > Admission"
// into clean segments. Be permissive: PolyU pages use `>`, `›`, `/`, sometimes ` - `.
function splitHeadingPath(raw: string): string[] {
  if (!raw) return [];
  return raw
    .split(/\s*(?:[>›»]|\s\/\s|\s-\s)\s*/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function pathOf(url: string): string {
  try {
    const u = new URL(url);
    return u.pathname + u.search;
  } catch {
    return "";
  }
}

// PolyU blocks come out of the DOM cleaner with two recurring issues:
//   1. Long runs of spaces (3+) where the original page had a layout gap
//      between a label and its value, or between table cells.
//   2. No newlines at all — the whole block is a single dense paragraph.
// We turn (1) into paragraph breaks (a 3+ space run is almost always a
// semantic boundary in PolyU's rendering) and collapse the rest to single
// spaces so the prose reads cleanly. Existing `\n` characters are kept.
function normalizeBlockText(raw: string): string[] {
  if (!raw) return [];
  return raw
    .replace(/[ \t]{3,}/g, "\n")
    .split(/\n+/)
    .map((p) => p.replace(/[ \t]+/g, " ").trim())
    .filter(Boolean);
}

interface BlockWithCite extends BlockRef {
  cite: number;
}

interface PageGroup {
  source_url: string;
  source_title: string;
  blocks: BlockWithCite[];
}

function groupByPage(blocks: BlockRef[]): PageGroup[] {
  const byUrl = new Map<string, PageGroup>();
  const order: string[] = [];
  blocks.forEach((b, i) => {
    const cite = i + 1;
    const key = b.source_url || `unknown-${i}`;
    if (!byUrl.has(key)) {
      order.push(key);
      byUrl.set(key, {
        source_url: b.source_url,
        source_title: b.source_title || hostOf(b.source_url) || "Untitled",
        blocks: [],
      });
    }
    byUrl.get(key)!.blocks.push({ ...b, cite });
  });
  return order.map((k) => byUrl.get(k)!);
}

// If the breadcrumb says the same thing as the page title (e.g. PolyU's
// "COMP - Doctor of Philosophy (PhD) | The Hong Kong Polytechnic University"
// gets split into 3 segments that recreate the title), skip it — the page
// header already shows that information.
function isRedundantHeadingPath(
  segments: string[],
  sourceTitle: string
): boolean {
  if (segments.length === 0) return true;
  // Strip both *delimiters* (- / | › > ·) and whitespace so the two strings
  // can be compared on content alone. The breadcrumb has already had the
  // delimiters split out; the title still has the original ones. Without
  // stripping them both reductions stay non-equal even when they carry the
  // same words.
  const norm = (s: string) =>
    s.toLowerCase().replace(/[\s|·›»>\-/]+/g, " ").trim();
  const joined = norm(segments.join(" "));
  const title = norm(sourceTitle);
  if (!title || !joined) return false;
  return joined === title || title.startsWith(joined) || joined.startsWith(title);
}

function HeadingPath({ segments }: { segments: string[] }) {
  if (segments.length === 0) return null;
  // PolyU's heading_context can include the page's *sidebar nav* (faculty
  // list, school list, etc.) — that produces 8+ segments that just look like
  // visual noise. Collapse anything longer than 4 segments down to the
  // trailing 4 so the breadcrumb stays scannable.
  const MAX = 4;
  const truncated = segments.length > MAX;
  const shown = truncated ? segments.slice(-MAX) : segments;
  return (
    <div className="flex items-baseline flex-wrap gap-1.5 text-[11px] font-mono text-text-muted">
      {truncated && (
        <span className="inline-flex items-baseline gap-1.5">
          <span className="text-border-strong">…</span>
          <span className="text-border-strong">›</span>
        </span>
      )}
      {shown.map((seg, i) => {
        const isLast = i === shown.length - 1;
        return (
          <span key={`${i}-${seg}`} className="inline-flex items-baseline gap-1.5">
            <span className={isLast ? "text-text-main" : ""}>{seg}</span>
            {!isLast && <span className="text-border-strong">›</span>}
          </span>
        );
      })}
    </div>
  );
}

function PageGroupHeader({ group }: { group: PageGroup }) {
  const host = hostOf(group.source_url);
  const path = pathOf(group.source_url);
  const favicon = host
    ? `https://www.google.com/s2/favicons?domain=${host}&sz=32`
    : null;

  return (
    <header className="flex items-center gap-2.5 min-w-0 pb-3 mb-3 border-b border-border">
      <div className="flex-none w-7 h-7 rounded-md bg-surface-alt border border-border flex items-center justify-center overflow-hidden">
        {favicon ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={favicon}
            alt=""
            width={16}
            height={16}
            className="opacity-90"
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).style.display = "none";
            }}
          />
        ) : (
          <FileText size={13} className="text-text-muted" />
        )}
      </div>
      <div className="flex-1 min-w-0">
        <div className="font-display text-[14px] leading-tight text-text-main truncate">
          {group.source_title}
        </div>
        <a
          href={group.source_url}
          target="_blank"
          rel="noopener noreferrer"
          className="group inline-flex items-center gap-1 text-[10px] font-mono text-text-muted hover:text-primary transition-colors truncate max-w-full"
        >
          <span className="truncate">
            <span className="text-text-main/60">{host}</span>
            <span className="text-text-muted/80">{path}</span>
          </span>
          <ExternalLink
            size={9}
            className="shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
          />
        </a>
      </div>
      <span className="flex-none text-[10px] font-mono uppercase tracking-[0.12em] text-text-muted">
        {group.blocks.length} blk
      </span>
    </header>
  );
}

function PageBlock({
  block,
  index,
  isHighlighted,
  isSelected,
  showHeadingPath,
  onClick,
  onHoverEnter,
  onHoverLeave,
}: {
  block: BlockWithCite;
  index: number;
  isHighlighted: boolean;
  isSelected: boolean;
  showHeadingPath: boolean;
  onClick?: () => void;
  onHoverEnter?: () => void;
  onHoverLeave?: () => void;
}) {
  const segments = splitHeadingPath(block.heading_context);
  const paragraphs = useMemo(
    () => normalizeBlockText(block.content),
    [block.content]
  );

  // Long PolyU blocks (e.g. a programme's full subject list, or admission
  // tables flattened into prose) routinely run 400–800px. Letting them all
  // expand makes the page panel a wall of text and hides cross-block
  // structure. Collapse anything taller than ~10 lines behind a "Show more"
  // toggle. The measure is in CSS pixels so it adapts to the user's font
  // size, not row count.
  const COLLAPSED_MAX_PX = 240;
  const bodyRef = useRef<HTMLDivElement>(null);
  const [isOverflowing, setIsOverflowing] = useState(false);
  const [expanded, setExpanded] = useState(false);

  // useLayoutEffect runs before paint so we never flash a "Show more"
  // button on a block that wasn't actually overflowing.
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    // scrollHeight reflects the *full* content height even when we've
    // capped the visible area with max-height.
    setIsOverflowing(el.scrollHeight > COLLAPSED_MAX_PX + 4);
  }, [paragraphs]);

  // When a block is *selected* (clicked, not merely hovered) and it's a
  // collapsed long block, auto-expand so the user lands on full context.
  // Gating on selection — not hover — keeps a stray mouse-over from
  // expanding blocks the user didn't intend to open.
  useEffect(() => {
    if (isSelected && isOverflowing) setExpanded(true);
  }, [isSelected, isOverflowing]);

  return (
    <motion.article
      id={`page-block-${block.cite}`}
      data-cite={block.cite}
      data-pulse={isHighlighted ? "true" : undefined}
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, delay: Math.min(index * 0.03, 0.24) }}
      onClick={onClick}
      onPointerEnter={onHoverEnter}
      onPointerLeave={onHoverLeave}
      className={`group relative pl-7 pr-1 py-3 cursor-default border-l-2 transition-colors ${
        isHighlighted
          ? "border-l-accent bg-accent/[0.04] page-block-pulse"
          : "border-l-transparent hover:border-l-primary/40 hover:bg-surface-alt/40"
      }`}
    >
      <span
        className={`absolute left-0 top-3 inline-flex items-center justify-center w-5 h-5 rounded text-[10.5px] font-bold font-mono transition-colors ${
          isHighlighted
            ? "bg-accent text-text-inverse"
            : "bg-surface-alt text-text-muted border border-border group-hover:bg-accent group-hover:text-text-inverse group-hover:border-accent"
        }`}
        aria-label={`Citation ${block.cite}`}
      >
        {block.cite}
      </span>

      {showHeadingPath && segments.length > 0 && (
        <div className="mb-2">
          <HeadingPath segments={segments} />
        </div>
      )}

      <div
        ref={bodyRef}
        style={
          isOverflowing && !expanded
            ? { maxHeight: COLLAPSED_MAX_PX, overflow: "hidden" }
            : undefined
        }
        className="space-y-2 text-[13px] leading-relaxed text-text-main relative"
      >
        {paragraphs.length > 0 ? (
          paragraphs.map((p, i) => (
            <p key={i} className="break-words">
              {p}
            </p>
          ))
        ) : (
          <p className="text-text-muted italic">(empty block)</p>
        )}
        {isOverflowing && !expanded && (
          // Fade the bottom of the clipped content into the surface colour so
          // the cut isn't an abrupt mid-sentence chop. Pointer-events-none so
          // hover on the citation/article still works.
          <div
            aria-hidden
            className="pointer-events-none absolute inset-x-0 bottom-0 h-12 bg-gradient-to-b from-transparent to-surface"
          />
        )}
      </div>

      {isOverflowing && (
        <button
          type="button"
          onClick={(e) => {
            // The article catches onClick for citation highlight — don't let
            // the toggle bubble up and double-trigger it.
            e.stopPropagation();
            setExpanded((v) => !v);
          }}
          className="mt-2 inline-flex items-center gap-1 text-[11px] font-mono text-text-muted hover:text-primary transition-colors"
          aria-expanded={expanded}
        >
          {expanded ? (
            <>
              <ChevronUp size={11} />
              Show less
            </>
          ) : (
            <>
              <ChevronDown size={11} />
              Show more
            </>
          )}
        </button>
      )}
    </motion.article>
  );
}

export default function PageStage({
  blocks,
  highlightedIndex,
  selectedIndex,
  isLoading,
  onCitationClick,
  onCitationHover,
}: {
  blocks: BlockRef[];
  highlightedIndex: number | null;
  selectedIndex: number | null;
  isLoading: boolean;
  onCitationClick?: (idx: number) => void;
  onCitationHover?: (idx: number | null) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const groups = useMemo(() => groupByPage(blocks), [blocks]);

  useEffect(() => {
    if (highlightedIndex === null || !containerRef.current) return;
    const el = containerRef.current.querySelector<HTMLElement>(
      `#page-block-${highlightedIndex}`
    );
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [highlightedIndex]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12 text-text-muted">
        <span className="text-xs font-mono">Rendering page context…</span>
      </div>
    );
  }

  if (blocks.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center text-center py-12 px-6">
        <div className="w-12 h-12 rounded-xl bg-surface-alt border border-border flex items-center justify-center mb-3">
          <FileText size={20} className="text-text-muted" />
        </div>
        <p className="text-sm font-medium text-text-main mb-1">Page context</p>
        <p className="text-xs text-text-muted max-w-[240px] leading-relaxed">
          Once a query runs, the retrieved blocks appear here in their original
          page layout — headings, sections, citations all linked.
        </p>
      </div>
    );
  }

  return (
    <div ref={containerRef} className="space-y-6">
      {groups.map((group) => (
        <section
          key={group.source_url}
          className="rounded-xl border border-border bg-surface px-3 sm:px-4 py-3 sm:py-4"
        >
          <PageGroupHeader group={group} />
          <div className="divide-y divide-border/60">
            {group.blocks.map((block, i) => {
              const segments = splitHeadingPath(block.heading_context);
              const showHeadingPath = !isRedundantHeadingPath(
                segments,
                group.source_title
              );
              return (
                <PageBlock
                  key={block.block_id || `${group.source_url}-${i}`}
                  block={block}
                  index={i}
                  isHighlighted={highlightedIndex === block.cite}
                  isSelected={selectedIndex === block.cite}
                  showHeadingPath={showHeadingPath}
                  onClick={
                    onCitationClick
                      ? () => onCitationClick(block.cite)
                      : undefined
                  }
                  onHoverEnter={
                    onCitationHover
                      ? () => onCitationHover(block.cite)
                      : undefined
                  }
                  onHoverLeave={
                    onCitationHover ? () => onCitationHover(null) : undefined
                  }
                />
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}
