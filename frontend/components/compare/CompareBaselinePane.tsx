"use client";

import { useState } from "react";
import { FileQuestion } from "lucide-react";
import {
  ComparePaneHeader,
  CompareMiniTheatre,
  CompareAnswerBlock,
} from "./PaneShell";
import { useReplayBaseline } from "@/lib/useReplayBaseline";
import type { BaselineRecord } from "@/lib/baselineLoader";

interface CompareBaselinePaneProps {
  systemName: string;
  modeLabel: string;
  badgeTone: "neutral" | "accent";
  record: BaselineRecord | null;
  loadError: string | null;
  playKey: number;
}

export default function CompareBaselinePane({
  systemName,
  modeLabel,
  badgeTone,
  record,
  loadError,
  playKey,
}: CompareBaselinePaneProps) {
  const [tab, setTab] = useState<"pipeline" | "blocks">("pipeline");
  const replay = useReplayBaseline(record, playKey);

  const visibleSteps = record
    ? record.pipeline_trace.slice(0, replay.traceShown)
    : [];
  const visibleBlocks = record
    ? record.blocks.slice(0, replay.blocksShown)
    : [];

  return (
    <div className="flex flex-col h-full min-h-0 bg-surface">
      <ComparePaneHeader
        system={systemName}
        modeLabel={modeLabel}
        badgeTone={badgeTone}
        isLoading={replay.isPlaying}
        elapsed={replay.done ? record?.elapsed_seconds ?? null : null}
        blockCount={record?.blocks.length ?? 0}
        status={loadError ?? undefined}
      />
      <div className="flex-1 min-h-0 grid grid-rows-[1fr_minmax(0,1fr)] divide-y divide-border">
        <div className="overflow-y-auto py-3">
          {loadError ? (
            <NoRecording systemName={systemName} reason={loadError} />
          ) : (
            <CompareAnswerBlock
              answer={replay.answer}
              isLoading={replay.isPlaying && !replay.answer}
            />
          )}
        </div>
        <CompareMiniTheatre
          steps={visibleSteps}
          blocks={visibleBlocks}
          isLoading={replay.isPlaying}
          tab={tab}
          onTabChange={setTab}
        />
      </div>
    </div>
  );
}

function NoRecording({
  systemName,
  reason,
}: {
  systemName: string;
  reason: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center px-5 py-8 text-text-muted">
      <FileQuestion size={22} className="opacity-40 mb-3" />
      <p className="text-[13px] text-text-main mb-1">
        No recording for {systemName}
      </p>
      <p className="text-[11px] leading-relaxed max-w-[260px]">
        {reason}. Drop a JSON into{" "}
        <code className="bg-surface-sunk px-1 rounded">
          /baselines/{systemName.toLowerCase()}/&lt;slug&gt;.json
        </code>{" "}
        to enable this pane.
      </p>
    </div>
  );
}
