# Production Topology E2E Runbook

## 1. 门禁验证什么

该门禁从真实 HTTP/SSE 客户端出发，经过独立 API 与 Worker 容器，最终验证 Neo4j、Qdrant 和共享 SQLite WAL：

```text
401/403 鉴权
  → SSE 冷查询与临时证据回答
  → API enqueue pending Job
  → 独立 Worker claim + heartbeat
  → SIGKILL + heartbeat stale
  → lease 到期 + 新 Worker 接管
  → 热查询零抓取
  → Freshness v2 更新
  → Fact 退休/激活
  → 更新后热查询
```

模型边界和测试网页是确定性的；FastAPI、SSE、RBAC/审计、Outbox、Worker loops、SQLite、Neo4j、Qdrant、DOM Diff、Fact Version 和 Telemetry 均使用生产实现。

## 2. 与其他门禁的职责边界

| 门禁 | 证明内容 |
|---|---|
| Evaluation Gate | 冻结集上的回答质量、探索行为与回归 |
| Business E2E Gate | 单进程内真实双存储业务状态机 |
| Production Topology E2E | HTTP/SSE、鉴权、API/Worker 进程边界、强杀与 lease 接管 |
| Container/Supply-chain Gate | non-root/read-only、Profile、SBOM、CVE 与镜像预算 |

Topology E2E 不替代论文指标或真实模型评测。

## 3. 本地前置条件

- Docker Desktop daemon 已启动；
- Docker Compose v2；
- Python 3.12 与项目 `.venv`/`uv` 环境可用；
- 本机端口 18000、18080 未占用；
- 至少约 4 GB 可用内存和足够镜像构建空间。

不需要 DeepSeek、SiliconFlow 或 Qwen API Key。

## 4. 执行完整场景

PowerShell：

```powershell
$env:TOPOLOGY_E2E_TOKEN = "topology-local-001"
$env:TOPOLOGY_E2E_PROJECT = "polyuquest-topology-local-001"
uv run agent-rag-topology-e2e `
  --project $env:TOPOLOGY_E2E_PROJECT `
  --token $env:TOPOLOGY_E2E_TOKEN `
  --output data/runtime/topology-e2e/report.json `
  --logs data/runtime/topology-e2e/compose.log
```

首次执行会构建 remote backend image。已经构建时可增加 `--skip-build`。

## 5. 查看实时状态

API readiness：

```powershell
curl.exe http://127.0.0.1:18000/api/health/ready
```

Fixture 状态：

```powershell
curl.exe http://127.0.0.1:18080/__control/state
```

Worker 与 indexing 路由需要 operator/admin key。完整驱动器根据每次唯一 token 在宿主进程内派生非生产测试凭据，只把 SHA-256 记录传给 API 容器，不会把原始值写入 Compose、镜像、报告或日志。

Compose 日志：

```powershell
docker compose -f compose.topology-e2e.yml `
  -p $env:TOPOLOGY_E2E_PROJECT logs -f api worker fixture
```

## 6. 报告字段

默认报告：`data/runtime/topology-e2e/report.json`。

重点阶段：

- `compose_start`：API、Neo4j、Qdrant readiness；
- `authentication`：401/403/角色；
- `cold_sse_query`：事件顺序、request ID、回答与 pending Job；
- `worker_claim`：running Job 与首个 heartbeat；
- `worker_sigkill`：旧实例 stale、Job 保留；
- `worker_lease_takeover`：新实例、attempts 与 published version；
- `hot_sse_query`：零抓取复用；
- `freshness_update`：v2 Job、版本与 Fact 时态；
- `updated_hot_query`：新事实零抓取回答；
- `audit_and_telemetry`：安全审计与零模型费用。

报告和日志都位于被 Git 忽略的 `data/runtime/`。

## 7. 安全停止与清理

只清理本次明确 project，不要对默认或 production project 执行模糊清理：

```powershell
docker compose -f compose.topology-e2e.yml `
  -p $env:TOPOLOGY_E2E_PROJECT down -v --remove-orphans
```

该操作删除本次 E2E 的隔离容器、网络和 volume，不能恢复其中的测试数据；不会访问 production Compose project 或开发数据库。

## 8. 常见失败

### 8.1 API readiness 超时

检查 `api`、`neo4j`、`qdrant` 日志。API 在 test runtime 启动时会创建 Schema/Collections，然后才构建空 BM25 并 ready。

### 8.2 Job 未进入 running

确认 Worker 日志包含 `worker_process_ready`，API `/api/workers/status` 可见 healthy 实例，且 API/Worker 的 `AGENT_LEDGER_PATH` 都是 `/app/data/runtime/topology.sqlite3`。

### 8.3 SIGKILL 后旧 Worker 仍 healthy

测试配置 heartbeat=0.5s、max age=2s。若系统负载很高，先看报告中的 heartbeat age；不要通过删除 stale 记录掩盖问题。

### 8.4 新 Worker 不接管

确认 cold Job 的 `lease_until` 已过期、`total_attempts` 尚未达到 max attempts。Outbox `claim()` 会同时选择 pending/retry 和 lease 已过期的 running Job。

### 8.5 热查询再次抓网页

检查 Worker 是否 published、Qdrant Block 数、SSE assessment 与 Fixture request delta。不要把 fixture request 总数中的 Freshness 更新请求误算为热查询抓取。

### 8.6 Fact 没有 retired

检查 PageVersion 的 modified Block、Knowledge Delta 的 previous/retired facts，以及抽取关系的 `source_block_ids` 是否绑定最具体的 procedure Block。

## 9. GitHub Actions

工作流 `Production Topology E2E Gate` 在相关 API、Worker、Store、Agent、Compose 与配置变化时运行。它总是：

1. 收集 `report.json` 和 `compose.log`；
2. 执行唯一 project 的 `down -v --remove-orphans`；
3. 上传保留 14 天的 artifact。

失败时先查看报告的第一个 failed stage，再查看同阶段的 API/Worker 日志；不要仅重跑以掩盖确定性缺陷。
