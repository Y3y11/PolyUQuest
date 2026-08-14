# PolyUQuest

**PolyUQuest** is a structure-aware, graph-enhanced Retrieval-Augmented Generation (RAG) system for university websites. It combines a three-layer knowledge graph (WebPage → DOM Block → Entity) with hybrid dense/sparse retrieval and a confidence-aware query router to answer natural-language questions over a website's content.

This repository contains the **online serving stack**: a FastAPI backend and a Next.js frontend. The two share a Neo4j (graph) + Qdrant (vector) backing store.

---

## Architecture at a glance

```
┌────────────────┐     HTTP /api      ┌──────────────────────┐
│  Next.js UI    │ ─────────────────► │  FastAPI backend     │
│  (frontend/)   │ ◄───────────────── │  (src/agent_rag/)    │
└────────────────┘                    └──────────┬───────────┘
                                                  │
                                   ┌──────────────┴──────────────┐
                                   ▼                             ▼
                            ┌────────────┐               ┌──────────────┐
                            │   Neo4j    │               │    Qdrant    │
                            │  (graph)   │               │  (vectors)   │
                            └────────────┘               └──────────────┘
```

**Query path** (`src/agent_rag/retrieval/`):
- `router.py` — confidence-aware router picks a retrieval mode.
- `direct.py` — block-level ANN search (mode A).
- `navigation.py` — page-anchored search with link expansion (mode B).
- `reasoning.py` — entity + topic-keyword graph traversal (mode C).
- `hybrid.py` — combines modes when router confidence is low.

---

## Prerequisites

