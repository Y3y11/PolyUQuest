const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

export interface Turn {
  user: string;
  assistant: string;
}

export interface BlockRef {
  block_id: string;
  content: string;
  heading_context: string;
  source_url: string;
  source_title: string;
  score: number;
}

export interface PipelineStep {
  step: string;
  label: string;
  duration_ms: number;
  data: Record<string, unknown>;
}

export interface QueryResponse {
  answer: string;
  mode: string;
  routing_reasoning: string;
  blocks: BlockRef[];
  elapsed_seconds: number;
  sub_queries?: { query: string; focus: string }[];
  keywords_extracted?: string[];
  entities_expanded?: number;
  pipeline_trace: PipelineStep[];
}

export interface AgentAction {
  sequence: number;
  action: string;
  status: "started" | "succeeded" | "failed" | "skipped";
  duration_ms: number;
  details: Record<string, unknown>;
}

export interface AgentAssessment {
  decision: "answer" | "expand" | "refresh" | "abstain";
  confidence: number;
  reasons: string[];
  supported_claims: string[];
  missing_claims: string[];
}

export interface AgentExplorationSummary {
  iterations: number;
  pages_fetched: number;
  pages_revalidated: number;
  conditional_cache_hits: number;
  fetch_failures: number;
  frontier_candidates_seen: number;
  temporary_evidence_blocks: number;
  patches_published: number;
  indexing_jobs_queued: number;
  pages_index_accepted: number;
  pages_evidence_only: number;
  pages_discarded: number;
  stop_reason: string;
}

interface AgentEvidenceBlock {
  block_id: string;
  content: string;
  heading_context: string;
  source_url: string;
  source_title: string;
  scores?: {
    retrieval?: number;
    reranker?: number | null;
    bm25?: number | null;
  };
}

export interface AgentQueryResponse {
  run_id: string;
  answer: string;
  response_status: "answered" | "partial" | "abstained" | "error";
  mode: string;
  evidence: AgentEvidenceBlock[];
  actions: AgentAction[];
  exploration: AgentExplorationSummary;
  pipeline_trace: PipelineStep[];
  elapsed_seconds: number;
}

export type AgentActivity =
  | { kind: "action"; action: AgentAction }
  | { kind: "assessment"; assessment: AgentAssessment }
  | { kind: "summary"; summary: AgentExplorationSummary };

export interface GraphStats {
  webpages: number;
  fetched_webpages?: number;
  stub_webpages?: number;
  blocks: number;
  entities: number;
  topic_keywords: number;
  links_to: number;
  contains: number;
  relates_to: number;
  extracted_from: number;
}

export interface GraphNode {
  id: string;
  label: string;
  type: string;
  properties: Record<string, unknown>;
}

export interface GraphEdge {
  source: string;
  target: string;
  type: string;
  properties: Record<string, unknown>;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

async function apiFetch<T>(
  input: RequestInfo,
  init?: RequestInit
): Promise<T> {
  const res = await fetch(input, init);
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.text();
      detail = body ? ` — ${body.slice(0, 200)}` : "";
    } catch {}
    throw new Error(`API error ${res.status}: ${res.statusText}${detail}`);
  }
  return res.json();
}

