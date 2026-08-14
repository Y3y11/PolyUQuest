import { createHash } from "node:crypto";
import { createServer } from "node:http";

const port = Number(process.env.BFF_E2E_UPSTREAM_PORT || "18081");
const expectedHash = process.env.BFF_E2E_EXPECTED_KEY_SHA256 || "";
if (!/^[a-f0-9]{64}$/.test(expectedHash)) {
  throw new Error("BFF_E2E_EXPECTED_KEY_SHA256 is required");
}

const state = {
  total_requests: 0,
  authenticated_requests: 0,
  authentication_failures: 0,
  cancelled_streams: 0,
  completed_streams: 0,
};

function json(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
  });
  response.end(body);
}

async function readBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 131_072) throw new Error("body too large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url || "/", `http://${request.headers.host}`);
  if (request.method === "GET" && url.pathname === "/__state") {
    json(response, 200, state);
    return;
  }
  if (request.method !== "POST" || url.pathname !== "/api/agent/query/stream") {
    json(response, 404, { detail: "not found" });
    return;
  }

  state.total_requests += 1;
  const supplied = String(request.headers["x-api-key"] || "");
  const suppliedHash = createHash("sha256").update(supplied).digest("hex");
  if (suppliedHash !== expectedHash) {
    state.authentication_failures += 1;
    json(response, 401, { detail: "upstream authentication diagnostics: key mismatch" });
    return;
  }
  state.authenticated_requests += 1;

  let payload;
  try {
    payload = JSON.parse(await readBody(request));
  } catch {
    json(response, 400, { detail: "invalid request" });
    return;
  }

  if (payload.query === "simulate-backend-error") {
    response.writeHead(500, { "Content-Type": "text/plain", "X-Internal-Debug": "secret" });
    response.end("internal-bff-e2e-secret-marker");
    return;
  }

  response.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache",
    "X-Request-ID": "upstream-e2e-request",
  });
  response.write('event: run_started\ndata: {"run_id":"bff-e2e-run"}\n\n');

  let completed = false;
  let timer;
  response.on("close", () => {
    if (!completed) {
      state.cancelled_streams += 1;
      if (timer) clearTimeout(timer);
    }
  });

  if (payload.query === "cancel-after-first-event") {
    timer = setTimeout(() => {
      if (!response.destroyed) {
        completed = true;
        state.completed_streams += 1;
        response.end('event: done\ndata: {"answer":"too late"}\n\n');
      }
    }, 5_000);
    return;
  }

  timer = setTimeout(() => {
    response.write('event: action\ndata: {"action":"polyuquest.search"}\n\n');
    timer = setTimeout(() => {
      completed = true;
      state.completed_streams += 1;
      response.end('event: done\ndata: {"answer":"grounded"}\n\n');
    }, 120);
  }, 120);
});

server.listen(port, "0.0.0.0", () => {
  console.log(JSON.stringify({ event: "bff_e2e_upstream_ready", port }));
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
