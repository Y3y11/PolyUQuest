import { describe, expect, it, vi } from "vitest";

import { loadBffConfig, type BffConfig } from "@/lib/server/bffConfig";
import { proxyBffRequest } from "@/lib/server/bffProxy";

const encoder = new TextEncoder();

function config(overrides: Partial<BffConfig> = {}): BffConfig {
  return {
    backendApiUrl: "http://api:8000/api",
    backendApiKey: "reader-key-for-contract",
    allowedOrigins: new Set(["https://quest.example.test"]),
    maxRequestBytes: 1_024,
    upstreamTimeoutMs: 2_000,
    requireOriginForUnsafeMethods: true,
    ...overrides,
  };
}

function postRequest(
  path: string,
  body = JSON.stringify({ query: "how to apply" }),
  origin: string | null = "https://quest.example.test"
): Request {
  const headers = new Headers({
    Accept: "text/event-stream",
    "Content-Type": "application/json",
    "X-API-Key": "browser-must-not-control-this",
  });
  if (origin) headers.set("Origin", origin);
  return new Request(`https://quest.example.test/api/${path}`, {
    method: "POST",
    headers,
    body,
  });
}

describe("BFF production configuration", () => {
  it("loads the raw reader credential from a secret file", () => {
    const readSecret = vi.fn(() => "reader-key-from-secret-file\n");
    const selected = loadBffConfig(
      {
        NODE_ENV: "production",
        BACKEND_API_URL: "http://api:8000/api",
        BFF_BACKEND_API_KEY_FILE: "/run/secrets/bff_backend_api_key",
        BFF_BACKEND_API_KEY: "environment-key-must-be-ignored",
        BFF_ALLOWED_ORIGINS: "https://quest.example.test",
      },
      readSecret
    );

    expect(readSecret).toHaveBeenCalledWith("/run/secrets/bff_backend_api_key");
    expect(selected.backendApiKey).toBe("reader-key-from-secret-file");
    expect(selected.requireOriginForUnsafeMethods).toBe(true);
  });

  it("fails closed when production has no secret file", () => {
    expect(() =>
      loadBffConfig({
        NODE_ENV: "production",
        BACKEND_API_URL: "http://api:8000/api",
        BFF_BACKEND_API_KEY: "environment-only-key",
        BFF_ALLOWED_ORIGINS: "https://quest.example.test",
      })
    ).toThrow(/BFF_BACKEND_API_KEY_FILE/);
  });
});

