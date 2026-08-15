export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  if ((process.env.OTEL_TRACING_MODE || "propagate") !== "otlp") return;
  const { registerOTel } = await import("@vercel/otel");
  registerOTel({ serviceName: process.env.OTEL_SERVICE_NAME || "polyuquest-bff" });
}
