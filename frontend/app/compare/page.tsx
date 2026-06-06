"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import TopNav from "@/components/TopNav";
import CompareOursPane, {
  type OursPaneState,
} from "@/components/compare/CompareOursPane";
import CompareBaselinePane from "@/components/compare/CompareBaselinePane";
import MetricsRow from "@/components/compare/MetricsRow";
import UnifiedComposer, {
  type PresetCase,
} from "@/components/compare/UnifiedComposer";
import {
  loadBaseline,
  type BaselineRecord,
} from "@/lib/baselineLoader";
import {
  queryStreamAPI,
  type BlockRef,
  type PipelineStep,
} from "@/lib/api";

const PRESETS: PresetCase[] = [
  {
    hop: "1-hop",
    label: "Single fact",
    q: "What is Prof. Li's research direction?",
    why: "Direct lookup — Mode A.",
  },
  {
    hop: "1-hop",
    label: "Programme detail",
    q: "What are the admission requirements for MSc DSA?",
    why: "Cross-page aggregation — Mode B.",
  },
  {
    hop: "2-hop",
    label: "Relational query",
    q: "Which professors in COMP do NLP research?",
    why: "Multi-hop entity reasoning — Mode C.",
  },
];

const initialOurs: OursPaneState = {
  isLoading: false,
  answer: "",
  steps: [],
  blocks: [],
  mode: null,
  elapsed: null,
};

type Baseline = "lightrag" | "htmlrag";

interface BaselinePaneData {
  record: BaselineRecord | null;
  error: string | null;
  playKey: number;
}

const emptyBaseline: BaselinePaneData = {
  record: null,
  error: null,
  playKey: 0,
};

type MobileTab = "lightrag" | "htmlrag" | "ours";

