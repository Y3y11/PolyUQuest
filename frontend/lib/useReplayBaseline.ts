"use client";

import { useEffect, useRef, useState } from "react";
import type { BaselineRecord } from "@/lib/baselineLoader";

interface PlayedState {
  answer: string;
  blocksShown: number;
  traceShown: number;
  elapsedShown: number;
  isPlaying: boolean;
  done: boolean;
}

// Synthetic typewriter "stream" of a pre-recorded baseline. Paces character
// emission so the total runtime matches the recorded elapsed_seconds.
export function useReplayBaseline(
  record: BaselineRecord | null,
  playKey: number
): PlayedState {
  const [state, setState] = useState<PlayedState>({
    answer: "",
    blocksShown: 0,
    traceShown: 0,
    elapsedShown: 0,
    isPlaying: false,
    done: false,
  });
  const timersRef = useRef<number[]>([]);

  useEffect(() => {
    timersRef.current.forEach((id) => window.clearTimeout(id));
    timersRef.current = [];

    if (!record) {
      setState({
        answer: "",
        blocksShown: 0,
        traceShown: 0,
        elapsedShown: 0,
        isPlaying: false,
        done: false,
      });
      return;
    }

    setState({
      answer: "",
      blocksShown: 0,
      traceShown: 0,
      elapsedShown: 0,
      isPlaying: true,
      done: false,
    });

    const totalMs = Math.max(400, record.elapsed_seconds * 1000);
    const chars = record.answer.length;
    const stepMs = chars > 0 ? Math.max(8, Math.floor(totalMs / chars)) : 16;

    // Trace and blocks reveal at fixed beats so judges see them populate.
    const traceCount = record.pipeline_trace.length;
    const blockCount = record.blocks.length;

    record.pipeline_trace.forEach((_, i) => {
      const at = ((i + 1) / Math.max(traceCount, 1)) * totalMs * 0.45;
      const id = window.setTimeout(() => {
        setState((s) => ({ ...s, traceShown: i + 1 }));
      }, at);
      timersRef.current.push(id);
    });

    const blocksAt = totalMs * 0.5;
    const blocksId = window.setTimeout(() => {
      setState((s) => ({ ...s, blocksShown: blockCount }));
    }, blocksAt);
    timersRef.current.push(blocksId);

    // Token-by-character drip for the answer.
    const startAt = totalMs * 0.55;
    for (let i = 1; i <= chars; i++) {
      const at = startAt + (i / chars) * (totalMs - startAt);
      const id = window.setTimeout(() => {
        setState((s) => ({
          ...s,
          answer: record.answer.slice(0, i),
          elapsedShown: Math.min(record.elapsed_seconds, (at / 1000)),
        }));
      }, at);
      timersRef.current.push(id);
    }

    const doneId = window.setTimeout(() => {
      setState({
        answer: record.answer,
        blocksShown: blockCount,
        traceShown: traceCount,
        elapsedShown: record.elapsed_seconds,
        isPlaying: false,
        done: true,
      });
    }, totalMs + 30);
    timersRef.current.push(doneId);

    return () => {
      timersRef.current.forEach((id) => window.clearTimeout(id));
      timersRef.current = [];
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [record, playKey]);

  return state;
}
