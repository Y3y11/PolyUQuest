"use client";

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Route,
  Search,
  GitBranch,
  Cpu,
  BarChart3,
  ChevronDown,
  ChevronRight,
  SplitSquareHorizontal,
  Sparkles,
  Filter,
  Shuffle,
  Layers,
} from "lucide-react";
import type { PipelineStep } from "@/lib/api";

type StepTheme = {
  icon: typeof Route;
  ring: string;        // background + border for the node circle
  iconColor: string;
};

// Single source of truth — palette aligned with paper-library tokens.
const STEP_THEME: Record<string, StepTheme> = {
  routing:           { icon: Route,                ring: "bg-amber-500/15  border-amber-500/35",  iconColor: "text-amber-500"   },
  query_decompose:   { icon: SplitSquareHorizontal,ring: "bg-violet-500/15 border-violet-500/35", iconColor: "text-violet-500"  },
  keyword_extraction:{ icon: Sparkles,             ring: "bg-violet-500/15 border-violet-500/35", iconColor: "text-violet-500"  },
  webpage_search:    { icon: Search,               ring: "bg-blue-500/15   border-blue-500/35",   iconColor: "text-blue-500"    },
  block_search:      { icon: Search,               ring: "bg-blue-500/15   border-blue-500/35",   iconColor: "text-blue-500"    },
  entity_search:     { icon: Search,               ring: "bg-blue-500/15   border-blue-500/35",   iconColor: "text-blue-500"    },
  block_retrieval:   { icon: Search,               ring: "bg-blue-500/15   border-blue-500/35",   iconColor: "text-blue-500"    },
  graph_traversal:   { icon: GitBranch,            ring: "bg-emerald-500/15 border-emerald-500/35",iconColor: "text-emerald-500"},
  context_enrichment:{ icon: Layers,               ring: "bg-emerald-500/15 border-emerald-500/35",iconColor: "text-emerald-500"},
  query_rewrite:     { icon: Shuffle,              ring: "bg-teal-500/15   border-teal-500/35",   iconColor: "text-teal-500"    },
  bm25_union:        { icon: Filter,               ring: "bg-teal-500/15   border-teal-500/35",   iconColor: "text-teal-500"    },
  rerank:            { icon: BarChart3,            ring: "bg-teal-500/15   border-teal-500/35",   iconColor: "text-teal-500"    },
  block_scoring:     { icon: BarChart3,            ring: "bg-teal-500/15   border-teal-500/35",   iconColor: "text-teal-500"    },
  hybrid_parallel:   { icon: SplitSquareHorizontal,ring: "bg-rose-500/15   border-rose-500/35",   iconColor: "text-rose-500"    },
  hybrid_merge:      { icon: Layers,               ring: "bg-rose-500/15   border-rose-500/35",   iconColor: "text-rose-500"    },
  answer_generation: { icon: Cpu,                  ring: "bg-violet-500/15 border-violet-500/35", iconColor: "text-violet-500"  },
};

const DEFAULT_THEME: StepTheme = {
  icon: Cpu,
  ring: "bg-surface-alt border-border",
  iconColor: "text-text-muted",
};

// --- Summary line ----------------------------------------------------------
// Render one human-readable line that captures the most-relevant numbers
// for this step. The full data blob is still available via expand.

function fmtNum(n: unknown): string {
  if (typeof n !== "number" || !Number.isFinite(n)) return String(n ?? "—");
  if (Number.isInteger(n)) return n.toLocaleString();
  return n.toFixed(3);
}