describe("BFF route and request policy", () => {
  it("rejects unknown and privileged routes before calling upstream", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const unknown = await proxyBffRequest(
      postRequest("admin/repair"),
      { params: { path: ["admin", "repair"] } },
      { config: config(), fetchImpl }
    );
    const operator = await proxyBffRequest(
      new Request("https://quest.example.test/api/workers/status"),
      { params: { path: ["workers", "status"] } },
      { config: config(), fetchImpl }
    );

    expect(unknown.status).toBe(404);
    expect(operator.status).toBe(404);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("distinguishes a valid path with a forbidden method", async () => {
    const response = await proxyBffRequest(
      new Request("https://quest.example.test/api/graph/stats", { method: "POST" }),
      { params: { path: ["graph", "stats"] } },
      { config: config() }
    );
    expect(response.status).toBe(405);
  });

  it("rejects missing or cross-site Origin for production POST", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    for (const origin of [null, "https://evil.example"]) {
      const response = await proxyBffRequest(
        postRequest("agent/query/stream", undefined, origin),
        { params: { path: ["agent", "query", "stream"] } },
        { config: config(), fetchImpl }
      );
      expect(response.status).toBe(403);
    }
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("enforces the actual request body size", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const response = await proxyBffRequest(
      postRequest("agent/query/stream", "x".repeat(1_025)),
      { params: { path: ["agent", "query", "stream"] } },
      { config: config(), fetchImpl }
    );
    expect(response.status).toBe(413);
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("BFF upstream and SSE contract", () => {
  it("injects the server key and preserves ordered SSE chunks", async () => {
    let upstreamHeaders: Headers | undefined;
    const fetchImpl = vi.fn<typeof fetch>(async (_input, init) => {
      upstreamHeaders = new Headers(init?.headers);
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode('event: run_started\ndata: {"run_id":"r1"}\n\n'));
            controller.enqueue(encoder.encode('event: done\ndata: {"answer":"ok"}\n\n'));
            controller.close();
          },
        }),
        {
          headers: {
            "Content-Type": "text/event-stream; charset=utf-8",
            "X-Request-ID": "api-request-1",
            "Set-Cookie": "must-not-leak=true",
          },
        }
      );
    });

    const response = await proxyBffRequest(
      postRequest("agent/query/stream"),
      { params: { path: ["agent", "query", "stream"] } },
      { config: config(), fetchImpl, createRequestId: () => "bff-request-1" }
    );

    expect(upstreamHeaders?.get("x-api-key")).toBe("reader-key-for-contract");
    expect(upstreamHeaders?.get("x-api-key")).not.toBe("browser-must-not-control-this");
    expect(response.headers.get("content-type")).toContain("text/event-stream");
    expect(response.headers.get("cache-control")).toBe("no-store, no-transform");
    expect(response.headers.get("x-accel-buffering")).toBe("no");
    expect(response.headers.get("x-request-id")).toBe("api-request-1");
    expect(response.headers.has("set-cookie")).toBe(false);
    expect(await response.text()).toBe(
      'event: run_started\ndata: {"run_id":"r1"}\n\n' +
        'event: done\ndata: {"answer":"ok"}\n\n'
    );
  });

  it("aborts the upstream request when the downstream reader cancels", async () => {
    let upstreamSignal: AbortSignal | null = null;
    const fetchImpl = vi.fn<typeof fetch>(async (_input, init) => {
      upstreamSignal = init?.signal || null;
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode("event: action\ndata: {}\n\n"));
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } }
      );
    });
    const response = await proxyBffRequest(
      postRequest("agent/query/stream"),
      { params: { path: ["agent", "query", "stream"] } },
      { config: config(), fetchImpl }
    );
    const reader = response.body!.getReader();
    await reader.read();
    await reader.cancel("browser stopped");

    expect(upstreamSignal).not.toBeNull();
    expect(upstreamSignal!.aborted).toBe(true);
  });

  it("sanitizes backend authentication failures", async () => {
    const response = await proxyBffRequest(
      postRequest("agent/query/stream"),
      { params: { path: ["agent", "query", "stream"] } },
      {
        config: config(),
        fetchImpl: async () =>
          new Response("internal auth diagnostics and secret", {
            status: 401,
            headers: { "X-Internal-Debug": "do-not-return" },
          }),
      }
    );
    const body = await response.text();
    expect(response.status).toBe(502);
    expect(body).toContain("backend_authentication_failed");
    expect(body).not.toContain("internal auth diagnostics");
    expect(response.headers.has("x-internal-debug")).toBe(false);
  });

  it("maps a pre-header upstream timeout to 504", async () => {
    vi.useFakeTimers();
    try {
      const fetchImpl = vi.fn<typeof fetch>(
        async (_input, init) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener(
              "abort",
              () => reject(new DOMException("aborted", "AbortError")),
              { once: true }
            );
          })
      );
      const pending = proxyBffRequest(
        postRequest("agent/query/stream"),
        { params: { path: ["agent", "query", "stream"] } },
        { config: config({ upstreamTimeoutMs: 25 }), fetchImpl }
      );
      await vi.advanceTimersByTimeAsync(25);
      const response = await pending;

      expect(response.status).toBe(504);
      expect(await response.text()).toContain("backend_timeout");
    } finally {
      vi.useRealTimers();
    }
  });
});
