import { readFileSync } from "node:fs";

export interface BffConfig {
  backendApiUrl: string;
  backendApiKey: string;
  allowedOrigins: ReadonlySet<string>;
  maxRequestBytes: number;
  upstreamTimeoutMs: number;
  requireOriginForUnsafeMethods: boolean;
  tracingMode: "disabled" | "propagate" | "otlp";
}

type SecretReader = (path: string) => string;

function parsePositiveInt(
  value: string | undefined,
  fallback: number,
  name: string,
  min: number,
  max: number
): number {
  if (!value) return fallback;
  if (!/^\d+$/.test(value)) {
    throw new Error(`${name} must be an integer`);
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(`${name} must be between ${min} and ${max}`);
  }
  return parsed;
}

function parseBackendUrl(raw: string): string {
  const url = new URL(raw);
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new Error("BACKEND_API_URL must use http or https");
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error("BACKEND_API_URL cannot contain credentials, query, or fragment");
  }
  return url.toString().replace(/\/$/, "");
}

function parseOrigins(raw: string): ReadonlySet<string> {
  const origins = raw
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => {
      const parsed = new URL(item);
      if (parsed.origin !== item || !['http:', 'https:'].includes(parsed.protocol)) {
        throw new Error("BFF_ALLOWED_ORIGINS entries must be exact http(s) origins");
      }
      return parsed.origin;
    });
  if (origins.length === 0) {
    throw new Error("BFF_ALLOWED_ORIGINS requires at least one origin");
  }
  return new Set(origins);
}

function validateSecret(secret: string): string {
  if (!secret) return "";
  if (!/^[A-Za-z0-9._~-]{16,512}$/.test(secret)) {
    throw new Error("BFF backend API key has an invalid format");
  }
  return secret;
}

export function loadBffConfig(
  env: NodeJS.ProcessEnv = process.env,
  readSecretFile: SecretReader = (path) => readFileSync(path, "utf8")
): BffConfig {
  const production = env.NODE_ENV === "production";
  const tracingMode = env.OTEL_TRACING_MODE || "propagate";
  if (!['disabled', 'propagate', 'otlp'].includes(tracingMode)) {
    throw new Error("OTEL_TRACING_MODE must be disabled, propagate, or otlp");
  }
  const backendApiUrl = parseBackendUrl(
    env.BACKEND_API_URL || (production ? "" : "http://127.0.0.1:8000/api")
  );
  const secretPath = env.BFF_BACKEND_API_KEY_FILE?.trim() || "";
  if (production && !secretPath) {
    throw new Error("BFF_BACKEND_API_KEY_FILE is required in production");
  }
  const secret = secretPath
    ? readSecretFile(secretPath).trim()
    : env.BFF_BACKEND_API_KEY?.trim() || "";
  if (production && !secret) {
    throw new Error("BFF backend API key secret file is empty");
  }
  const originSource =
    env.BFF_ALLOWED_ORIGINS ||
    (production ? "" : "http://localhost:3000,http://127.0.0.1:3000");

  return {
    backendApiUrl,
    backendApiKey: validateSecret(secret),
    allowedOrigins: parseOrigins(originSource),
    maxRequestBytes: parsePositiveInt(
      env.BFF_MAX_REQUEST_BYTES,
      65_536,
      "BFF_MAX_REQUEST_BYTES",
      1_024,
      1_048_576
    ),
    upstreamTimeoutMs:
      parsePositiveInt(
        env.BFF_UPSTREAM_TIMEOUT_SECONDS,
        120,
        "BFF_UPSTREAM_TIMEOUT_SECONDS",
        1,
        600
      ) * 1_000,
    requireOriginForUnsafeMethods: production,
    tracingMode: tracingMode as BffConfig["tracingMode"],
  };
}

let cachedConfig: BffConfig | undefined;

export function getBffConfig(): BffConfig {
  cachedConfig ??= loadBffConfig();
  return cachedConfig;
}
