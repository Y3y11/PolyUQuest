"use client";

import { motion } from "framer-motion";
import { Clock, Layers, GitMerge, Target } from "lucide-react";
import type { BlockRef } from "@/lib/api";

interface SystemSummary {
  name: string;
  blocks: BlockRef[];
  elapsed: number | null;
  isReady: boolean;
}

// Cosine-of-sets overlap: |A ∩ B ∩ C| / |A ∪ B ∪ C| over block_id.
function computeOverlap(systems: SystemSummary[]): {
  jaccard: number | null;
  intersection: number;
  union: number;
} {
  const ready = systems.filter((s) => s.isReady && s.blocks.length > 0);
  if (ready.length < 2) return { jaccard: null, intersection: 0, union: 0 };
  const sets = ready.map(
    (s) => new Set(s.blocks.map((b) => b.block_id).filter(Boolean))
  );
  const union = new Set<string>();
  sets.forEach((s) => s.forEach((id) => union.add(id)));
  const intersection = [...sets[0]].filter((id) =>
    sets.slice(1).every((s) => s.has(id))
  );
  if (union.size === 0) return { jaccard: null, intersection: 0, union: 0 };
  return {
    jaccard: intersection.length / union.size,
    intersection: intersection.length,
    union: union.size,
  };
}

export default function MetricsRow({
  systems,
}: {
  systems: SystemSummary[];
}) {
  const overlap = computeOverlap(systems);
  const fastest = systems
    .filter((s) => s.isReady && s.elapsed != null)
    .sort((a, b) => (a.elapsed ?? Infinity) - (b.elapsed ?? Infinity))[0];

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22 }}
      className="flex-none border-t border-border bg-surface-alt/50"
    >
      <div className="px-4 sm:px-6 py-2.5 grid grid-cols-2 sm:grid-cols-4 gap-3 sm:gap-5">
        <MetricCell
          icon={Clock}
          label="Fastest"
          value={
            fastest && fastest.elapsed != null
              ? `${fastest.name} · ${fastest.elapsed.toFixed(1)}s`
              : "—"
          }
        />
        <MetricCell
          icon={Layers}
          label="Blocks · L / H / O"
          value={systems.map((s) => s.blocks.length).join(" / ")}
        />
        <MetricCell
          icon={GitMerge}
          label="Block overlap"
          value={
            overlap.jaccard == null
              ? "—"
              : `${Math.round(overlap.jaccard * 100)}% (${overlap.intersection}/${overlap.union})`
          }
          help="Jaccard over block_id across all ready systems"
        />
        <MetricCell
          icon={Target}
          label="Cited blocks (Ours)"
          value={
            systems.find((s) => s.name === "Ours")?.blocks.length
              ? String(systems.find((s) => s.name === "Ours")!.blocks.length)
              : "—"
          }
        />
      </div>
    </motion.div>
  );
}

function MetricCell({
  icon: Icon,
  label,
  value,
  help,
}: {
  icon: typeof Clock;
  label: string;
  value: string;
  help?: string;
}) {
  return (
    <div title={help} className="flex flex-col gap-1 min-w-0">
      <div className="flex items-center gap-1.5 text-[9.5px] font-mono uppercase tracking-[0.16em] text-text-muted">
        <Icon size={10} />
        <span className="truncate">{label}</span>
      </div>
      <span className="text-[12.5px] font-mono tabular-nums text-text-main truncate">
        {value}
      </span>
    </div>
  );
}
