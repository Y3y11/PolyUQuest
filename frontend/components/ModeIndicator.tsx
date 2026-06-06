"use client";

import { motion } from "framer-motion";
import clsx from "clsx";

const MODE_LABELS: Record<string, { label: string; desc: string }> = {
  A: { label: "Direct", desc: "Single-hop factual retrieval" },
  B: { label: "Navigation", desc: "Cross-page information aggregation" },
  C: { label: "Reasoning", desc: "Multi-hop entity reasoning" },
  mode_a: { label: "Direct", desc: "Single-hop factual retrieval" },
  mode_b: { label: "Navigation", desc: "Cross-page information aggregation" },
  mode_c: { label: "Reasoning", desc: "Multi-hop entity reasoning" },
};

export default function ModeIndicator({ mode }: { mode: string }) {
  const info = MODE_LABELS[mode] || { label: mode, desc: "" };
  const letter = mode.replace("mode_", "").toUpperCase();

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.9 }}
      animate={{ opacity: 1, scale: 1 }}
      className="inline-flex items-center gap-2"
    >
      <span
        className={clsx(
          "inline-flex items-center justify-center w-7 h-7 rounded-md text-xs font-bold font-mono",
          letter === "A" && "bg-emerald-600 text-white",
          letter === "B" && "bg-blue-600 text-white",
          letter === "C" && "bg-amber-600 text-white"
        )}
      >
        {letter}
      </span>
      <span className="text-sm font-medium text-text-main">{info.label}</span>
      <span className="text-xs text-text-muted hidden sm:inline">
        {info.desc}
      </span>
    </motion.div>
  );
}