function summariseStep(step: PipelineStep): string {
  const d = step.data || {};
  switch (step.step) {
    case "routing": {
      const mode = d.mode ? String(d.mode) : "—";
      const conf = typeof d.confidence === "number" ? `${(d.confidence * 100).toFixed(0)}% conf` : null;
      const src = d.source ? `via ${d.source}` : null;
      return [mode, conf, src].filter(Boolean).join(" · ");
    }
    case "query_decompose": {
      const subs = Array.isArray(d.sub_queries) ? d.sub_queries.length : 0;
      return `${subs} sub-quer${subs === 1 ? "y" : "ies"}`;
    }
    case "keyword_extraction": {
      const kws = Array.isArray(d.keywords) ? d.keywords.length : 0;
      const ents = typeof d.topic_entities_found === "number" ? d.topic_entities_found : null;
      return [
        `${kws} keyword${kws === 1 ? "" : "s"}`,
        ents !== null ? `${ents} topic entities` : null,
      ].filter(Boolean).join(" · ");
    }
    case "webpage_search": {
      const found = d.pages_found ?? "—";
      const exp = d.pages_after_expansion;
      return exp != null && exp !== found
        ? `${found} pages → ${exp} after LINKS_TO`
        : `${found} pages`;
    }
    case "block_search":
    case "block_retrieval": {
      const hits = d.hits_count ?? d.total_candidates ?? d.first_stage_kept;
      const k = d.first_stage_top_k ?? d.first_stage_pool;
      const bm = d.bm25_added;
      return [
        hits != null ? `${hits} hits` : null,
        k != null ? `top-${k}` : null,
        bm ? `+${bm} BM25` : null,
      ].filter(Boolean).join(" · ");
    }
    case "entity_search": {
      const hits = d.hits ?? "—";
      return `${hits} entit${hits === 1 ? "y" : "ies"} matched`;
    }
    case "graph_traversal": {
      const init = d.initial_entities ?? "—";
      const exp = d.expanded_entities ?? "—";
      const hops = d.hops ?? "?";
      return `${init} → ${exp} entities · ${hops}-hop`;
    }
    case "context_enrichment": {
      const n = d.blocks_enriched ?? "—";
      return `${n} block${n === 1 ? "" : "s"} enriched`;
    }
    case "query_rewrite": {
      const rw = Array.isArray(d.rewrites) ? d.rewrites.length : 0;
      const added = d.added ?? 0;
      return `${rw} rewrite${rw === 1 ? "" : "s"} · +${added} new`;
    }
    case "bm25_union": {
      const added = d.added ?? 0;
      const k = d.bm25_top_k ?? "?";
      return `top-${k} sparse · +${added} new`;
    }
    case "rerank": {
      const cand = d.candidates ?? "—";
      const kept = d.kept ?? "—";
      const top = typeof d.top_score === "number" ? fmtNum(d.top_score) : null;
      return [
        `${cand} → ${kept}`,
        top ? `top ${top}` : null,
      ].filter(Boolean).join(" · ");
    }
    case "block_scoring": {
      const cand = d.candidates ?? "—";
      const sel = d.selected ?? "—";
      return `${cand} candidates · ${sel} selected`;
    }
    case "hybrid_parallel": {
      const modes = Array.isArray(d.modes) ? d.modes.join(" + ") : "—";
      return modes;
    }
    case "hybrid_merge": {
      const merged = d.merged_blocks ?? "—";
      const overlap = d.overlap_count ?? 0;
      return `${merged} merged · ${overlap} overlap`;
    }
    case "answer_generation": {
      const tok = d.prompt_tokens_est;
      const skip = d.skipped;
      if (skip) return "skipped";
      return tok != null ? `~${tok} prompt tokens` : "generating…";
    }
    default: {
      // Fallback: show first numeric-looking field, otherwise blank.
      const entry = Object.entries(d).find(
        ([, v]) => typeof v === "number" || typeof v === "string"
      );
      return entry ? `${entry[0].replace(/_/g, " ")}: ${fmtNum(entry[1])}` : "";
    }
  }
}

// --- Expanded detail -------------------------------------------------------

function renderValue(value: unknown): string {
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    if (typeof value[0] === "object") {
      return value
        .map((v: Record<string, unknown>) => {
          if (v.name && v.score) return `${v.name} (${v.score})`;
          if (v.query) return String(v.query);
          return JSON.stringify(v);
        })
        .join(", ");
    }
    return value.join(", ");
  }
  if (typeof value === "object" && value !== null) {
    return JSON.stringify(value);
  }
  return String(value);
}

function StepDetail({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data).filter(
    ([, v]) => v !== null && v !== undefined && v !== ""
  );
  if (entries.length === 0) return null;

  return (
    <div className="mt-2.5 rounded-md border border-border bg-surface-alt/60 px-3 py-2.5 space-y-1.5">
      {entries.map(([key, value]) => (
        <div key={key} className="grid grid-cols-[110px_1fr] gap-2 items-baseline">
          <span className="text-[10.5px] font-mono uppercase tracking-[0.06em] text-text-muted">
            {key.replace(/_/g, " ")}
          </span>
          <span className="text-[12.5px] text-text-main break-all leading-snug">
            {renderValue(value)}
          </span>
        </div>
      ))}
    </div>
  );
}

// --- Step row --------------------------------------------------------------

