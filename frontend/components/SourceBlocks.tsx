"use client";

import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { ExternalLink, FileText, ChevronDown, ChevronUp } from "lucide-react";
import type { BlockRef } from "@/lib/api";

function ScoreBar({ score }: { score: number }) {
  const pct = Math.min(Math.max(score * 100, 0), 100);
  return (
    <div className="flex items-center gap-1.5">
      <div className="w-16 h-1.5 rounded-full bg-surface-alt overflow-hidden">
        <motion.div
          initial={{ width: 0 }}
          animate={{ width: `${pct}%` }}
          transition={{ duration: 0.4, delay: 0.2 }}
          className="h-full rounded-full bg-accent"
        />
      </div>
      <span className="text-[10px] font-mono text-text-muted">
        {score.toFixed(3)}
      </span>
    </div>
  );
}

function BlockCard({
  block,
  index,
  isHighlighted,
  id,
}: {
  block: BlockRef;
  index: number;
  isHighlighted: boolean;
  id: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const contentTruncated = block.content.length > 200;
  const displayContent = expanded
    ? block.content
    : block.content.slice(0, 200) + (contentTruncated ? "..." : "");

  return (
    <motion.div
      id={id}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.06 }}
      className={`rounded-lg border p-3 transition-all duration-200 ${
        isHighlighted
          ? "border-accent bg-accent/5 ring-2 ring-accent/30 shadow-sm"
          : "border-border hover:border-primary/30"
      }`}
    >
      <div className="flex items-start gap-2.5">
        <span className="flex-none inline-flex items-center justify-center w-5 h-5 rounded text-[10px] font-bold font-mono bg-accent text-text-inverse">
          {index + 1}
        </span>
        <div className="flex-1 min-w-0">
          {block.heading_context && (
            <div className="text-[10px] text-text-muted font-mono mb-1.5 flex items-center gap-1 truncate">
              <FileText size={10} className="shrink-0" />
              <span className="truncate">{block.heading_context}</span>
            </div>
          )}

          <p className="text-xs leading-relaxed text-text-main">
            {displayContent}
          </p>

          {contentTruncated && (
            <button
              onClick={() => setExpanded(!expanded)}
              className="flex items-center gap-0.5 text-[10px] text-primary hover:text-accent mt-1 transition-colors"
            >
              {expanded ? (
                <>
                  <ChevronUp size={10} /> Show less
                </>
              ) : (
                <>
                  <ChevronDown size={10} /> Show more
                </>
              )}
            </button>
          )}

          <div className="flex items-center justify-between mt-2 gap-2">
            {block.source_url && (
              <a
                href={block.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-[10px] text-primary hover:text-accent transition-colors truncate max-w-[70%]"
              >
                <ExternalLink size={10} className="shrink-0" />
                <span className="truncate">
                  {block.source_title || block.source_url}
                </span>
              </a>
            )}
            <ScoreBar score={block.score} />
          </div>
        </div>
      </div>
    </motion.div>
  );
}

export default function SourceBlocks({
  blocks,
  highlightedIndex,
}: {
  blocks: BlockRef[];
  highlightedIndex: number | null;
}) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (highlightedIndex === null || !containerRef.current) return;
    const el = containerRef.current.querySelector(
      `#source-block-${highlightedIndex}`
    );
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [highlightedIndex]);

  if (blocks.length === 0) return null;

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-[10px] font-mono text-text-muted tracking-wider uppercase">
          Source References
        </h3>
        <span className="text-[10px] font-mono text-text-muted">
          {blocks.length} blocks
        </span>
      </div>
      <div ref={containerRef} className="space-y-2.5">
        {blocks.map((block, i) => (
          <BlockCard
            key={block.block_id}
            block={block}
            index={i}
            isHighlighted={highlightedIndex === i + 1}
            id={`source-block-${i + 1}`}
          />
        ))}
      </div>
    </div>
  );
}
