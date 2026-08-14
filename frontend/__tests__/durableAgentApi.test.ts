import { describe, expect, it, vi } from "vitest";

import {
  parseSSE,
  streamAgentRunEventsAPI,
  type AgentQueryResponse,
} from "@/lib/api";

const runId = "run-0123456789abcdef0123456789abcdef";

function completedResponse(): AgentQueryResponse {
  return {
    run_id: runId,
    answer: "Grounded answer [1]",
    response_status: "answered",
    mode: "mode_b",
    evidence: [],
    actions: [],
    exploration: {
      iterations: 1,
      pages_fetched: 1,
      pages_revalidated: 0,
      conditional_cache_hits: 0,
      fetch_failures: 0,
      frontier_candidates_seen: 1,
      temporary_evidence_blocks: 2,
      patches_published: 1,
      indexing_jobs_queued: 1,
      pages_index_accepted: 1,
      pages_evidence_only: 0,
      pages_discarded: 0,
      stop_reason: "evidence_sufficient",
    },
    pipeline_trace: [],
    elapsed_seconds: 1.2,
  };
}

describe("durable Agent SSE client", () => {
  it("parses event identifiers, CRLF and keepalives", () => {
    const events = [...parseSSE(": keepalive\r\n\r\nid: 4\r\nevent: action\r\ndata: {\"ok\":true}\r\n\r\n")];
    expect(events).toEqual([
      { id: 4, event: "action", data: '{"ok":true}' },
    ]);
  });

  it("reconnects with Last-Event-ID and does not rerun the query", async () => {
    const response = completedResponse();
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response('id: 1\nevent: run_started\ndata: {"run_id":"' + runId + '","query":"q"}\n\n')
      )
      .mockResolvedValueOnce(
        Response.json({
          run_id: runId,
          query: "q",
          status: "running",
          attempts: 1,
          max_attempts: 2,
          cancel_requested: false,
          last_event_id: 1,
          created_at: "2026-08-14T00:00:00Z",
          updated_at: "2026-08-14T00:00:00Z",
          started_at: "2026-08-14T00:00:00Z",
          completed_at: null,
          error_code: null,
          error: null,
          result: null,
        })
      )
      .mockResolvedValueOnce(
        new Response(
          `id: 2\nevent: done\ndata: ${JSON.stringify(response)}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } }
        )
      );
    vi.stubGlobal("fetch", fetchMock);
    const done = vi.fn();
    const reconnect = vi.fn();

    const cursor = await streamAgentRunEventsAPI(
      runId,
      { onDone: done },
      undefined,
      { maxReconnects: 1, onReconnect: reconnect }
    );

    expect(cursor).toBe(2);
    expect(done).toHaveBeenCalledWith(response);
    expect(reconnect).toHaveBeenCalledTimes(1);
    expect(new Headers(fetchMock.mock.calls[2][1]?.headers).get("last-event-id")).toBe("1");
    vi.unstubAllGlobals();
  });
});
