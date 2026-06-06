"use client";

import { useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, GitBranch, Sparkles } from "lucide-react";
import ModeIndicator from "@/components/ModeIndicator";
import type { RoutingInfo } from "@/lib/queryStore";

interface RouterCardProps {
  mode: string;
  routing: RoutingInfo | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

// Backend currently emits the canonical letter form ("A"/"B"/"C"/"D") in the
// `routing` SSE event but `mode_a`/`mode_b`/... in the `retrieval` step's
// data field — see retrieval/{direct,navigation,reasoning,subgraph}.py vs
// retrieval/router.py. We accept both shapes here so the card never falls
// back to a generic "Custom route" label for known modes.
const MODE_PROSE_BY_LETTER: Record<string, { tagline: string; method: string }> = {
  A: {
    tagline: "Direct factual lookup",
    method: "Block-level vector ANN over Qdrant — fastest path, no graph walk.",
  },
  B: {
    tagline: "Cross-page navigation",
    method:
      "Page-level retrieval, then expand to siblings via the LINKS_TO graph.",
  },
  C: {
    tagline: "Multi-hop reasoning",
    method:
      "Entity-anchored retrieval with 1–2 hop traversal over the RELATES_TO graph.",
  },
  D: {
    tagline: "Subgraph-in-prompt",
    method:
      "Mode C plus a compact RELATES_TO subgraph rendered into the prompt for explicit multi-hop grounding.",
  },
};

function modeLetter(mode: string): string {
  if (!mode) return "";
  if (mode.startsWith("mode_")) return mode.slice("mode_".length).toUpperCase();
  // Hybrid modes look like "hybrid(a+c)" — strip to the first letter inside
  // parentheses so we can at least show *something* informative.
  return mode.toUpperCase();
}

function proseFor(mode: string): { tagline: string; method: string } | null {
  if (!mode) return null;
  if (mode.toLowerCase().startsWith("hybrid")) {
    return {
      tagline: "Hybrid route",
      method:
        "Router was uncertain — running two modes in parallel and fusing the results.",
    };
  }
  return MODE_PROSE_BY_LETTER[modeLetter(mode)] || null;
}

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(1, value));
  const percentLabel = `${Math.round(pct * 100)}% sure`;
  return (
    <div>
      <div className="flex items-baseline justify-between mb-1.5">
        <span className="text-[10px] font-mono uppercase tracking-[0.16em] text-text-muted">
          Router confidence
        </span>
        <span className="text-[11px] font-mono tabular-nums text-text-main">
          {percentLabel}
        </span>
      </div>
      <div
        className="h-1.5 rounded-full bg-surface-sunk overflow-hidden"
        role="progressbar"
        aria-valuenow={Math.round(pct * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <motion.div
          className="h-full rounded-full bg-accent"
          initial={{ width: 0 }}
          animate={{ width: `${pct * 100}%` }}
          transition={{ duration: 0.45, ease: [0.2, 0.8, 0.2, 1] }}
        />
      </div>
    </div>
  );
}

export default function RouterCard({
  mode,
  routing,
  open,
  onOpenChange,
}: RouterCardProps) {
  const cardRef = useRef<HTMLDivElement>(null);

  // Esc + outside-click dismissal
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onOpenChange(false);
    };
    const onClick = (e: MouseEvent) => {
      const card = cardRef.current;
      if (!card) return;
      const target = e.target as Node;
      if (card.contains(target)) return;
      // Allow clicks on the trigger itself to be handled by its own onClick
      const trigger = (target as HTMLElement).closest?.(
        "[data-router-card-trigger]"
      );
      if (trigger) return;
      onOpenChange(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [open, onOpenChange]);

  const prose = proseFor(mode) || {
    tagline: "Custom route",
    method: "Routing details unavailable.",
  };

  const reasoning = routing?.reasoning?.trim();
  const altMode = routing?.alt_mode;
  const confidence = routing?.confidence;

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          ref={cardRef}
          role="dialog"
          aria-label="Router decision details"
          initial={{ opacity: 0, y: -6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
          className="absolute right-0 top-[calc(100%+8px)] z-50 w-[340px] sm:w-[380px] rounded-xl border border-border bg-surface shadow-[0_18px_40px_-12px_rgb(0_0_0_/_0.2)] overflow-hidden"
        >
          {/* Header */}
          <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-border bg-surface-alt/60">
            <div className="flex items-center gap-2 min-w-0">
              <Sparkles size={13} className="text-accent shrink-0" />
              <span className="text-[10.5px] font-mono uppercase tracking-[0.16em] text-text-muted">
                Router decision
              </span>
            </div>
            <button
              onClick={() => onOpenChange(false)}
              aria-label="Close"
              className="p-1 rounded-md text-text-muted hover:text-text-main hover:bg-surface-sunk transition-colors"
            >
              <X size={13} />
            </button>
          </div>

          {/* Body */}
          <div className="px-4 py-4 space-y-4">
            {/* Mode + tagline */}
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <ModeIndicator mode={mode} />
                <p className="mt-1.5 text-[12px] text-text-muted leading-snug">
                  {prose.tagline}
                </p>
              </div>
            </div>

            {/* Confidence */}
            {typeof confidence === "number" && <ConfidenceBar value={confidence} />}

            {/* Method blurb */}
            <div className="text-[12.5px] text-text-main leading-relaxed">
              {prose.method}
            </div>

            {/* Reasoning quote */}
            {reasoning && (
              <blockquote className="relative pl-3 border-l-2 border-accent/70 text-[12.5px] leading-relaxed text-text-main font-display italic">
                <span className="absolute -left-1.5 -top-1 text-accent/80 select-none text-base leading-none">
                  &ldquo;
                </span>
                {reasoning}
              </blockquote>
            )}

            {/* Alt mode hint */}
            {altMode && altMode !== mode && (
              <div className="flex items-center gap-2 text-[11px] font-mono text-text-muted pt-1 border-t border-border">
                <GitBranch size={11} className="text-text-muted" />
                <span>Fallback:</span>
                <span className="text-text-main">{altMode}</span>
              </div>
            )}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