- **Python ≥ 3.11** — managed via [uv](https://docs.astral.sh/uv/) (recommended).
- **Node.js ≥ 18** + npm — for the frontend.
- **Docker** + Docker Compose — for Neo4j and Qdrant.
- An LLM API key and an embedding API key (see `.env.example`). The defaults use [SiliconFlow](https://siliconflow.cn/) for both the LLM and BGE-M3 embeddings, so no local GPU is required.

---

## Quick start

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in at least:
#   SILICONFLOW_API_KEY   (LLM + embeddings)
#   NEO4J_PASSWORD        (must match docker-compose.yml)
```

### 2. Start the backing stores

```bash
docker-compose up -d
# Neo4j browser → http://localhost:7474  (bolt on 7687)
# Qdrant REST   → http://localhost:6333
```

### 3. Run the backend (FastAPI)

```bash
# With uv (auto-manages Python 3.11 + .venv):
uv sync
uv run agent-rag-serve          # → http://localhost:8000

# Swagger docs at http://localhost:8000/docs
```

<details>
<summary>Without uv (plain pip)</summary>

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
agent-rag-serve
```
</details>

The default dependency set is the lightweight remote-inference profile. If
`EMBEDDING_PROVIDER=local`, install the explicit local ML capability instead:

```bash
uv sync --locked --extra local-ml
```

`FlagEmbedding` is not installed by either serving profile because the online
code does not import it; local BGE-M3 embedding uses Sentence Transformers.

### 4. Run the frontend (Next.js)

```bash
cd frontend
npm install
npm run dev                     # → http://localhost:3000
```

The frontend talks to the backend at `http://localhost:8000/api` by default.
To point it elsewhere, set `NEXT_PUBLIC_API_URL` before `npm run dev`:

```bash
NEXT_PUBLIC_API_URL=http://localhost:8000/api npm run dev
```

---

## Configuration

| Concern | File |
|---|---|
| API keys / DB password / CORS | `.env` (template in `.env.example`) |
| Runtime capability (`auto`, `remote`, `local-ml`) | `APP_RUNTIME_PROFILE` / `BACKEND_RUNTIME_PROFILE` |
| Per-stage LLM model + temperature | `configs/llm.yaml` |
| Router confidence cutoffs, retrieval thresholds | `configs/thresholds.yaml` |
| Query-driven Agent budgets / evidence / fetch limits | `configs/agent.yaml` |
| Deterministic business E2E contract | `configs/business_e2e.yaml` |
| Runtime telemetry retention and SLO targets | `configs/observability.yaml` |
| API authentication, RBAC keys, security audit retention | `.env` |
| Entity alias dictionary | `configs/aliases.yaml` |
| Connector site identity / seed labels / URL policy | `configs/crawl.yaml` |

**LLM provider** defaults to `siliconflow`; `deepseek` and `qwen` also work by
setting `LLM_PROVIDER` in `.env`. **Embeddings** default to BGE-M3 (1024-d) via
the SiliconFlow API.

### API security

Local development defaults to `APP_ENVIRONMENT=development` and
`API_AUTH_MODE=disabled`. Production refuses to start anonymously. Generate a
one-time random key and its hash-only configuration record with:

```bash
python -m agent_rag.security.cli --key-id service-admin --role admin
```

Store the raw value in the caller's secret manager, put only the generated
`key_id:role:sha256` record in `API_AUTH_KEYS`, set
`API_AUTH_MODE=api_key`, and send the raw value as `X-API-Key`. Roles are
hierarchical: reader can query and inspect the graph, operator can read
operational state and run a reconciliation scan, and admin can retry, refresh,
pause/resume, execute repairs, and inspect the security audit. Health probes
remain public.

Do not put a static service key in `NEXT_PUBLIC_*`, browser storage, or a
frontend bundle. A production browser deployment should use enterprise
SSO/session handling in a BFF or API gateway, which injects the service key on
the server side. See
[`docs/API_SECURITY_RBAC_AUDIT_PRD.md`](docs/API_SECURITY_RBAC_AUDIT_PRD.md).

---

## API endpoints

Once the backend is running, the main routes (all under `/api`) are:

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/query` | Ask a question; returns answer + retrieval trace |
| `POST` | `/api/query/stream` | Ask a question with SSE answer streaming |
| `POST` | `/api/agent/query` | Bounded search/expand/fetch Agent with evidence trace |
| `POST` | `/api/agent/query/stream` | Agent actions and final response over SSE |
| `GET`  | `/api/graph/stats` | Knowledge-graph node counts |
| `GET`  | `/api/indexing/jobs` | Async indexing jobs and status |
| `GET`  | `/api/indexing/quality/decisions` | Audited page-quality decisions |
| `GET`  | `/api/indexing/quality/stats` | Index/evidence-only/discard counts |
| `POST` | `/api/indexing/reconciliation/runs` | Read-only cross-store drift scan and repair plan |
| `POST` | `/api/indexing/reconciliation/runs/{run_id}/execute?confirm=true` | Execute confirmed idempotent repairs |
| `GET`  | `/api/indexing/reconciliation/stats` | Drift and repair audit metrics |
| `GET`  | `/api/freshness/targets` | Adaptive page lifecycle targets |
| `GET`  | `/api/freshness/stats` | Refresh schedule and staleness metrics |
| `GET`  | `/api/telemetry/runs` | Privacy-safe Agent/indexing/reconciliation runs |
| `GET`  | `/api/telemetry/runs/{run_id}` | Run detail with stage and LLM usage spans |
| `GET`  | `/api/telemetry/stats` | P50/P95/P99 latency, success and token aggregates |
| `GET`  | `/api/telemetry/slo` | Configured targets with pass/fail/insufficient-data status |
| `GET`  | `/api/security/whoami` | Authenticated workload identity and role |
| `GET`  | `/api/security/audit` | Admin-only body-free security audit |
| `GET`  | `/api/security/audit/stats` | Authorization outcome and dropped-write counts |
| `GET`  | `/api/health/live` | Process liveness without dependency access |
| `GET`  | `/api/health/ready` | Startup and core dependency readiness |
| `GET`  | `/api/health/dependencies` | Neo4j and Qdrant status |
| `GET`  | `/api/health` | Backward-compatible dependency health |

Full interactive documentation is at `http://localhost:8000/docs`.

The Agent endpoint preserves the original retrieval API. Web exploration is
restricted to the crawler domain allowlist. Fetched pages are written to the
durable graph through an audited Patch by default; set
`persist_discoveries=false` for an explicitly read-only request, or
`AGENT_ALLOW_PERSISTENCE=false` for a read-only deployment. Observation and
Patch recovery state is stored in `data/runtime/agent_ledger.sqlite3` by
default. Production startup disables Uvicorn reload; use `API_RELOAD=true` only
for local development. See
[`docs/AGENT_INCREMENTAL_KNOWLEDGE_PRD.md`](docs/AGENT_INCREMENTAL_KNOWLEDGE_PRD.md)
and [`docs/AGENT_RELIABILITY_OPTIMIZATION_PRD.md`](docs/AGENT_RELIABILITY_OPTIMIZATION_PRD.md)
for the write and recovery contracts. Cross-store governance is documented in
[`docs/CONSISTENCY_RECONCILIATION_PRD.md`](docs/CONSISTENCY_RECONCILIATION_PRD.md):
scans are read-only by default, repair execution requires explicit confirmation,
and ambiguous fact-history conflicts are routed to manual review rather than deleted.

By default, durable indexing is asynchronous. A generic page-quality gate first
separates current-answer evidence from long-term index value. Only `index`
decisions stage a Patch and enqueue a SQLite Outbox job; `evidence_only` pages
remain temporary and `discard` pages support neither answer nor index. The
in-process Index Worker publishes accepted Patches to Neo4j/Qdrant with
lease-based retries. Operational APIs:

- `GET /api/indexing/jobs` and `/api/indexing/jobs/{job_id}`;
- `GET /api/indexing/stats`;
- `POST /api/indexing/jobs/{job_id}/retry`.
- `GET /api/indexing/quality/decisions` and `/api/indexing/quality/stats`.

Set `AGENT_ASYNC_INDEXING=false` to fall back to synchronous publishing, or
`INDEX_WORKER_ENABLED=false` when running a separately managed worker.

Indexed pages are registered with an adaptive Freshness Worker. It uses
ETag/Last-Modified conditional requests, expands the validation interval for
stable pages, shortens it for changing or frequently accessed pages, and sends
changed snapshots back through the quality gate and asynchronous Outbox. A 304
updates `last_validated_at` without re-embedding. Operations are available under
`/api/freshness/*`; set `FRESHNESS_WORKER_ENABLED=false` for an externally
scheduled deployment.

Agent queries, asynchronous indexing attempts, and reconciliation scans share a
durable telemetry contract in the SQLite ledger. The root/parent IDs connect a
user query to the later indexing work it triggered. Raw questions, prompts,
answers, fetched page bodies, and API keys are not stored: runs retain only a
query hash, bounded counters, allowlisted identifiers, status, latency, and
logical versus billable LLM usage. Telemetry writes are fail-open so an
observability outage cannot break the answer path.

For repeatable release checks, freeze business scenarios as JSONL, bind them to
a versioned manifest, and validate the dataset before scoring. The checked-in
sample manifest is intentionally `draft`; it demonstrates the contract but is
not production business Gold:

```bash
python -m agent_rag.evaluation.cli validate \
  --manifest data/eval/agent_business_scenarios.sample.manifest.yaml \
  --output data/runtime/eval/dataset-validation.json

python -m agent_rag.evaluation.cli run \
  --manifest data/eval/agent_business_scenarios.sample.manifest.yaml \
  --api-url http://127.0.0.1:8000 \
  --variant candidate --output data/runtime/eval/candidate.json

python -m agent_rag.evaluation.cli compare \
  --baseline data/runtime/eval/baseline.json \
  --candidate data/runtime/eval/candidate.json

python -m agent_rag.evaluation.cli gate \
  --baseline data/runtime/eval/baseline.json \
  --candidate data/runtime/eval/candidate.json \
  --policy configs/release_gate.yaml \
  --output data/runtime/eval/gate-decision.json
```

Metrics without a reference fact/source are reported as `N/A`, not fabricated
as zero. Report comparison rejects different dataset snapshots or evaluator
contracts. The release gate distinguishes `pass`, `fail`, and
`insufficient_evidence`, checks absolute floors, regressions, cost ratios,
critical cases, and business slices, and emits stable exit codes for CI. The
deterministic GitHub Actions workflow uses synthetic fixtures and no LLM,
Neo4j, Qdrant, production secret, or external website. See
[`docs/END_TO_END_OBSERVABILITY_EVALUATION_PRD.md`](docs/END_TO_END_OBSERVABILITY_EVALUATION_PRD.md)
and
[`docs/EVALUATION_GOVERNANCE_RELEASE_GATE_PRD.md`](docs/EVALUATION_GOVERNANCE_RELEASE_GATE_PRD.md).

### Business end-to-end gate

The deterministic evaluation gate scores frozen responses; it deliberately does
not prove the live write path. A separate business E2E gate starts real Neo4j
and Qdrant services and executes the complete online knowledge loop:

```text
cold query → trusted HTTP fetch → temporary grounded answer → SQLite Outbox
→ injected partial Qdrant failure → restarted Index Worker recovery
→ hot query with zero fetch → ETag refresh → DOM-diff update
→ fact retirement/activation → idempotent Patch replay
```

Only the non-repeatable model boundary is deterministic. Agent orchestration,
page quality, SQLite ledgers, Cypher, Qdrant vectors, freshness, incremental
publication, fact temporality, and read-after-write checks use production code.
No external API key is required.

```bash
docker compose -f compose.e2e.yml up -d --wait
uv run agent-rag-business-e2e \
  --output data/runtime/business-e2e/report.json \
  --runtime-dir data/runtime/business-e2e
```

Local ports differ from the defaults; follow
[`docs/BUSINESS_E2E_GATE_RUNBOOK.md`](docs/BUSINESS_E2E_GATE_RUNBOOK.md) for the
required environment. The product contract and failure evidence schema are in
[`docs/BUSINESS_E2E_GATE_PRD.md`](docs/BUSINESS_E2E_GATE_PRD.md).

Changed pages are published with deterministic DOM Block Diff. Only modified,
added, or missing-vector blocks are embedded; structurally relocated blocks
reuse their existing vectors, and unchanged page metadata reuses the page
vector. Every Patch has a durable PageVersion audit record. Inspect it through:

- `GET /api/indexing/versions` and `/api/indexing/versions/{version_id}`;
- `GET /api/indexing/version-stats` for embedding savings and change ratios.

The same diff drives incremental knowledge enrichment. Only semantically changed
or added blocks are sent to the extraction model; relocated blocks migrate their
provenance without another LLM call. Current Entity/Relation state remains in
Neo4j/Qdrant, while SQLite keeps bitemporal fact versions and replayable
KnowledgeDelta records. Operations are available through:

- `GET /api/indexing/facts` and `/api/indexing/facts/{fact_key}/history`;
- `GET /api/indexing/knowledge-stats`.

Long-running extraction is protected by an Index Worker lease heartbeat, so a
second worker cannot reclaim the same job while the first still owns it.

Agent query analysis uses an open-domain `entity + qualifier + intent +
required_claim` contract. Institution names and seed labels live in connector
configuration; the core planner/evaluator does not contain PolyU department or
degree rules, so the same constraint checks can be reused for product versions,
policies, services, and other intranet entities.

### Production deployment baseline

The repository includes reproducible non-root backend/frontend images and a
single-host production Compose topology. API serving and the durable index /
freshness worker use the same application image but run as separate processes;
Neo4j and Qdrant remain on an internal network. Production configuration fails
fast on anonymous auth, a default graph password, reload mode, or missing
credentials for the selected remote model provider.

Production defaults to the `remote` backend profile: the full Agent, graph,
vector, crawler, indexing, and audit stack remains available, while local
Torch/Sentence Transformers are excluded. A `local-ml` build is available for
self-hosted embedding. The image contains an immutable profile marker, so a
deployment cannot enable local embedding merely by relabeling a remote image.

```bash
docker build --build-arg APP_RUNTIME_PROFILE=remote -t polyuquest-backend:remote .
docker build --build-arg APP_RUNTIME_PROFILE=local-ml -t polyuquest-backend:local-ml .
```

```bash
python scripts/validate_deployment.py
docker compose --env-file deploy/.env.production -f compose.production.yml config --quiet
docker compose --env-file deploy/.env.production -f compose.production.yml up -d
python scripts/deployment_ops.py --env-file deploy/.env.production verify
```

The guarded operations CLI performs checksum-backed cold backups and requires
an explicit destructive confirmation before restore. See
[`docs/PRODUCTION_DEPLOYMENT_BASELINE_PRD.md`](docs/PRODUCTION_DEPLOYMENT_BASELINE_PRD.md)
and
[`docs/PRODUCTION_DEPLOYMENT_RUNBOOK.md`](docs/PRODUCTION_DEPLOYMENT_RUNBOOK.md).

### Container supply-chain gate

Container-affecting pushes and pull requests build both Linux images with
BuildKit, run the backend contract and frontend HTTP smoke as UID/GID
`10001:10001` on a read-only root filesystem, and generate CycloneDX SBOM plus
HIGH/CRITICAL vulnerability evidence. Fixable CRITICAL findings fail the gate.
Remote GitHub Actions and the Trivy release archive are pinned by immutable
commit/checksum policy in `configs/supply_chain.json`.

Run the dependency-free policy check locally with:

```bash
python scripts/validate_supply_chain.py validate
```

See [`docs/CONTAINER_SUPPLY_CHAIN_GATE_PRD.md`](docs/CONTAINER_SUPPLY_CHAIN_GATE_PRD.md)
and [`docs/CONTAINER_SUPPLY_CHAIN_RUNBOOK.md`](docs/CONTAINER_SUPPLY_CHAIN_RUNBOOK.md)
for the evidence contract, vulnerability policy, upgrade procedure, and current
boundaries.

Runtime profile selection, local development migration, negative contract
checks, and the manual local-ml supply-chain gate are documented in
[`docs/RUNTIME_PROFILE_IMAGE_OPTIMIZATION_PRD.md`](docs/RUNTIME_PROFILE_IMAGE_OPTIMIZATION_PRD.md)
and
[`docs/RUNTIME_PROFILE_IMAGE_OPTIMIZATION_RUNBOOK.md`](docs/RUNTIME_PROFILE_IMAGE_OPTIMIZATION_RUNBOOK.md).

---

## Note on data

This repository ships the **serving code** and the **PolyU benchmark dataset**.
The Neo4j + Qdrant stores start empty — you need to populate them with an indexed
corpus before live queries return results. The offline indexing pipeline is not
included here; the released dataset lives under `data/` —
see [`data/README.md`](data/README.md).

---

## License

This project is **dual-licensed**:

- **Code** (everything except `data/`) — [Apache License 2.0](LICENSE).
- **Dataset** (`data/`) — **research use only**; derived from public PolyU web
  pages with required `_source_urls` attribution. See
  [`data/DATA_LICENSE.md`](data/DATA_LICENSE.md).

## Project layout

```
PolyUQuest/
├── src/agent_rag/        # FastAPI backend + retrieval engine
│   ├── api/              # routes, schemas, app entry point
│   ├── retrieval/        # router + retrieval modes (direct/nav/reasoning/hybrid)
│   ├── storage/          # Neo4j + Qdrant adapters
│   ├── kg/               # knowledge-graph extraction & resolution
│   ├── knowledge/        # incremental fact delta + bitemporal history
│   ├── crawler/          # web crawler + URL filter
│   ├── e2e/              # deterministic real-store business gate
│   ├── html_processing/  # DOM → block-tree cleaner
│   └── llm/              # LLM client + Jinja prompt templates
├── frontend/             # Next.js 14 UI
│   ├── app/              # routes (chat, graph, compare)
│   ├── components/       # React components
│   └── lib/              # API client + state store
├── configs/              # YAML config (LLM, thresholds, aliases, crawl)
├── docker-compose.yml    # Neo4j + Qdrant
├── pyproject.toml        # Python deps (managed by uv)
└── .env.example          # environment template
```
