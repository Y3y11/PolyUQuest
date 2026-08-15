import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";

const port = Number(process.env.BFF_E2E_UPSTREAM_PORT || "18081");
const expectedHash = process.env.BFF_E2E_EXPECTED_KEY_SHA256 || "";
const identitySecretPath = process.env.BFF_E2E_INTERNAL_IDENTITY_SECRET_FILE || "";
if (!/^[a-f0-9]{64}$/.test(expectedHash)) {
  throw new Error("BFF_E2E_EXPECTED_KEY_SHA256 is required");
}
const identitySecret = readFileSync(identitySecretPath, "utf8").trim();
if (Buffer.byteLength(identitySecret) < 32) {
  throw new Error("BFF_E2E internal identity secret is invalid");
}

const state = {
  total_requests: 0,
  authenticated_requests: 0,
  authentication_failures: 0,
  cancelled_streams: 0,
  completed_streams: 0,
  verified_identities: 0,
};

function validInternalIdentity(token) {
  if (!/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token)) return false;
  const [headerPart, payloadPart, signaturePart] = token.split(".");
  let header;
  let claims;
  try {
    header = JSON.parse(Buffer.from(headerPart, "base64url").toString("utf8"));
    claims = JSON.parse(Buffer.from(payloadPart, "base64url").toString("utf8"));
  } catch {
    return false;
  }
  if (header.alg !== "HS256" || header.typ !== "polyuquest-internal+jwt") return false;
  const expected = createHmac("sha256", identitySecret)
    .update(`${headerPart}.${payloadPart}`)
    .digest();
  const supplied = Buffer.from(signaturePart, "base64url");
  const now = Math.floor(Date.now() / 1000);
  return (
    supplied.length === expected.length &&
    timingSafeEqual(supplied, expected) &&
    claims.iss === "polyuquest-bff" &&
    claims.aud === "polyuquest-api" &&
    claims.sub === "browser-e2e-user" &&
    claims.tenant_id === "browser-e2e-tenant" &&
    Number.isInteger(claims.iat) &&
    Number.isInteger(claims.exp) &&
    claims.iat <= now + 5 &&
    claims.exp >= now - 5 &&
    claims.exp - claims.iat <= 120
  );
}

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
  const streamRoute = request.method === "POST" && url.pathname === "/api/agent/query/stream";
  const durableRoute = request.method === "POST" && url.pathname === "/api/agent/runs";
  if (!streamRoute && !durableRoute) {
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

  if (durableRoute) {
    const identity = String(request.headers["x-polyuquest-identity"] || "");
    if (!validInternalIdentity(identity)) {
      json(response, 401, { detail: "invalid internal identity" });
      return;
    }
    await readBody(request);
    state.verified_identities += 1;
    json(response, 202, { run_id: "run-0123456789abcdef0123456789abcdef" });
    return;
  }

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
