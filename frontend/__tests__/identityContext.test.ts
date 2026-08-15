import { describe, expect, it, vi } from "vitest";

import type { BffConfig } from "@/lib/server/bffConfig";
import { proxyBffRequest } from "@/lib/server/bffProxy";
import {
  GATEWAY_IDENTITY_HEADER,
  GATEWAY_JWT_TYPE,
  INTERNAL_IDENTITY_HEADER,
  INTERNAL_JWT_TYPE,
  decodeIdentityAssertion,
  encodeIdentityAssertion,
  type IdentityClaims,
} from "@/lib/server/identityContext";

const gatewaySecret = "gateway-secret-with-at-least-thirty-two-bytes";
const internalSecret = "internal-secret-with-at-least-thirty-two-bytes";

function config(): BffConfig {
  return {
    backendApiUrl: "http://api:8000/api",
    backendApiKey: "reader-key-for-contract",
    allowedOrigins: new Set(["https://quest.example.test"]),
    maxRequestBytes: 1_024,
    upstreamTimeoutMs: 2_000,
    requireOriginForUnsafeMethods: true,
    tracingMode: "disabled",
    identityMode: "signed_jwt",
    gatewayIdentitySecret: gatewaySecret,
    internalIdentitySecret: internalSecret,
    gatewayIdentityIssuer: "polyuquest-gateway",
    gatewayIdentityAudience: "polyuquest-bff",
    internalIdentityIssuer: "polyuquest-bff",
    internalIdentityAudience: "polyuquest-api",
    identityMaxTtlSeconds: 120,
    identityClockSkewSeconds: 5,
  };
}

function claims(now = Math.floor(Date.now() / 1000)): IdentityClaims {
  return {
    v: 1,
    iss: "polyuquest-gateway",
    aud: "polyuquest-bff",
    sub: "alice",
    tenant_id: "tenant-a",
    groups: ["researchers"],
    iat: now,
    exp: now + 60,
    jti: "gateway-token-1",
  };
}

function request(token: string): Request {
  return new Request("https://quest.example.test/api/agent/runs", {
    method: "POST",
    headers: {
      Origin: "https://quest.example.test",
      "Content-Type": "application/json",
      "Idempotency-Key": "browser-0123456789abcdef",
      [GATEWAY_IDENTITY_HEADER]: token,
      [INTERNAL_IDENTITY_HEADER]: "browser-forged-internal-token",
    },
    body: JSON.stringify({ query: "how to apply" }),
  });
}

describe("trusted end-user identity propagation", () => {
  it("validates the gateway assertion and reissues a distinct API assertion", async () => {
    let upstream = new Headers();
    const fetchImpl = vi.fn<typeof fetch>(async (_url, init) => {
      upstream = new Headers(init?.headers);
      return Response.json({ run_id: "run-0123456789abcdef0123456789abcdef" }, { status: 202 });
    });
    const gatewayToken = encodeIdentityAssertion(claims(), gatewaySecret, GATEWAY_JWT_TYPE);
    const response = await proxyBffRequest(
      request(gatewayToken),
      { params: { path: ["agent", "runs"] } },
      { config: config(), fetchImpl }
    );

    expect(response.status).toBe(202);
    const internalToken = upstream.get(INTERNAL_IDENTITY_HEADER);
    expect(internalToken).not.toBeNull();
    expect(internalToken).not.toBe("browser-forged-internal-token");
    const identity = decodeIdentityAssertion(internalToken!, internalSecret, {
      type: INTERNAL_JWT_TYPE,
      issuer: "polyuquest-bff",
      audience: "polyuquest-api",
      maxTtlSeconds: 120,
      clockSkewSeconds: 5,
    });
    expect(identity.sub).toBe("alice");
    expect(identity.tenant_id).toBe("tenant-a");
    expect(upstream.has(GATEWAY_IDENTITY_HEADER)).toBe(false);
  });

  it("rejects missing, forged, expired, and cross-audience assertions pre-upstream", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const now = Math.floor(Date.now() / 1000);
    const candidates = [
      "",
      encodeIdentityAssertion(claims(now), "attacker-secret-with-at-least-thirty-two-bytes", GATEWAY_JWT_TYPE),
      encodeIdentityAssertion({ ...claims(now), iat: now - 200, exp: now - 100 }, gatewaySecret, GATEWAY_JWT_TYPE),
      encodeIdentityAssertion({ ...claims(now), aud: "another-bff" }, gatewaySecret, GATEWAY_JWT_TYPE),
    ];

    for (const candidate of candidates) {
      const response = await proxyBffRequest(
        request(candidate),
        { params: { path: ["agent", "runs"] } },
        { config: config(), fetchImpl }
      );
      expect(response.status).toBe(401);
      expect(await response.text()).toContain("end_user_identity_invalid");
    }
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
