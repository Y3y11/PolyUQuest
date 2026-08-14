import { randomUUID } from "node:crypto";

import { getBffConfig, type BffConfig } from "./bffConfig";

export interface BffRouteContext {
  params: { path?: string[] };
}

interface BffDependencies {
  config?: BffConfig;
  fetchImpl?: typeof fetch;
  createRequestId?: () => string;
}

interface RouteRule {
  methods: ReadonlySet<string>;
  pattern: RegExp;
}

const ROUTES: readonly RouteRule[] = [
  { methods: new Set(["POST"]), pattern: /^query$/ },
  { methods: new Set(["POST"]), pattern: /^query\/stream$/ },
  { methods: new Set(["POST"]), pattern: /^agent\/query\/stream$/ },
  { methods: new Set(["GET"]), pattern: /^graph\/stats$/ },
  { methods: new Set(["POST"]), pattern: /^graph\/data$/ },
  { methods: new Set(["GET"]), pattern: /^graph\/neighbors\/[^/]{1,512}$/ },
  { methods: new Set(["GET"]), pattern: /^graph\/path$/ },
  { methods: new Set(["GET"]), pattern: /^graph\/layered_slice$/ },
  { methods: new Set(["GET"]), pattern: /^health$/ },
];

function jsonError(
  status: number,
  error: string,
  requestId?: string,
  detail?: unknown
): Response {
  return Response.json(
    {
      error,
      ...(requestId ? { request_id: requestId } : {}),
      ...(detail === undefined ? {} : { detail }),
    },
    {
      status,
      headers: {
        "Cache-Control": "no-store",
        ...(requestId ? { "X-BFF-Request-ID": requestId } : {}),
      },
    }
  );
}

function routePath(context: BffRouteContext): string | null {
  const segments = context.params.path;
  if (!segments || segments.length === 0) return null;
  if (
    segments.some(
      (segment) =>
        !segment ||
        segment === "." ||
        segment === ".." ||
        segment.includes("/") ||
        segment.includes("\\") ||
        segment.includes("\0")
    )
  ) {
    return null;
  }
  return segments.join("/");
}

function authorizeRoute(method: string, path: string): 200 | 404 | 405 {
  const matching = ROUTES.filter((route) => route.pattern.test(path));
  if (matching.length === 0) return 404;
  return matching.some((route) => route.methods.has(method)) ? 200 : 405;
}

function originAllowed(request: Request, config: BffConfig): boolean {
  if (!["POST", "PUT", "PATCH", "DELETE"].includes(request.method)) return true;
  const origin = request.headers.get("origin");
  if (!origin) return !config.requireOriginForUnsafeMethods;
  return config.allowedOrigins.has(origin);
}

async function requestBody(request: Request, maxBytes: number): Promise<ArrayBuffer | null> {
  if (["GET", "HEAD"].includes(request.method)) return null;
  const contentLength = request.headers.get("content-length");
  if (contentLength) {
    if (!/^\d+$/.test(contentLength) || Number(contentLength) > maxBytes) {
      throw new RangeError("request body exceeds configured limit");
    }
  }
  const body = await request.arrayBuffer();
  if (body.byteLength > maxBytes) {
    throw new RangeError("request body exceeds configured limit");
  }
  return body;
}

async function safeUpstreamDetail(response: Response): Promise<unknown> {
  const raw = (await response.text()).slice(0, 2_048);
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw) as { detail?: unknown };
    return parsed.detail ?? "request rejected";
  } catch {
    return "request rejected";
  }
}

function responseHeaders(upstream: Response, requestId: string): Headers {
  const headers = new Headers({
    "Cache-Control": "no-store, no-transform",
    "X-BFF-Request-ID": requestId,
    "X-Request-ID": upstream.headers.get("x-request-id") || requestId,
  });
  const contentType = upstream.headers.get("content-type");
  if (contentType) headers.set("Content-Type", contentType);
  if (contentType?.toLowerCase().startsWith("text/event-stream")) {
    headers.set("X-Accel-Buffering", "no");
  }
  return headers;
}

