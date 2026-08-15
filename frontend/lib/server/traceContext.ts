import { randomBytes } from "node:crypto";

import { context, propagation } from "@opentelemetry/api";

const TRACEPARENT = /^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$/;

function generatedTraceparent(): string {
  return `00-${randomBytes(16).toString("hex")}-${randomBytes(8).toString("hex")}-01`;
}

/** Inject only W3C traceparent; baggage and vendor tracestate are intentionally dropped. */
export function injectBffTraceContext(headers: Headers): void {
  const carrier: Record<string, string> = {};
  propagation.inject(context.active(), carrier);
  const selected = carrier.traceparent?.toLowerCase();
  headers.set("traceparent", selected && TRACEPARENT.test(selected) ? selected : generatedTraceparent());
  headers.delete("tracestate");
  headers.delete("baggage");
}
