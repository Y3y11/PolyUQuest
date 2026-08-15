import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";

import type { BffConfig } from "./bffConfig";

export const GATEWAY_IDENTITY_HEADER = "X-PolyUQuest-Gateway-Identity";
export const INTERNAL_IDENTITY_HEADER = "X-PolyUQuest-Identity";
export const GATEWAY_JWT_TYPE = "polyuquest-gateway+jwt";
export const INTERNAL_JWT_TYPE = "polyuquest-internal+jwt";

export interface IdentityClaims {
  v: 1;
  iss: string;
  aud: string;
  sub: string;
  tenant_id: string;
  groups: string[];
  iat: number;
  exp: number;
  jti: string;
}

export class IdentityAssertionError extends Error {}

function encode(value: object): string {
  return Buffer.from(JSON.stringify(value), "utf8").toString("base64url");
}

function parsePart(value: string): Record<string, unknown> {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new IdentityAssertionError();
  let parsed: unknown;
  try {
    parsed = JSON.parse(Buffer.from(value, "base64url").toString("utf8"));
  } catch {
    throw new IdentityAssertionError();
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new IdentityAssertionError();
  }
  return parsed as Record<string, unknown>;
}

export function encodeIdentityAssertion(
  claims: IdentityClaims,
  secret: string,
  type: string
): string {
  const unsigned = `${encode({ alg: "HS256", typ: type })}.${encode(claims)}`;
  const signature = createHmac("sha256", secret).update(unsigned).digest("base64url");
  return `${unsigned}.${signature}`;
}

export function decodeIdentityAssertion(
  token: string,
  secret: string,
  options: {
    type: string;
    issuer: string;
    audience: string;
    maxTtlSeconds: number;
    clockSkewSeconds: number;
    nowSeconds?: number;
  }
): IdentityClaims {
  if (token.length > 4096 || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token)) {
    throw new IdentityAssertionError();
  }
  const [headerPart, payloadPart, signaturePart] = token.split(".");
  const header = parsePart(headerPart);
  if (
    Object.keys(header).length !== 2 ||
    header.alg !== "HS256" ||
    header.typ !== options.type
  ) {
    throw new IdentityAssertionError();
  }
  const expected = createHmac("sha256", secret)
    .update(`${headerPart}.${payloadPart}`)
    .digest();
  const supplied = Buffer.from(signaturePart, "base64url");
  if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
    throw new IdentityAssertionError();
  }
  const raw = parsePart(payloadPart);
  const required = ["aud", "exp", "groups", "iat", "iss", "jti", "sub", "tenant_id", "v"];
  if (Object.keys(raw).sort().join(",") !== required.join(",")) {
    throw new IdentityAssertionError();
  }
  const identifier = /^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$/;
  const group = /^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,63}$/;
  if (
    raw.v !== 1 ||
    raw.iss !== options.issuer ||
    raw.aud !== options.audience ||
    typeof raw.sub !== "string" ||
    !identifier.test(raw.sub) ||
    typeof raw.tenant_id !== "string" ||
    !identifier.test(raw.tenant_id) ||
    typeof raw.jti !== "string" ||
    !identifier.test(raw.jti) ||
    !Array.isArray(raw.groups) ||
    raw.groups.length > 32 ||
    raw.groups.some((item) => typeof item !== "string" || !group.test(item)) ||
    new Set(raw.groups).size !== raw.groups.length ||
    !Number.isInteger(raw.iat) ||
    !Number.isInteger(raw.exp)
  ) {
    throw new IdentityAssertionError();
  }
  const now = options.nowSeconds ?? Math.floor(Date.now() / 1000);
  const issuedAt = raw.iat as number;
  const expiresAt = raw.exp as number;
  if (
    expiresAt <= issuedAt ||
    expiresAt - issuedAt > options.maxTtlSeconds ||
    issuedAt > now + options.clockSkewSeconds ||
    expiresAt < now - options.clockSkewSeconds
  ) {
    throw new IdentityAssertionError();
  }
  return raw as unknown as IdentityClaims;
}

export function internalIdentityAssertion(request: Request, config: BffConfig): string | null {
  if (config.identityMode === "disabled") return null;
  const gateway = decodeIdentityAssertion(
    request.headers.get(GATEWAY_IDENTITY_HEADER) || "",
    config.gatewayIdentitySecret,
    {
      type: GATEWAY_JWT_TYPE,
      issuer: config.gatewayIdentityIssuer,
      audience: config.gatewayIdentityAudience,
      maxTtlSeconds: config.identityMaxTtlSeconds,
      clockSkewSeconds: config.identityClockSkewSeconds,
    }
  );
  const issuedAt = Math.floor(Date.now() / 1000);
  return encodeIdentityAssertion(
    {
      ...gateway,
      iss: config.internalIdentityIssuer,
      aud: config.internalIdentityAudience,
      iat: issuedAt,
      exp: issuedAt + Math.min(60, config.identityMaxTtlSeconds),
      jti: `bff-${randomUUID().replaceAll("-", "")}`,
    },
    config.internalIdentitySecret,
    INTERNAL_JWT_TYPE
  );
}
