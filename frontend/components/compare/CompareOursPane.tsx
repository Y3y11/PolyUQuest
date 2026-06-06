"use client";

import { useState } from "react";
import {
  ComparePaneHeader,
  CompareMiniTheatre,
  CompareAnswerBlock,
} from "./PaneShell";
import type { BlockRef, PipelineStep } from "@/lib/api";

export interface OursPaneState {
  isLoading: boolean;
  answer: string;
  steps: PipelineStep[];
  blocks: BlockRef[];
  mode: string | null;
  elapsed: number | null;
}

const MODE_LABEL: Record<string, string> = {
  mode_a: "Mode A · Direct lookup",
  mode_b: "Mode B · Cross-page navigation",
  mode_c: "Mode C · Multi-hop reasoning",
};

export default function CompareOursPane({ state }: { state: OursPaneState }) {
  const [tab, setTab] = useState<"pipeline" | "blocks">("pipeline");
  const modeLabel = state.mode
    ? MODE_LABEL[state.mode] || state.mode
    : "Routed by intent → vector + graph traversal → reranked → cited";

  return (
    <div className="flex flex-col h-full min-h-0 bg-surface">
      <ComparePaneHeader
        system="Graph-RAG (Ours)"
        modeLabel={modeLabel}
        badgeTone="ours"
        isLoading={state.isLoading}
        elapsed={state.elapsed}
        blockCount={state.blocks.length}
      />
      <div className="flex-1 min-h-0 grid grid-rows-[1fr_minmax(0,1fr)] divide-y divide-border">
        <div className="overflow-y-auto py-3">
          <CompareAnswerBlock answer={state.answer} isLoading={state.isLoading} />
        </div>
        <CompareMiniTheatre
          steps={state.steps}
          blocks={state.blocks}
          isLoading={state.isLoading}
          tab={tab}
          onTabChange={setTab}
        />
      </div>
    </div>
  );
}
