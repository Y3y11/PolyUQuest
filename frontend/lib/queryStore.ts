"use client";

import { create } from "zustand";
import type { BlockRef, PipelineStep } from "./api";

export type TheatreStage = "pipeline" | "page" | "blocks";

export interface RoutingInfo {
  mode: string;
  alt_mode: string | null;
  confidence: number | null;
  reasoning: string;
}

interface QueryStoreState {
  // streaming gate
  isStreaming: boolean;

  // pipeline outputs
  routing: RoutingInfo | null;
  steps: PipelineStep[];
  blocks: BlockRef[];
  cacheHit: boolean | null;
  elapsedSeconds: number | null;

  // follow-up suggestions (rendered as bubbles above the composer)
  suggestions: string[];

  // UI focus
  hoveredCite: number | null;
  selectedCite: number | null;
  currentStage: TheatreStage;

  // actions
  beginStream: () => void;
  endStream: () => void;
  setRouting: (info: RoutingInfo) => void;
  pushStep: (step: PipelineStep) => void;
  setBlocks: (blocks: BlockRef[]) => void;
  setCacheHit: (hit: boolean) => void;
  setElapsed: (seconds: number) => void;
  setSuggestions: (items: string[], userQuery?: string) => void;
  clearSuggestions: () => void;
  setHoveredCite: (n: number | null) => void;
  setSelectedCite: (n: number | null) => void;
  setStage: (stage: TheatreStage) => void;
  reset: () => void;
}

const initialState = {
  isStreaming: false,
  routing: null,
  steps: [] as PipelineStep[],
  blocks: [] as BlockRef[],
  cacheHit: null,
  elapsedSeconds: null,
  suggestions: [] as string[],
  hoveredCite: null,
  selectedCite: null,
  currentStage: "pipeline" as TheatreStage,
};

function sanitizeSuggestions(items: string[], userQuery?: string): string[] {
  const seen = new Set<string>();
  const qNorm = (userQuery || "").trim().toLowerCase();
  const out: string[] = [];
  for (const raw of items || []) {
    if (typeof raw !== "string") continue;
    const s = raw.trim().replace(/^["']|["']$/g, "");
    if (s.length < 8 || s.length > 120) continue;
    const key = s.toLowerCase();
    if (key === qNorm || seen.has(key)) continue;
    seen.add(key);
    out.push(s);
    if (out.length >= 3) break;
  }
  return out;
}

export const useQueryStore = create<QueryStoreState>((set) => ({
  ...initialState,
  beginStream: () =>
    set({
      isStreaming: true,
      routing: null,
      steps: [],
      blocks: [],
      cacheHit: null,
      elapsedSeconds: null,
      suggestions: [],
      hoveredCite: null,
      selectedCite: null,
    }),
  endStream: () => set({ isStreaming: false }),
  setRouting: (info) => set({ routing: info }),
  pushStep: (step) => set((s) => ({ steps: [...s.steps, step] })),
  setBlocks: (blocks) => set({ blocks }),
  setCacheHit: (hit) => set({ cacheHit: hit }),
  setElapsed: (seconds) => set({ elapsedSeconds: seconds }),
  setSuggestions: (items, userQuery) =>
    set({ suggestions: sanitizeSuggestions(items, userQuery) }),
  clearSuggestions: () => set({ suggestions: [] }),
  setHoveredCite: (n) => set({ hoveredCite: n }),
  setSelectedCite: (n) => set({ selectedCite: n }),
  setStage: (stage) => set({ currentStage: stage }),
  reset: () => set(initialState),
}));

// selected (click) takes precedence over hovered for the highlight target
export const selectHighlightedCite = (s: QueryStoreState): number | null =>
  s.selectedCite ?? s.hoveredCite;
