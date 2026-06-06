import type { BlockRef, PipelineStep } from "@/lib/api";

export interface BaselineRecord {
  system_name: string;
  mode_label: string;
  answer: string;
  blocks: BlockRef[];
  pipeline_trace: PipelineStep[];
  elapsed_seconds: number;
  recorded_at?: string;
  notes?: string;
}

export interface BaselineLookup {
  ok: boolean;
  data?: BaselineRecord;
  slug: string;
  error?: string;
}

// Slug rule (documented in public/baselines/README.md):
//   lowercase, strip punctuation, collapse whitespace,
//   keep first 6 alphanumeric tokens, join with "_".
export function slugifyQuestion(q: string): string {
  return q
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, " ")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 6)
    .join("_");
}

export async function loadBaseline(
  system: "lightrag" | "htmlrag",
  question: string
): Promise<BaselineLookup> {
  const slug = slugifyQuestion(question);
  if (!slug) return { ok: false, slug, error: "empty slug" };
  const url = `/baselines/${system}/${slug}.json`;
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) {
      return { ok: false, slug, error: `not recorded (${res.status})` };
    }
    const data = (await res.json()) as BaselineRecord;
    return { ok: true, slug, data };
  } catch (err) {
    return { ok: false, slug, error: (err as Error).message };
  }
}
