import { rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { mkdir } from "node:fs/promises";

const baseUrl = process.env.BFF_E2E_BASE_URL || "http://127.0.0.1:13000";
const upstreamUrl = process.env.BFF_E2E_UPSTREAM_URL || "http://127.0.0.1:13001";
const allowedOrigin = process.env.BFF_E2E_ALLOWED_ORIGIN || baseUrl;
const secretMarker = process.env.BFF_E2E_SECRET_MARKER;
const output = process.env.BFF_E2E_OUTPUT || "frontend/artifacts/bff-e2e/report.json";
if (!secretMarker) throw new Error("BFF_E2E_SECRET_MARKER is required");
const startedAt = new Date();
const checks = [];

function check(name, expected, actual, ok) {
  checks.push({ name, ok: Boolean(ok), expected, actual });
  if (!ok) throw new Error(`${name} failed`);
}

async function waitFor(url, timeoutMs = 90_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { cache: "no-store" });
      if (response.ok) return response;
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`Timed out waiting for ${url}: ${lastError}`);
}

async function state() {
  return (await (await fetch(`${upstreamUrl}/__state`, { cache: "no-store" })).json());
}

function agentBody(query) {
  return JSON.stringify({
    query,
    mode: "auto",
    history: [],
    explore_web: true,
    persist_discoveries: true,
    freshness: "auto",
    budget: { max_iterations: 1, max_pages: 1, max_depth: 1, max_seconds: 10 },
  });
}

async function postAgent(query, origin = allowedOrigin) {
  const headers = { "Content-Type": "application/json", Accept: "text/event-stream" };
  if (origin !== null) headers.Origin = origin;
  return fetch(`${baseUrl}/api/agent/query/stream`, {
    method: "POST",
    headers,
    body: agentBody(query),
  });
}

async function readStream(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  const chunkTimes = [];
  while (true) {
    const item = await reader.read();
    if (item.done) break;
    chunkTimes.push(Date.now());
    text += decoder.decode(item.value, { stream: true });
  }
  text += decoder.decode();
  const events = [...text.matchAll(/^event:\s*(.+)$/gm)].map((match) => match[1].trim());
  return { text, events, chunkTimes };
}

async function run() {
  await waitFor(`${baseUrl}/`);
  await waitFor(`${upstreamUrl}/__state`);
  const before = await state();

  const missingOrigin = await postAgent("missing-origin", null);
  const crossSite = await postAgent("cross-site", "https://evil.example");
  const privileged = await fetch(`${baseUrl}/api/workers/status`);
  const tooLarge = await fetch(`${baseUrl}/api/agent/query/stream`, {
    method: "POST",
    headers: { Origin: allowedOrigin, "Content-Type": "application/json" },
    body: "x".repeat(70_000),
  });
  const afterRejected = await state();
  check(
    "bff.pre_upstream_policy",
    "403 / 403 / 404 / 413 with zero upstream calls",
    {
      missing_origin: missingOrigin.status,
      cross_site: crossSite.status,
      privileged: privileged.status,
      too_large: tooLarge.status,
      upstream_delta: afterRejected.total_requests - before.total_requests,
    },
    missingOrigin.status === 403 &&
      crossSite.status === 403 &&
      privileged.status === 404 &&
      tooLarge.status === 413 &&
      afterRejected.total_requests === before.total_requests
  );

  const streamResponse = await postAgent("stream-contract");
  const stream = await readStream(streamResponse);
  check(
    "bff.sse_passthrough",
    "ordered multi-chunk run_started/action/done",
    {
      status: streamResponse.status,
      events: stream.events,
      chunks: stream.chunkTimes.length,
      body: stream.text.slice(0, 512),
      cache_control: streamResponse.headers.get("cache-control"),
      buffering: streamResponse.headers.get("x-accel-buffering"),
      request_id: streamResponse.headers.get("x-request-id"),
    },
    streamResponse.status === 200 &&
      stream.events.join(",") === "run_started,action,done" &&
      stream.chunkTimes.length >= 2 &&
      streamResponse.headers.get("cache-control") === "no-store, no-transform" &&
      streamResponse.headers.get("x-accel-buffering") === "no" &&
      streamResponse.headers.get("x-request-id") === "upstream-e2e-request"
  );

  const backendError = await postAgent("simulate-backend-error");
  const backendErrorText = await backendError.text();
  check(
    "bff.error_sanitization",
    "502 without internal upstream diagnostics",
    { status: backendError.status, body: backendErrorText },
    backendError.status === 502 &&
      backendErrorText.includes("backend_unavailable") &&
      !backendErrorText.includes("internal-bff-e2e-secret-marker")
  );

  const cancelResponse = await postAgent("cancel-after-first-event");
  const cancelReader = cancelResponse.body.getReader();
  await cancelReader.read();
  await cancelReader.cancel("e2e browser stop");
  const cancelDeadline = Date.now() + 5_000;
  let finalState = await state();
  while (finalState.cancelled_streams < 1 && Date.now() < cancelDeadline) {
    await new Promise((resolve) => setTimeout(resolve, 100));
    finalState = await state();
  }
  check(
    "bff.cancel_propagation",
    "downstream cancel closes upstream stream",
    finalState,
    finalState.cancelled_streams >= 1
  );
  check(
    "bff.server_key_injection",
    "three authenticated upstream calls and no key mismatch",
    finalState,
    finalState.authenticated_requests === 3 && finalState.authentication_failures === 0
  );

  const home = await (await fetch(`${baseUrl}/`)).text();
  const scriptPaths = [...home.matchAll(/<script[^>]+src="([^"]+)"/g)].map(
    (match) => match[1]
  );
  const publicBodies = [home];
  for (const path of scriptPaths) {
    publicBodies.push(await (await fetch(new URL(path, baseUrl))).text());
  }
  const publicBundle = publicBodies.join("\n");
  check(
    "bff.client_bundle_redaction",
    "no raw key, internal API URL, or deprecated public API variable",
    { scripts_scanned: scriptPaths.length },
    !publicBundle.includes(secretMarker) &&
      !publicBundle.includes("http://api:8000") &&
      !publicBundle.includes("NEXT_PUBLIC_API_URL")
  );
}

let status = "passed";
let error = "";
try {
  await run();
} catch (caught) {
  status = "failed";
  error = caught instanceof Error ? caught.message : String(caught);
} finally {
  const completedAt = new Date();
  const report = {
    schema_version: 1,
    scenario_id: "browser-bff-sse",
    status,
    started_at: startedAt.toISOString(),
    completed_at: completedAt.toISOString(),
    duration_ms: completedAt.getTime() - startedAt.getTime(),
    checks,
    error,
  };
  await mkdir(dirname(output), { recursive: true });
  const temporary = `${output}.tmp`;
  await writeFile(temporary, `${JSON.stringify(report, null, 2)}\n`, { mode: 0o600 });
  await rename(temporary, output);
}

if (status !== "passed") {
  throw new Error(error || "BFF E2E failed");
}