export default function ComparePage() {
  const [query, setQuery] = useState("");
  const [activeQuery, setActiveQuery] = useState<string | null>(null);
  const [ours, setOurs] = useState<OursPaneState>(initialOurs);
  const [lightrag, setLightrag] = useState<BaselinePaneData>(emptyBaseline);
  const [htmlrag, setHtmlrag] = useState<BaselinePaneData>(emptyBaseline);
  const [mobileTab, setMobileTab] = useState<MobileTab>("ours");
  const abortRef = useRef<AbortController | null>(null);

  const runOurs = useCallback(async (q: string) => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    setOurs({
      isLoading: true,
      answer: "",
      steps: [],
      blocks: [],
      mode: null,
      elapsed: null,
    });

    let collected = "";
    let collectedBlocks: BlockRef[] = [];
    let collectedTrace: PipelineStep[] = [];
    let mode = "";

    try {
      await queryStreamAPI(
        q,
        undefined,
        {
          onRouting: (data) => {
            mode = data.mode;
            setOurs((p) => ({ ...p, mode: data.mode }));
          },
          onRetrievalStep: (step) => {
            collectedTrace = [...collectedTrace, step];
            setOurs((p) => ({ ...p, steps: collectedTrace }));
          },
          onBlocks: (blocks) => {
            collectedBlocks = blocks;
            setOurs((p) => ({ ...p, blocks }));
          },
          onToken: (text) => {
            collected += text;
            setOurs((p) => ({ ...p, answer: collected }));
          },
          onDone: (data) => {
            const finalAnswer = data.answer || collected;
            setOurs((p) => ({
              ...p,
              answer: finalAnswer,
              steps: collectedTrace,
              blocks: collectedBlocks,
              mode: mode || p.mode,
              elapsed: data.elapsed_seconds,
              isLoading: false,
            }));
          },
          onError: (detail) => {
            setOurs((p) => ({
              ...p,
              answer: "Error: " + detail,
              isLoading: false,
            }));
          },
        },
        ctrl.signal
      );
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setOurs((p) => ({ ...p, isLoading: false }));
      }
    }
  }, []);

  const runBaseline = useCallback(
    async (
      system: Baseline,
      q: string,
      setter: React.Dispatch<React.SetStateAction<BaselinePaneData>>
    ) => {
      setter({ record: null, error: null, playKey: 0 });
      const lookup = await loadBaseline(system, q);
      if (!lookup.ok) {
        setter({
          record: null,
          error: lookup.error || "not recorded",
          playKey: 0,
        });
        return;
      }
      setter((prev) => ({
        record: lookup.data!,
        error: null,
        playKey: prev.playKey + 1,
      }));
    },
    []
  );

  const handleSubmit = useCallback(() => {
    const q = query.trim();
    if (!q || ours.isLoading) return;
    setActiveQuery(q);
    runOurs(q);
    runBaseline("lightrag", q, setLightrag);
    runBaseline("htmlrag", q, setHtmlrag);
  }, [query, ours.isLoading, runOurs, runBaseline]);

  const handlePickPreset = useCallback((preset: PresetCase) => {
    setQuery(preset.q);
  }, []);

  // Cleanup on unmount
  useEffect(() => () => abortRef.current?.abort(), []);

  const oursSummary = {
    name: "Ours" as const,
    blocks: ours.blocks,
    elapsed: ours.elapsed,
    isReady: !ours.isLoading && ours.answer.length > 0,
  };
  const lightragSummary = {
    name: "LightRAG" as const,
    blocks: lightrag.record?.blocks ?? [],
    elapsed: lightrag.record?.elapsed_seconds ?? null,
    isReady: lightrag.record !== null,
  };
  const htmlragSummary = {
    name: "HTMLRAG" as const,
    blocks: htmlrag.record?.blocks ?? [],
    elapsed: htmlrag.record?.elapsed_seconds ?? null,
    isReady: htmlrag.record !== null,
  };

  return (
    <div className="h-screen flex flex-col">
      <TopNav />

      {/* Active question banner */}
      {activeQuery && (
        <div className="flex-none px-4 sm:px-6 py-2 border-b border-border bg-surface-alt/40">
          <p className="text-[11px] font-mono text-text-muted truncate">
            <span className="uppercase tracking-[0.16em]">Active question:</span>{" "}
            <span className="text-text-main">{activeQuery}</span>
          </p>
        </div>
      )}

      {/* Mobile tab strip */}
      <div className="md:hidden flex-none flex border-b border-border bg-surface-alt/50">
        {(["lightrag", "htmlrag", "ours"] as MobileTab[]).map((t) => {
          const active = t === mobileTab;
          const label =
            t === "lightrag" ? "LightRAG" : t === "htmlrag" ? "HTMLRAG" : "Ours";
          return (
            <button
              key={t}
              onClick={() => setMobileTab(t)}
              className={`flex-1 py-2 text-[11px] font-mono uppercase tracking-[0.14em] transition-colors ${
                active
                  ? "text-primary border-b-2 border-primary"
                  : "text-text-muted hover:text-text-main"
              }`}
            >
              {label}
            </button>
          );
        })}
      </div>

      {/* 3-pane row (desktop) / single-active (mobile) */}
      <div className="flex-1 min-h-0 overflow-hidden">
        <div className="hidden xl:grid h-full grid-cols-[27fr_27fr_46fr] divide-x divide-border min-w-[1100px] xl:min-w-0">
          <CompareBaselinePane
            systemName="LightRAG"
            modeLabel="Entity-centric KG retrieval"
            badgeTone="neutral"
            record={lightrag.record}
            loadError={activeQuery ? lightrag.error : null}
            playKey={lightrag.playKey}
          />
          <CompareBaselinePane
            systemName="HTMLRAG"
            modeLabel="HTML-pruning + flat retrieval"
            badgeTone="accent"
            record={htmlrag.record}
            loadError={activeQuery ? htmlrag.error : null}
            playKey={htmlrag.playKey}
          />
          <CompareOursPane state={ours} />
        </div>

        {/* Mid-size: horizontal scroll of 3 panes */}
        <div className="hidden md:flex xl:hidden h-full overflow-x-auto divide-x divide-border">
          <div className="min-w-[360px] flex-1">
            <CompareBaselinePane
              systemName="LightRAG"
              modeLabel="Entity-centric KG retrieval"
              badgeTone="neutral"
              record={lightrag.record}
              loadError={activeQuery ? lightrag.error : null}
              playKey={lightrag.playKey}
            />
          </div>
          <div className="min-w-[360px] flex-1">
            <CompareBaselinePane
              systemName="HTMLRAG"
              modeLabel="HTML-pruning + flat retrieval"
              badgeTone="accent"
              record={htmlrag.record}
              loadError={activeQuery ? htmlrag.error : null}
              playKey={htmlrag.playKey}
            />
          </div>
          <div className="min-w-[420px] flex-[1.4]">
            <CompareOursPane state={ours} />
          </div>
        </div>

        {/* Mobile: single active pane */}
        <div className="md:hidden h-full">
          {mobileTab === "lightrag" && (
            <CompareBaselinePane
              systemName="LightRAG"
              modeLabel="Entity-centric KG retrieval"
              badgeTone="neutral"
              record={lightrag.record}
              loadError={activeQuery ? lightrag.error : null}
              playKey={lightrag.playKey}
            />
          )}
          {mobileTab === "htmlrag" && (
            <CompareBaselinePane
              systemName="HTMLRAG"
              modeLabel="HTML-pruning + flat retrieval"
              badgeTone="accent"
              record={htmlrag.record}
              loadError={activeQuery ? htmlrag.error : null}
              playKey={htmlrag.playKey}
            />
          )}
          {mobileTab === "ours" && <CompareOursPane state={ours} />}
        </div>
      </div>

      {/* Metrics + composer */}
      {activeQuery && (
        <MetricsRow systems={[lightragSummary, htmlragSummary, oursSummary]} />
      )}
      <UnifiedComposer
        value={query}
        onChange={setQuery}
        onSubmit={handleSubmit}
        isLoading={ours.isLoading}
        presets={PRESETS}
        onPickPreset={handlePickPreset}
      />
    </div>
  );
}
