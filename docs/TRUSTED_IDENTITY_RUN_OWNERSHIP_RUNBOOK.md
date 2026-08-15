# Trusted Identity and Agent Run Ownership Runbook

## 1. Security boundary

- The enterprise gateway authenticates the user; PolyUQuest does not implement an IdP.
- The gateway JWT and BFF-to-API JWT use different secrets, issuers, audiences, and `typ` values.
- The BFF receives both secrets. The API receives only the internal secret. Workers receive neither.
- A workload API key is still required. End-user identity never replaces workload RBAC.
- Only durable Agent Run routes require end-user identity in this iteration.
- Never log JWTs, claims, raw idempotency keys, or user questions while diagnosing identity failures.

## 2. Provision secrets

Generate two independent values with at least 32 random bytes and store them outside Git:

```bash
openssl rand -base64 48 > /etc/polyuquest/secrets/gateway-identity.key
openssl rand -base64 48 > /etc/polyuquest/secrets/internal-identity.key
chown root:10001 /etc/polyuquest/secrets/gateway-identity.key /etc/polyuquest/secrets/internal-identity.key
chmod 0440 /etc/polyuquest/secrets/gateway-identity.key /etc/polyuquest/secrets/internal-identity.key
```

Do not copy the same value into both files. The BFF deliberately fails startup if the values match.

## 3. Configure the trusted gateway

After successful enterprise OIDC authentication, issue `X-PolyUQuest-Gateway-Identity` with:

- `alg=HS256`, `typ=polyuquest-gateway+jwt`;
- `iss=polyuquest-gateway`, `aud=polyuquest-bff`;
- `v=1`, stable opaque `sub`, stable `tenant_id`, bounded `groups`;
- integer `iat`, `exp` no more than 60 seconds later, and unique `jti`.

The gateway must strip any inbound header with the same name before inserting its own assertion.

## 4. Configure production

Set protected host paths:

```dotenv
BFF_GATEWAY_IDENTITY_SECRET_FILE=/etc/polyuquest/secrets/gateway-identity.key
INTERNAL_IDENTITY_SECRET_FILE=/etc/polyuquest/secrets/internal-identity.key
```

Compose mounts the gateway key only into the frontend and the internal key into frontend + API. Verify the worker has no identity secret mount.

## 5. Pre-deployment checks

```bash
python scripts/validate_deployment.py
docker compose -f compose.production.yml --env-file deploy/.env.production config --quiet
```

Back up `agent_runs.sqlite3`. On startup, verify `tenant_id`, `owner_subject`, and `idx_agent_runs_owner` exist. Existing rows should read `legacy-tenant/legacy-user`; do not reassign them automatically.

## 6. Canary acceptance

1. Create a durable Run as user A and retain its `run_id`.
2. Retry with the same idempotency key and body; confirm the same Run is returned.
3. Create as user B with the same browser idempotency key; confirm a different Run.
4. As user B, GET user A's snapshot, events, and cancel endpoint; all must return the same 404.
5. As user A, reconnect SSE and cancel; both must work.
6. Remove or corrupt the gateway assertion; BFF must return 401 without an API request.
7. Submit a browser-controlled `X-PolyUQuest-Identity`; confirm the API receives a newly signed BFF assertion instead.

## 7. Failure diagnosis

| Symptom | Likely cause | Safe action |
|---|---|---|
| BFF 503 at startup/request | missing, short, unreadable, or reused secret | check file path, owner, mode, and that the two digests differ; never print values |
| BFF 401 `end_user_identity_invalid` | gateway token missing, expired, wrong audience/type, clock drift | inspect gateway config and UTC clock; do not decode production token in logs |
| BFF 502 auth failure | internal issuer/audience/key mismatch or workload key failure | compare configured names and secret file digests on authorized hosts |
| Run 404 for owner | subject/tenant changed between requests or Run belongs to legacy owner | compare identity source configuration; do not bypass owner filter |
| same user creates duplicate Run | unstable subject/tenant or different raw idempotency key | fix gateway stable identifiers/client persistence |
| different users collide | scoped key migration missing | stop rollout, verify application version and database columns/index |

## 8. Safe digest comparison

On authorized hosts, compare only SHA-256 digests and do not paste output into tickets. The BFF internal digest must match the API internal digest; the gateway digest must differ from the internal digest.

## 9. Rotation

The MVP accepts one key per boundary, so rotation requires a coordinated short maintenance/canary window:

1. stop new durable submissions at the gateway;
2. deploy the new internal key to API and BFF together;
3. deploy the new gateway key to gateway and BFF together;
4. restart API/BFF, run the canary, then reopen submissions;
5. securely retire old files.

Future `kid` + dual-verification support should remove this coordination window.

## 10. Rollback

Do not set production identity mode to `disabled`. Restore the previous known-good gateway/BFF/API images and matching secret files. Keep the added SQLite columns and index; they are backward-compatible. Treat rollback to a version without owner enforcement as a time-bounded security exception requiring access restriction.

## 11. Incident response

- Gateway key leak: rotate gateway + BFF gateway key; internal key can remain if not exposed.
- Internal key leak: rotate BFF + API internal key; assume attackers could impersonate users to durable endpoints if they also possess a reader workload key.
- Both keys or BFF compromise: rotate both, rotate the BFF reader API key, invalidate gateway sessions, and inspect safe security audit metadata.
- Do not delete Run data during credential response unless retention/legal owners approve it.

## 12. Operational limits

- Short TTL limits but does not eliminate replay.
- This iteration isolates Runs, not Neo4j/Qdrant/BM25 knowledge.
- Legacy Runs remain under the legacy owner and are not automatically claimable.
- Symmetric keys are an MVP baseline; use JWKS/asymmetric verification for broader deployments.