function streamedResponse(
  upstream: Response,
  upstreamController: AbortController,
  request: Request,
  timeout: ReturnType<typeof setTimeout>,
  onRequestAbort: () => void,
  requestId: string
): Response {
  if (!upstream.body) {
    clearTimeout(timeout);
    request.signal.removeEventListener("abort", onRequestAbort);
    return jsonError(502, "backend_empty_response", requestId);
  }
  const reader = upstream.body.getReader();
  let finished = false;
  const cleanup = () => {
    if (finished) return;
    finished = true;
    clearTimeout(timeout);
    request.signal.removeEventListener("abort", onRequestAbort);
  };
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const chunk = await reader.read();
        if (chunk.done) {
          cleanup();
          controller.close();
        } else {
          controller.enqueue(chunk.value);
        }
      } catch (error) {
        cleanup();
        controller.error(error);
      }
    },
    async cancel(reason) {
      cleanup();
      upstreamController.abort(reason);
      await reader.cancel(reason).catch(() => undefined);
    },
  });
  return new Response(stream, {
    status: upstream.status,
    headers: responseHeaders(upstream, requestId),
  });
}

export async function proxyBffRequest(
  request: Request,
  context: BffRouteContext,
  dependencies: BffDependencies = {}
): Promise<Response> {
  const path = routePath(context);
  if (!path) return jsonError(404, "route_not_allowed");
  const routeStatus = authorizeRoute(request.method, path);
  if (routeStatus === 404) return jsonError(404, "route_not_allowed");
  if (routeStatus === 405) return jsonError(405, "method_not_allowed");

  let config: BffConfig;
  try {
    config = dependencies.config ?? getBffConfig();
  } catch {
    return jsonError(503, "bff_not_configured");
  }
  const requestId =
    dependencies.createRequestId?.() || `bff-${randomUUID().replaceAll("-", "")}`;
  if (!originAllowed(request, config)) {
    return jsonError(403, "origin_forbidden", requestId);
  }

  let body: ArrayBuffer | null;
  try {
    body = await requestBody(request, config.maxRequestBytes);
  } catch (error) {
    if (error instanceof RangeError) {
      return jsonError(413, "request_too_large", requestId);
    }
    return jsonError(400, "request_body_invalid", requestId);
  }

  const requestedUrl = new URL(request.url);
  const upstreamUrl = new URL(`${config.backendApiUrl}/${path}`);
  upstreamUrl.search = requestedUrl.search;
  const headers = new Headers({
    Accept: request.headers.get("accept") || "application/json",
    "X-Request-ID": requestId,
  });
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("Content-Type", contentType);
  if (config.backendApiKey) headers.set("X-API-Key", config.backendApiKey);

  const upstreamController = new AbortController();
  let timedOut = false;
  const onRequestAbort = () => upstreamController.abort(request.signal.reason);
  request.signal.addEventListener("abort", onRequestAbort, { once: true });
  const timeout = setTimeout(() => {
    timedOut = true;
    upstreamController.abort(new Error("BFF upstream timeout"));
  }, config.upstreamTimeoutMs);

  let upstream: Response;
  try {
    upstream = await (dependencies.fetchImpl ?? fetch)(upstreamUrl, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: upstreamController.signal,
    });
  } catch {
    clearTimeout(timeout);
    request.signal.removeEventListener("abort", onRequestAbort);
    if (timedOut) return jsonError(504, "backend_timeout", requestId);
    if (request.signal.aborted) return jsonError(499, "client_closed_request", requestId);
    return jsonError(502, "backend_unavailable", requestId);
  }

  if (!upstream.ok) {
    clearTimeout(timeout);
    request.signal.removeEventListener("abort", onRequestAbort);
    if ([400, 409, 422, 429].includes(upstream.status)) {
      const detail = await safeUpstreamDetail(upstream);
      return jsonError(upstream.status, "backend_rejected_request", requestId, detail);
    }
    if ([401, 403].includes(upstream.status)) {
      return jsonError(502, "backend_authentication_failed", requestId);
    }
    return jsonError(502, "backend_unavailable", requestId);
  }

  return streamedResponse(
    upstream,
    upstreamController,
    request,
    timeout,
    onRequestAbort,
    requestId
  );
}
