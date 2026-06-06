"use client";

import { motion } from "framer-motion";
import { Clock, Zap, Loader2, AlertCircle } from "lucide-react";
import ChatPanel from "@/components/ChatPanel";
import PipelineStage from "@/components/stage/PipelineStage";
import BlocksStage from "@/components/stage/BlocksStage";
import type { BlockRef, PipelineStep } from "@/lib/api";

type PaneTab = "pipeline" | "blocks";

interface PaneHeaderProps {
  system: string;
  modeLabel: string;
  badgeTone: "neutral" | "ours" | "accent";
  isLoading: boolean;
  elapsed: number | null;
  blockCount: number;
  status?: string;
}

export function ComparePaneHeader({
  system,
  modeLabel,
  badgeTone,
  isLoading,
  elapsed,
  blockCount,
  status,
}: PaneHeaderProps) {
  const badgeClass =
    badgeTone === "ours"
      ? "bg-primary text-text-inverse"
      : badgeTone === "accent"
      ? "bg-accent text-text-inverse"
      : "bg-surface text-text-muted border border-border";
  const headerBg =
    badgeTone === "ours" ? "bg-primary/[0.05]" : "bg-surface-sunk/40";

  return (
    <div className={`flex-none px-4 py-3 border-b border-border ${headerBg}`}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <span
            className={`text-[10px] font-mono uppercase tracking-[0.16em] px-1.5 py-0.5 rounded ${badgeClass}`}
          >
            {badgeTone === "ours" ? "Ours" : "Baseline"}
          </span>
          <span className="font-display text-[14px] font-semibold text-text-main truncate">
            {system}
          </span>
        </div>
        <div className="flex items-center gap-2 text-[10px] font-mono text-text-muted shrink-0">
          {isLoading ? (
            <span className="flex items-center gap-1.5">
              <Loader2 size={10} className="animate-spin" />
              streaming
            </span>
          ) : elapsed != null ? (
            <>
              <span className="flex items-center gap-1">
                <Clock size={10} />
                {elapsed.toFixed(1)}s
              </span>
              <span className="flex items-center gap-1">
                <Zap size={10} />
                {blockCount} blk
              </span>
            </>
          ) : status ? (
            <span className="flex items-center gap-1.5">
              <AlertCircle size={10} />
              {status}
            </span>
          ) : (
            <span>idle</span>
          )}
        </div>
      </div>
      <p className="text-[11px] text-text-muted mt-1 leading-snug">
        {modeLabel}
      </p>
    </div>
  );
}

interface MiniTheatreProps {
  steps: PipelineStep[];
  blocks: BlockRef[];
  isLoading: boolean;
  tab: PaneTab;
  onTabChange: (t: PaneTab) => void;
}

export function CompareMiniTheatre({
  steps,
  blocks,
  isLoading,
  tab,
  onTabChange,
}: MiniTheatreProps) {
  return (
    <div className="flex flex-col h-full min-h-0">
      <div
        role="tablist"
        className="flex-none flex items-stretch border-b border-border bg-surface-alt/40"
      >
        {(["pipeline", "blocks"] as PaneTab[]).map((t) => {
          const active = t === tab;
          return (
            <button
              key={t}
              role="tab"
              aria-selected={active}
              onClick={() => onTabChange(t)}
              className={`relative flex-1 py-1.5 text-[10px] font-mono uppercase tracking-[0.14em] transition-colors ${
                active
                  ? "text-primary"
                  : "text-text-muted hover:text-text-main"
              }`}
            >
              {t}
              {active && (
                <motion.span
                  layoutId={`mini-tab-${t}`}
                  className="absolute left-2 right-2 -bottom-px h-[2px] bg-primary"
                />
              )}
            </button>
          );
        })}
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-3">
        {tab === "pipeline" && (
          <PipelineStage steps={steps} isLoading={isLoading && steps.length === 0} />
        )}
        {tab === "blocks" && (
          <BlocksStage blocks={blocks} highlightedIndex={null} />
        )}
      </div>
    </div>
  );
}

interface ChatBlockProps {
  answer: string;
  isLoading: boolean;
}

export function CompareAnswerBlock({ answer, isLoading }: ChatBlockProps) {
  if (!answer && !isLoading) {
    return (
      <div className="px-4 py-6 text-[12px] text-text-muted">
        Awaiting answer…
      </div>
    );
  }
  return (
    <div className="px-3">
      <ChatPanel
        messages={
          answer
            ? [{ role: "assistant", content: answer }]
            : []
        }
        isLoading={isLoading && !answer}
      />
    </div>
  );
}
