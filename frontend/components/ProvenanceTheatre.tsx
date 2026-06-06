"use client";

import { motion, AnimatePresence } from "framer-motion";
import { Route, FileText, Quote } from "lucide-react";
import PipelineStage from "@/components/stage/PipelineStage";
import BlocksStage from "@/components/stage/BlocksStage";
import PageStage from "@/components/stage/PageStage";
import type { BlockRef, PipelineStep } from "@/lib/api";

export type TheatreTab = "pipeline" | "page" | "blocks";

const TABS: { id: TheatreTab; label: string; icon: typeof Route }[] = [
  { id: "pipeline", label: "Pipeline", icon: Route },
  { id: "page", label: "Page", icon: FileText },
  { id: "blocks", label: "Blocks", icon: Quote },
];

export default function ProvenanceTheatre({
  steps,
  blocks,
  highlightedIndex,
  selectedIndex,
  isLoading,
  activeTab,
  onTabChange,
  onCitationClick,
  onCitationHover,
  compact = false,
}: {
  steps: PipelineStep[];
  blocks: BlockRef[];
  highlightedIndex: number | null;
  selectedIndex: number | null;
  isLoading: boolean;
  activeTab: TheatreTab;
  onTabChange: (tab: TheatreTab) => void;
  onCitationClick?: (idx: number) => void;
  onCitationHover?: (idx: number | null) => void;
  compact?: boolean;
}) {
  return (
    <div className="flex flex-col h-full min-h-0">
      <TheatreTabs
        active={activeTab}
        onChange={onTabChange}
        counts={{ pipeline: steps.length, page: blocks.length, blocks: blocks.length }}
        compact={compact}
      />
      <div className="flex-1 min-h-0 overflow-y-auto">
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={activeTab}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
            className={compact ? "p-3" : "p-5"}
          >
            {activeTab === "pipeline" && (
              <PipelineStage steps={steps} isLoading={isLoading && steps.length === 0} />
            )}
            {activeTab === "page" && (
              <PageStage
                blocks={blocks}
                highlightedIndex={highlightedIndex}
                selectedIndex={selectedIndex}
                isLoading={isLoading && blocks.length === 0}
                onCitationClick={onCitationClick}
                onCitationHover={onCitationHover}
              />
            )}
            {activeTab === "blocks" && (
              <BlocksStage blocks={blocks} highlightedIndex={highlightedIndex} />
            )}
          </motion.div>
        </AnimatePresence>
      </div>
    </div>
  );
}

function TheatreTabs({
  active,
  onChange,
  counts,
  compact,
}: {
  active: TheatreTab;
  onChange: (tab: TheatreTab) => void;
  counts: Record<TheatreTab, number>;
  compact: boolean;
}) {
  return (
    <div
      role="tablist"
      aria-label="Provenance stages"
      className="flex-none flex items-stretch border-b border-border bg-surface-alt/50"
    >
      {TABS.map((tab) => {
        const Icon = tab.icon;
        const isActive = active === tab.id;
        const count = counts[tab.id];
        return (
          <button
            key={tab.id}
            role="tab"
            aria-selected={isActive}
            onClick={() => onChange(tab.id)}
            className={`relative flex-1 flex items-center justify-center gap-1.5 ${
              compact ? "py-2 text-[11px]" : "py-2.5 text-xs"
            } font-mono uppercase tracking-[0.14em] transition-colors ${
              isActive
                ? "text-primary"
                : "text-text-muted hover:text-text-main"
            }`}
          >
            <Icon size={compact ? 11 : 12} />
            <span>{tab.label}</span>
            {count > 0 && (
              <span
                className={`text-[9px] tabular-nums px-1 rounded ${
                  isActive
                    ? "bg-primary/15 text-primary"
                    : "bg-surface-sunk text-text-muted"
                }`}
              >
                {count}
              </span>
            )}
            {isActive && (
              <motion.span
                layoutId="theatre-tab-underline"
                className="absolute left-2 right-2 -bottom-px h-[2px] bg-primary"
                transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
              />
            )}
          </button>
        );
      })}
    </div>
  );
}
