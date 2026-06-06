"use client";

import { motion, AnimatePresence } from "framer-motion";
import { X, ExternalLink, FileText } from "lucide-react";
import type { BlockRef } from "@/lib/api";

export default function CitationDrawer({
  blocks,
  activeIndex,
  onClose,
}: {
  blocks: BlockRef[];
  activeIndex: number | null;
  onClose: () => void;
}) {
  return (
    <AnimatePresence>
      {activeIndex !== null && blocks.length > 0 && (
        <motion.div
          initial={{ opacity: 0, x: 20 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: 20 }}
          className="fixed right-0 top-0 h-full w-full sm:w-[420px] bg-surface border-l border-border shadow-2xl z-50 overflow-y-auto"
        >
          <div className="sticky top-0 bg-surface border-b border-border p-4 flex items-center justify-between">
            <h3 className="font-display text-lg font-semibold text-primary">
              Source References
            </h3>
            <button
              onClick={onClose}
              className="p-1 rounded hover:bg-surface-alt transition-colors"
            >
              <X size={18} />
            </button>
          </div>

          <div className="p-4 space-y-4">
            {blocks.map((block, i) => (
              <motion.div
                key={block.block_id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.05 }}
                className={`rounded-lg border p-4 transition-all ${
                  i + 1 === activeIndex
                    ? "border-accent bg-accent/5 ring-1 ring-accent/30"
                    : "border-border hover:border-primary/30"
                }`}
              >
                <div className="flex items-start gap-3">
                  <span className="flex-none inline-flex items-center justify-center w-6 h-6 rounded text-xs font-bold font-mono bg-accent text-text-inverse">
                    {i + 1}
                  </span>
                  <div className="flex-1 min-w-0">
                    {block.heading_context && (
                      <div className="text-xs text-text-muted font-mono mb-2 flex items-center gap-1">
                        <FileText size={12} />
                        {block.heading_context}
                      </div>
                    )}
                    <p className="text-sm leading-relaxed text-text-main">
                      {block.content}
                    </p>
                    {block.source_url && (
                      <a
                        href={block.source_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-xs text-primary hover:text-accent mt-3 transition-colors"
                      >
                        <ExternalLink size={12} />
                        {block.source_title || block.source_url}
                      </a>
                    )}
                    <div className="text-[10px] text-text-muted mt-1 font-mono">
                      Score: {block.score.toFixed(3)}
                    </div>
                  </div>
                </div>
              </motion.div>
            ))}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
