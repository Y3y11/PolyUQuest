# Trace Backend Operationalization Runbook

This runbook operates the optional OpenTelemetry Collector and Tempo profile
defined in `compose.production.yml`. The profile is a single-node baseline for
development, acceptance, and small controlled deployments. It is not a
multi-replica or disaster-recovery design.

## 1. Security boundary

- Collector and Tempo join only the private `backend` network and publish no
  production host ports.
- The application exports spans asynchronously. Collector or Tempo failure must
  not block Agent queries or indexing.
- Collector removes non-allowlisted resources, attributes, event data, status
  messages, and dynamic span names before storage.
- FastAPI applies a second allowlist and returns only a bounded waterfall.
- `GET /api/telemetry/traces/{trace_id}` requires an operator or admin key. The
  reader BFF must not expose this endpoint.
- Never put questions, answers, evidence text, URLs, credentials, model prompts,
  or hidden reasoning into span attributes.

## 2. Enable the profile

Set the following values in the protected production environment file:

```dotenv
OTEL_TRACING_MODE=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=0.1
TRACE_BACKEND_ENABLED=true
TRACE_BACKEND_URL=http://tempo:3200
TRACE_BACKEND_TIMEOUT_SECONDS=3
TRACE_BACKEND_MAX_RESPONSE_BYTES=2097152
TRACE_BACKEND_MAX_SPANS=500
```

Then validate and start the optional services before restarting application
processes with the new exporter settings:

```powershell
.venv\Scripts\python.exe scripts/validate_deployment.py
docker compose --env-file deploy/.env.production -f compose.production.yml config --quiet
docker compose --env-file deploy/.env.production -f compose.production.yml --profile observability up -d tempo otel-collector
docker compose --env-file deploy/.env.production -f compose.production.yml up -d api worker frontend
```

Do not add host port mappings to inspect Tempo. Query it through the protected
FastAPI endpoint or from an authorized container on the backend network.

## 3. Acceptance check

1. Submit a normal Agent Run and retain the response `trace_id`.
2. Query the exact ID with an operator key:

```powershell
$headers = @{ "X-API-Key" = "<operator-key>" }
Invoke-RestMethod -Headers $headers -Uri "http://127.0.0.1:8000/api/telemetry/traces/<trace-id>"
```

3. Verify `services`, `span_count`, `duration_ms`, and parent relationships are
   present, while question text, URLs, evidence, and exception messages are not.
4. Run a business query with Collector stopped. The query must still complete;
   the exact trace lookup should return a stable 503.

The repository's Linux gate performs a stronger canary test against real
Collector and Tempo containers and stores `data/runtime/tracing-e2e/report.json`
as CI evidence.

## 4. Response semantics

| Response | Meaning | Operator action |
|---|---|---|
| `200` | Privacy-safe waterfall available | Inspect stage duration and parentage |
| `404` | Invalid, unknown, expired, or not-yet-ingested trace | Check the exact ID, sampling, and retry briefly |
| `502 trace_backend_response_too_large` | Backend trace exceeded the byte budget | Investigate runaway span creation; do not raise limits blindly |
| `502 trace_backend_invalid_response` | Backend schema or JSON was not accepted | Check Tempo compatibility and recent upgrades |
| `503 trace_backend_disabled` | Query feature intentionally disabled | Enable only in a controlled operator environment |
| `503 trace_backend_unavailable` | Tempo timeout, connection failure, or 5xx | Check services, volume, resource pressure, and Collector export logs |

## 5. Routine operations

- Watch Collector memory and refused/dropped span counters, Tempo volume growth,
  query latency, exporter failures, and application sampling rate.
- Keep the 24-hour local retention aligned with disk capacity. Do not manually
  delete files from a live Tempo volume.
- Treat image upgrades as controlled changes: run config validation, the real
  OTLP privacy gate, and a rollback rehearsal before promotion.
- Keep tracing logs at normal informational levels. Never enable payload-debug
  exporters in an environment containing real queries.

## 6. Failure diagnosis

### Collector is unhealthy

Inspect container state and logs. Typical causes are invalid processor syntax,
memory pressure, or failure to reach Tempo. Keep application traffic running;
disable `OTEL_TRACING_MODE` if the exporter produces sustained resource pressure.

### Tempo is unhealthy

Check write permissions on `/var/tempo`, free disk, WAL recovery logs, and
retention pressure. A Tempo outage affects diagnostics, not Agent readiness.
Collector's bounded queue may drop spans after its retry window; this is
preferable to exhausting application resources.

### Trace remains 404

Confirm the application returned a sampled trace, propagation preserved the
same ID, Collector received spans, and the trace has completed. Head sampling
means some valid IDs are intentionally absent when the sampling ratio is below
one.

### Sensitive canary appears

Treat this as a release blocker. Disable OTLP export, preserve only access and
configuration audit evidence, rotate any exposed credential, correct both the
Collector and API allowlists, and rerun the real privacy gate before re-enabling.

## 7. Rollback

1. Set `TRACE_BACKEND_ENABLED=false` to close the operator query endpoint.
2. Set `OTEL_TRACING_MODE=disabled` and restart API/Worker/frontend to stop new
   exports.
3. Stop only the optional services:

```powershell
docker compose --env-file deploy/.env.production -f compose.production.yml --profile observability stop otel-collector tempo
```

Do not remove the Tempo volume as part of an application rollback. Volume
deletion is a separate destructive retention decision requiring authorization.

## 8. Production migration boundary

Before multi-node or regulated production, replace local Tempo storage with
managed or highly available object storage, add authenticated/TLS ingestion,
tenant isolation, durable Collector queues, backups, deletion auditing, and
capacity-tested retention. Keep the exact-ID API and privacy projection as the
application-facing contract even if the backend changes.