export async function queryAPI(
  query: string,
  mode?: string,
  signal?: AbortSignal,
  history?: Turn[]
): Promise<QueryResponse> {
  return apiFetch<QueryResponse>(`${API_BASE}/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query,
      mode: mode || null,
      history: history && history.length > 0 ? history : null,
    }),
    signal,
  });
}

// Build a sliding window of the last N user/assistant pairs from the chat
// transcript. Skips any in-flight assistant placeholder (empty content).
// Source of truth stays in `page.tsx`'s `messages` array; we derive on submit.
export function recentHistory(
  messages: { role: "user" | "assistant"; content: string }[],
  n: number = 3
): Turn[] {
  const turns: Turn[] = [];
  let pendingUser: string | null = null;
  for (const m of messages) {
    if (m.role === "user") {
      pendingUser = m.content;
    } else if (m.role === "assistant" && pendingUser !== null) {
      const content = (m.content || "").trim();
      if (!content) {
        // skip in-flight placeholders so we don't ship empty assistant turns
        pendingUser = null;
        continue;
      }
      turns.push({ user: pendingUser, assistant: content });
      pendingUser = null;
    }
  }
  return turns.slice(-n);
}

// --- Streaming (SSE) --------------------------------------------------------
//
// /api/query/stream emits these events:
//   routing      { mode, alt_mode, reasoning }
//   retrieval    PipelineStep
//   blocks       BlockRef[]
//   cache        { hit: boolean }
//   token        { text: string }     -- incremental answer chunk
//   done         { elapsed_seconds, cache_hit?, answer? }
//   error        { detail }

export interface StreamCallbacks {
  onRouting?: (data: {
    mode: string;
    alt_mode: string | null;
    reasoning: string;
    confidence: number | null;
  }) => void;
  onRetrievalStep?: (step: PipelineStep) => void;
  onBlocks?: (blocks: BlockRef[]) => void;
  onCache?: (data: { hit: boolean }) => void;
  onToken?: (text: string) => void;
  onSuggestions?: (items: string[]) => void;
  onDone?: (data: {
    elapsed_seconds: number;
    cache_hit?: boolean;
    answer?: string;
  }) => void;
  onError?: (detail: string) => void;
}

export interface AgentStreamCallbacks {
  onRunStarted?: (data: { run_id: string; query: string }) => void;
  onRouting?: (data: {
    mode: string;
    alt_mode: string | null;
    reasoning: string;
    confidence: number;
    source: string;
  }) => void;
  onAction?: (action: AgentAction) => void;
  onEvidence?: (blocks: BlockRef[]) => void;
  onAssessment?: (assessment: AgentAssessment) => void;
  onDone?: (response: AgentQueryResponse) => void;
  onError?: (detail: string) => void;
}

interface ParsedEvent {
  event: string;
  data: string;
}

function* parseSSE(buffer: string): Generator<ParsedEvent> {
  const blocks = buffer.split("\n\n");
  for (const block of blocks) {
    if (!block.trim()) continue;
    let event = "message";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) {
        event = line.slice("event:".length).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice("data:".length).trim());
      }
    }
    yield { event, data: dataLines.join("\n") };
  }
}

export async function queryStreamAPI(
  query: string,
  mode: string | undefined,
  callbacks: StreamCallbacks,
  signal?: AbortSignal,
  history?: Turn[]
): Promise<void> {
  const res = await fetch(`${API_BASE}/query/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({
      query,
      mode: mode || null,
      history: history && history.length > 0 ? history : null,
    }),
    signal,
  });
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => "");
    throw new Error(`Stream API ${res.status}: ${text.slice(0, 200)}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";

  const dispatch = (event: string, raw: string) => {
    let data: unknown = null;
    try {
      data = raw ? JSON.parse(raw) : null;
    } catch {
      data = raw;
    }
    switch (event) {
      case "routing":
        callbacks.onRouting?.(data as Parameters<NonNullable<StreamCallbacks["onRouting"]>>[0]);
        break;
      case "retrieval":
        callbacks.onRetrievalStep?.(data as PipelineStep);
        break;
      case "blocks":
        callbacks.onBlocks?.((data as BlockRef[]) || []);
        break;
      case "cache":
        callbacks.onCache?.(data as { hit: boolean });
        break;
      case "token":
        callbacks.onToken?.((data as { text: string })?.text || "");
        break;
      case "suggestions":
        callbacks.onSuggestions?.(
          ((data as { items: string[] })?.items || []).filter(
            (s) => typeof s === "string"
          )
        );
        break;
      case "done":
        callbacks.onDone?.(data as { elapsed_seconds: number });
        break;
      case "error":
        callbacks.onError?.((data as { detail: string })?.detail || "stream error");
        break;
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    pending += decoder.decode(value, { stream: true });
    const splitAt = pending.lastIndexOf("\n\n");
    if (splitAt === -1) continue;
    const consumable = pending.slice(0, splitAt + 2);
    pending = pending.slice(splitAt + 2);
    for (const ev of parseSSE(consumable)) {
      dispatch(ev.event, ev.data);
    }
  }
  // Flush any trailing event without the closing blank line.
  if (pending.trim()) {
    for (const ev of parseSSE(pending)) {
      dispatch(ev.event, ev.data);
    }
  }
}

function toBlockRef(block: AgentEvidenceBlock): BlockRef {
  return {
    block_id: block.block_id,
    content: block.content,
    heading_context: block.heading_context || "",
    source_url: block.source_url,
    source_title: block.source_title || "",
    score:
      block.scores?.reranker ??
      block.scores?.retrieval ??
      block.scores?.bm25 ??
      0,
  };
}

/**
 * Run the bounded retrieval Agent. Unlike /query/stream, this endpoint may
 * leave the indexed graph, explore trusted PolyU pages, and report each
 * auditable decision/action before returning the grounded answer.
 */
export async function agentQueryStreamAPI(
  query: string,
  callbacks: AgentStreamCallbacks,
  signal?: AbortSignal,
  history?: Turn[]
): Promise<void> {
  const res = await fetch(`${API_BASE}/agent/query/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({
      query,
      mode: "auto",
      history: history || [],
      explore_web: true,
      persist_discoveries: true,
      freshness: "auto",
      budget: {
        max_iterations: 3,
        max_pages: 5,
        max_depth: 2,
        max_seconds: 90,
      },
    }),
    signal,
  });
  if (!res.ok || !res.body) {
    const body = await res.text().catch(() => "");
    throw new Error(`Agent stream API ${res.status}: ${body.slice(0, 200)}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";

  const dispatch = (event: string, raw: string) => {
    let data: unknown = null;
    try {
      data = raw ? JSON.parse(raw) : null;
    } catch {
      data = raw;
    }
    switch (event) {
      case "run_started":
        callbacks.onRunStarted?.(data as { run_id: string; query: string });
        break;
      case "action":
        callbacks.onAction?.(data as AgentAction);
        break;
      case "routing":
        callbacks.onRouting?.(
          data as {
            mode: string;
            alt_mode: string | null;
            reasoning: string;
            confidence: number;
            source: string;
          }
        );
        break;
      case "evidence": {
        const blocks = ((data as { blocks?: AgentEvidenceBlock[] })?.blocks || []).map(
          toBlockRef
        );
        callbacks.onEvidence?.(blocks);
        break;
      }
      case "assessment":
        callbacks.onAssessment?.(data as AgentAssessment);
        break;
      case "done":
        callbacks.onDone?.(data as AgentQueryResponse);
        break;
      case "error":
        callbacks.onError?.(
          (data as { detail?: string })?.detail || "Agent stream error"
        );
        break;
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    pending += decoder.decode(value, { stream: true });
    const splitAt = pending.lastIndexOf("\n\n");
    if (splitAt === -1) continue;
    const consumable = pending.slice(0, splitAt + 2);
    pending = pending.slice(splitAt + 2);
    for (const event of parseSSE(consumable)) {
      dispatch(event.event, event.data);
    }
  }
  if (pending.trim()) {
    for (const event of parseSSE(pending)) {
      dispatch(event.event, event.data);
    }
  }
}

export async function getGraphStats(
  signal?: AbortSignal
): Promise<GraphStats> {
  return apiFetch<GraphStats>(`${API_BASE}/graph/stats`, { signal });
}

export async function getGraphData(
  centerEntity?: string,
  maxNodes?: number,
  signal?: AbortSignal
): Promise<GraphData> {
  return apiFetch<GraphData>(`${API_BASE}/graph/data`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      center_entity: centerEntity || null,
      max_nodes: maxNodes || 100,
    }),
    signal,
  });
}

export async function getGraphNeighbors(
  nodeId: string,
  hops: number = 1,
  signal?: AbortSignal
): Promise<GraphData> {
  const params = new URLSearchParams({ hops: String(hops) });
  return apiFetch<GraphData>(
    `${API_BASE}/graph/neighbors/${encodeURIComponent(nodeId)}?${params}`,
    { signal }
  );
}

export async function getGraphPath(
  source: string,
  target: string,
  maxDepth: number = 4,
  signal?: AbortSignal
): Promise<GraphData> {
  const params = new URLSearchParams({
    source,
    target,
    max_depth: String(maxDepth),
  });
  return apiFetch<GraphData>(`${API_BASE}/graph/path?${params}`, { signal });
}

export async function getGraphLayeredSlice(
  anchor: string,
  maxNodes: number = 80,
  signal?: AbortSignal
): Promise<GraphData> {
  const params = new URLSearchParams({
    anchor,
    max_nodes: String(maxNodes),
  });
  return apiFetch<GraphData>(`${API_BASE}/graph/layered_slice?${params}`, {
    signal,
  });
}

export async function healthCheck(
  signal?: AbortSignal
): Promise<{
  status: string;
  neo4j: boolean;
  qdrant: boolean;
}> {
  return apiFetch(`${API_BASE}/health`, { signal });
}
