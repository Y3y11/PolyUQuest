"use client";

import RetrievalTrace from "@/components/RetrievalTrace";
import type { PipelineStep } from "@/lib/api";

export default function PipelineStage({
  steps,
  isLoading,
}: {
  steps: PipelineStep[];
  isLoading: boolean;
}) {
  return <RetrievalTrace steps={steps} isLoading={isLoading} />;
}