function TraceStep({
  step,
  index,
  isLast,
  isActive,
}: {
  step: PipelineStep;
  index: number;
  isLast: boolean;
  isActive: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const theme = STEP_THEME[step.step] || DEFAULT_THEME;
  const Icon = theme.icon;
  const hasData = Object.keys(step.data || {}).length > 0;
  const summary = summariseStep(step);

  // Format duration: <1000ms shows "412 ms", >=1000ms shows "2.84 s"
  const durStr =
    step.duration_ms >= 1000
      ? `${(step.duration_ms / 1000).toFixed(2)} s`
      : `${step.duration_ms} ms`;

  return (
    <motion.div
      initial={{ opacity: 0, x: -8 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{
        delay: Math.min(index * 0.04, 0.2),
        duration: 0.25,
        ease: [0.2, 0.8, 0.2, 1],
      }}
      className="relative flex gap-3.5"
    >
      {/* Vertical connector line */}
      {!isLast && (
        <div className="absolute left-[17px] top-[36px] bottom-0 w-[1.5px] bg-border" />
      )}

      {/* Step node */}
      <div className="relative z-10 flex-none pt-0.5">
        <div
          className={`w-9 h-9 rounded-full border flex items-center justify-center ${theme.ring} ${
            isActive ? "page-block-pulse" : ""
          }`}
        >
          <Icon size={16} className={theme.iconColor} />
        </div>
      </div>

      {/* Content column */}
      <button
        onClick={() => hasData && setExpanded(!expanded)}
        className={`flex-1 min-w-0 pb-5 text-left ${hasData ? "cursor-pointer" : "cursor-default"}`}
      >
        {/* Row 1 — small uppercase step name */}
        <div className="text-[10.5px] font-mono uppercase tracking-[0.14em] text-text-muted leading-tight">
          {step.label}
        </div>

        {/* Row 2 — primary summary (the biggest, most readable line) */}
        {summary && (
          <div className="mt-1 text-[14px] leading-snug text-text-main font-medium">
            {summary}
          </div>
        )}

        {/* Row 3 — duration + expand affordance */}
        <div className="mt-1 flex items-center gap-2 text-[11px] font-mono text-text-muted tabular-nums">
          <span>{durStr}</span>
          {hasData && (
            <span className="inline-flex items-center gap-0.5 text-text-muted hover:text-primary transition-colors">
              {expanded ? (
                <>
                  <ChevronDown size={12} /> hide
                </>
              ) : (
                <>
                  <ChevronRight size={12} /> details
                </>
              )}
            </span>
          )}
        </div>

        {/* Expanded detail */}
        <AnimatePresence initial={false}>
          {expanded && hasData && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.18 }}
              className="overflow-hidden"
            >
              <StepDetail data={step.data} />
            </motion.div>
          )}
        </AnimatePresence>
      </button>
    </motion.div>
  );
}

// --- Container -------------------------------------------------------------

export default function RetrievalTrace({
  steps,
  isLoading,
}: {
  steps: PipelineStep[];
  isLoading: boolean;
}) {
  if (isLoading && steps.length === 0) {
    return (
      <div className="space-y-4 p-1">
        {[0, 1, 2].map((i) => (
          <div key={i} className="flex gap-3.5 animate-pulse">
            <div className="w-9 h-9 rounded-full bg-surface-alt" />
            <div className="flex-1 space-y-2 py-1.5">
              <div className="h-2.5 bg-surface-alt rounded w-1/3" />
              <div className="h-3.5 bg-surface-alt rounded w-3/4" />
              <div className="h-2 bg-surface-alt rounded w-1/4" />
            </div>
          </div>
        ))}
        <p className="text-xs text-text-muted text-center mt-3">
          Searching the knowledge graph…
        </p>
      </div>
    );
  }

  if (steps.length === 0) return null;

  const totalMs = steps.reduce((sum, s) => sum + s.duration_ms, 0);
  const totalStr =
    totalMs >= 1000 ? `${(totalMs / 1000).toFixed(2)} s` : `${totalMs} ms`;

  // While the stream is still open, the most recently appended step is the
  // one currently executing — pulse its node. Once `isLoading` goes false
  // we stop the animation so it doesn't blink forever after the answer.
  const activeIdx = isLoading ? steps.length - 1 : -1;

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-[11px] font-mono text-text-muted tracking-[0.18em] uppercase">
          Pipeline
        </h3>
        <span className="text-xs font-mono text-text-muted tabular-nums">
          {totalStr} total
        </span>
      </div>
      <div>
        {steps.map((step, i) => (
          <TraceStep
            key={`${step.step}-${i}`}
            step={step}
            index={i}
            isLast={i === steps.length - 1}
            isActive={i === activeIdx}
          />
        ))}
      </div>
    </div>
  );
}
