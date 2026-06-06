"use client";

import SourceBlocks from "@/components/SourceBlocks";
import type { BlockRef } from "@/lib/api";

export default function BlocksStage({
  blocks,
  highlightedIndex,
}: {
  blocks: BlockRef[];
  highlightedIndex: number | null;
}) {
  return <SourceBlocks blocks={blocks} highlightedIndex={highlightedIndex} />;
}
