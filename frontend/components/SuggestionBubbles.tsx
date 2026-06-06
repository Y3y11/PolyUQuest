"use client";

import { motion } from "framer-motion";
import { Sparkles, ArrowUpRight } from "lucide-react";

interface Props {
  items: string[];
  onPick: (q: string) => void;
  disabled?: boolean;
}

export default function SuggestionBubbles({ items, onPick, disabled }: Props) {
  if (!items || items.length === 0) return null;
  // Note: deliberately no AnimatePresence here — the parent already gates
  // mounting via `messages.length > 0 && !isLoading && suggestions.length > 0`,
  // and AnimatePresence around a single static-keyed child was causing
  // reconciliation failures (insertBefore) under React 18 StrictMode + Next dev.
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
      className="flex flex-wrap items-center gap-2"
      aria-label="Follow-up suggestions"
    >
      <span className="inline-flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-[0.16em] text-text-muted shrink-0">
        <Sparkles size={11} className="text-accent" />
        Suggested next
      </span>
      {items.map((q, i) => (
        <button
          key={`${i}-${q.slice(0, 12)}`}
          disabled={disabled}
          onClick={() => onPick(q)}
          className="group inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-border bg-surface hover:border-primary/45 hover:bg-primary/[0.06] text-[12px] text-text-main disabled:opacity-50 disabled:hover:bg-surface transition-colors max-w-full"
          title={q}
        >
          <span className="truncate max-w-[280px]">{q}</span>
          <ArrowUpRight
            size={11}
            className="text-text-muted opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
          />
        </button>
      ))}
    </motion.div>
  );
}
