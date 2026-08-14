# PolyUQuest 生产拓扑 E2E 与 Worker 接管门禁 PRD

> 迭代 17 · 2026-08-14 · 状态：实施中

## 1. 业务背景

迭代 16 已用真实 Neo4j、Qdrant、SQLite 和生产 Agent/Worker 类证明“冷查询探索、增量入库、故障恢复、热查询复用、页面更新、事实时态、幂等重放”的业务闭环。但所有组件仍由一个 Python 场景进程直接组合，无法证明生产部署中的进程、网络、鉴权和流式协议边界。

企业内网或机构网站问答系统实际运行时，API 和后台 Worker 必须独立扩缩容：API 负责低延迟回答与写入 Outbox，Worker 负责耗时的图/向量发布和知识刷新。两者只通过共享持久化账本与数据库协作。如果 Worker 被 OOM、滚动发布或节点故障强制终止，API 不应丢失回答能力，任务租约到期后新 Worker 应自动接管。

本轮把这一生产承诺变成无外部模型费用、可在 GitHub Linux runner 重复执行的拓扑级 E2E 门禁。

## 2. 修改前现状与主要问题

### 2.1 业务 E2E 没有经过 HTTP/SSE

- 迭代 16 直接调用 `QueryDrivenAgent.run()`，没有验证 FastAPI 路由、Pydantic 请求、SSE framing 和客户端解析；
- 无法证明 `run_started → action/evidence/assessment → done` 的事件链在真实网络响应中成立；
- HTTP 401/403、API Key 角色和安全审计没有进入同一业务故事。

### 2.2 API 与 Worker 虽已分离配置，但没有运行证据

- `compose.production.yml` 已使用两个容器和共享 `runtime_data`；
- 现有测试不能证明 API enqueue 的 SQLite Job 会被另一个进程 claim；
- 单进程重建 Store 不等价于容器 PID 被 SIGKILL 后重新启动。

### 2.3 Worker 只有进程日志，没有可查询 readiness

- API 有 `/health/live`、`/health/ready`，独立 Worker 仅记录 `worker_process_ready`；
- Compose/Kubernetes 只能判断进程存在，无法区分 event loop 卡死、心跳过期和正常消费；
- API/运维端无法回答“当前哪个 Worker 实例健康、何时最后心跳、是否发生接管”。

### 2.4 默认构造器无法在无密钥 CI 中复用完整运行时

- Agent 路由直接 `QueryDrivenAgent()`，Worker 直接 `PublishPatchTool()` / `FreshnessWorker()`；
- 拓扑门禁若使用这些默认边界会访问远程 LLM/Embedding，带来密钥、费用和抖动；
- 不能通过修改生产 SSRF 或全局 monkeypatch 绕过，应使用显式、仅 test 环境可启用的 runtime provider。

### 2.5 缺少强杀后的租约接管证据

- Outbox 已支持 `running + lease_until <= now` 重新 claim；
- 当前只有存储单测，没有“任务已 running → SIGKILL → 心跳过期 → 新实例二次 claim → succeeded”的容器证据；
- 没有 artifact 关联旧/新 Worker instance、Job attempts、SSE run 和最终知识版本。

## 3. 迭代目标

1. 用独立 API、Worker、Fixture、Neo4j、Qdrant 容器运行完整业务链路；
2. 所有查询经 `/api/agent/query/stream`，验证 SSE 事件顺序和最终响应；
3. 验证无凭据 401、reader 访问 operator 接口 403、合法 reader/admin 请求成功并进入安全审计；
4. API 只 enqueue，Worker 跨进程消费同一 SQLite WAL；
5. 在 Job 已被 claim 后 SIGKILL Worker，等待 lease 过期，再启动新 Worker 自动接管；
6. 增加持久化 Worker heartbeat、运维查询接口和容器 health CLI；
7. 在恢复后验证热查询零抓取、页面 v2 刷新、旧 Fact 退休和新事实回答；
8. 输出机器可读 JSON artifact，并在失败时保留 Compose 日志。

## 4. 非目标

- 不用本轮替代论文正确性、覆盖率和忠实度评测；
- 不在 CI 调用 DeepSeek、SiliconFlow、Qwen 或本地大模型；
- 不把原始 API Key 写入镜像、Compose、日志或报告；
- 不放松生产 `fetch_httpx_document` 的 SSRF/private-address 防护；
- 不在共享开发数据库执行清库，Compose project 使用独立匿名/命名卷；
- 不验证浏览器视觉样式和 Next.js BFF；浏览器级交付作为后续独立迭代；
- 不模拟多副本并发吞吐和跨节点共享文件系统。

## 5. 用户故事与验收场景

### 5.1 鉴权与 SSE 冷查询

客户端先无凭据访问 reader 路由，必须得到 401；再用 reader key 调用 operator 路由，必须得到 403。合法 reader 通过 SSE 提交带唯一 token 的访问申请问题。事件流必须以 `run_started` 开始、包含 action/evidence/assessment，并以唯一 `done` 结束。最终 answered、抓取 1 页、产生 1 个 Job。

### 5.2 API/Worker 跨进程交接

冷查询期间 Worker 不启动，因此 API 只能把 Job 持久化为 pending。启动 Worker 后，Job 变为 running；Worker heartbeat 在 API 运维端可见且 healthy。

### 5.3 SIGKILL 与租约接管

test-only Publisher barrier 在 claim 后第一次发布前暂停。驱动器观察到 running 后对 Worker 容器发送 SIGKILL。旧实例心跳必须变 stale，Job 保持 running 而不是丢失。lease 到期后重启 Worker，新 instance ID 必须出现并自动把同一 Job 完成为 succeeded/published，`total_attempts >= 2`。

### 5.4 热查询与知识复用

发布后再次通过 SSE 提交相同问题。Agent 必须从真实 Qdrant/Neo4j 取证，`pages_fetched=0`，Fixture origin request 增量为 0，并且不新建重复 Job。

### 5.5 页面变化与事实更新

Fixture 控制面切到 v2，admin 将来源页面标记为立即刷新。独立 Worker 的 Freshness loop 使用 ETag 获取新页面、产生更新 Job，Index loop 完成局部发布。API 运维接口应显示 active Fact=1、retired Fact>=1；更新后 SSE 查询不抓网页且回答包含新审批方。

### 5.6 安全审计与证据制品

安全审计统计至少包含 unauthorized、forbidden 和 allowed。报告记录 SSE event types、HTTP request IDs、Job/Patch/Version、旧/新 Worker instances、lease 接管次数、Fixture 请求数和阶段耗时。

## 6. 技术方案

### 6.1 生产拓扑

```text
Host E2E Driver
  ├─ HTTP/SSE + X-API-Key ──> API container
  │                              ├─ SQLite WAL / runtime volume
  │                              ├─ Neo4j read
  │                              └─ Qdrant read
  ├─ docker kill/start ─────> Worker container
  │                              ├─ Outbox lease + heartbeat
  │                              ├─ Index/Freshness loops
  │                              └─ Neo4j/Qdrant write
  └─ control version ───────> Fixture container
```

API 与 Worker 使用同一 backend image，但入口分别为 `agent-rag-serve` 和 `agent-rag-worker`。两者共享 runtime volume，不共享进程内对象。

### 6.2 Runtime Provider

新增通用构造边界：

- `build_query_agent()`；
- `build_publish_patch_tool()`；
- `build_freshness_worker()`。

默认返回现有生产实现。只有 `BUSINESS_E2E_MODE=topology` 且 `APP_ENVIRONMENT=test` 时，延迟导入确定性实现。配置校验禁止在 development/production 误启用。

测试态仍使用真实 Search、Expand、Quality、Outbox、Store、Diff、Fact 和 Telemetry，只替换 LLM/Embedding/受控网页来源；生产 SSRF 函数不变。

### 6.3 Worker Heartbeat

在共享 agent ledger 新增 `worker_heartbeats`：

- instance_id、pid、capabilities、state；
- started_at、heartbeat_at、stopped_at；
- API 查询时按 `heartbeat_at` 与阈值计算 healthy，不篡改历史记录；
- 正常退出标记 stopped；SIGKILL 保留 running 但变 stale；
- health CLI 根据最新实例返回 0/1，供容器探针使用。

### 6.4 一次性 Claim Barrier

测试态 Publisher 在共享 runtime 目录以原子 `O_EXCL` 创建 marker。第一个调用创建成功后暂停固定秒数；容器被杀后 marker 保留，新 Worker 不再暂停。该行为只存在于 E2E provider，生产 Publisher 无条件不读取 marker。

### 6.5 Fixture Service 与 SSRF 边界

Fixture 容器提供：

- 版本化 HTML、ETag/304、请求计数；
- `/__control/state` 与版本切换；
- canonical URL 仍为 `http://e2e.test/...`；
- test-only fetch adapter 将 canonical URL 映射到 Docker 内部 origin。

生产 fetcher 继续拒绝 private/loopback DNS，不增加关闭开关。

### 6.6 Host Driver

Python driver 负责 Compose build/up、HTTP/SSE、状态轮询、SIGKILL/restart 和原子报告。每次使用唯一 Compose project 与 volume，成功/失败均保留 report；CI 的 always steps 收集日志并销毁隔离资源。

## 7. 数据与接口合同

新增接口：

- `GET /api/workers/status?limit=N`：operator，返回历史实例及 healthy；
- `GET /api/workers/health`：operator，返回最新实例或 503；
- `agent-rag-worker-health --max-age-seconds N`：容器探针；
- `agent-rag-topology-e2e`：Host 驱动器；
- `agent-rag-e2e-fixture`：Fixture 服务。

报告沿用 `BusinessE2EReport`，新增 topology stage metrics，不保存 raw API key、完整 HTML 或 Authorization header。

## 8. CI 与 Compose 设计

新增 `compose.topology-e2e.yml` 和 `Production Topology E2E Gate` workflow：

1. 锁定 checkout/setup-uv/upload-artifact Action SHA；
2. 执行合同测试、Ruff、部署与供应链 validator；
3. 构建一次 remote backend image；
4. 先启动 Neo4j/Qdrant/Fixture/API，不启动 Worker；
5. Driver 通过 SSE 创建 pending Job；
6. 启动 Worker、观察 running/healthy、SIGKILL、等待 stale、重启；
7. 验证 recovery、hot query、refresh/update、Fact 时态和审计；
8. `if: always()` 上传 report 与 Compose logs，并 `down -v --remove-orphans` 清理唯一 project。

## 9. 验收标准

| 边界 | 标准 |
|---|---|
| API readiness | 真实 Neo4j/Qdrant、BM25/Embedding/startup 均 ready |
| 认证 | missing=401；reader→operator=403；合法角色成功 |
| SSE | run_started 首个、done 唯一且最后；包含 action/evidence/assessment |
| 冷查询 | answered；1 fetch；1 pending Job |
| 进程隔离 | API worker loops=false；独立 Worker heartbeat healthy |
| 强杀 | running 时 SIGKILL；旧 heartbeat stale；Job 未丢 |
| 接管 | 新 instance ID；同一 Job succeeded；total_attempts>=2 |
| 热查询 | 0 fetch；origin delta=0；无重复 Job |
| 页面更新 | Freshness 新 Job succeeded；content hash 改变 |
| Fact 时态 | active=1；retired>=1；回答含新事实 |
| 安全审计 | unauthorized/forbidden/allowed 均存在 |
| 外部模型 | LLM/Reranker 调用为 0 |
| 制品 | report/logs 失败时也上传 |

## 10. 文件级修改计划

新建：

- `docs/PRODUCTION_TOPOLOGY_E2E_PRD.md`；
- `docs/PRODUCTION_TOPOLOGY_E2E_RUNBOOK.md`；
- `compose.topology-e2e.yml`；
- `.github/workflows/production-topology-e2e.yml`；
- `src/agent_rag/runtime.py`；
- `src/agent_rag/e2e/topology_runtime.py`；
- `src/agent_rag/e2e/fixture_app.py`；
- `src/agent_rag/e2e/topology_driver.py`；
- `src/agent_rag/workers/status.py`；
- `src/agent_rag/api/routes/worker_router.py`；
- `tests/test_topology_e2e_contract.py`；
- `tests/test_worker_status.py`。

修改：

- `config.py`：test-only mode、fixture、barrier、heartbeat 配置与 fail-fast；
- `agent_router.py` / `indexing.worker.py` / `freshness.worker.py`：使用 runtime provider；
- `workers/main.py`：注册、心跳与正常停止；
- `api/main.py` / routes：暴露运维状态；
- `pyproject.toml`：新增 CLI；
- `README.md`：生产拓扑门禁和门禁分层；
- 本地迭代文档：记录缺陷、修复与远端证据。

## 11. 风险与应对

- **镜像构建耗时**：复用 remote profile、BuildKit cache，同一 workflow 只构建一次；
- **SIGKILL 时序抖动**：一次性 barrier 保证 Job 已 claim 且尚未发布；
- **SQLite 锁竞争**：沿用 WAL、短事务与 lease CAS，Driver 使用只读 HTTP 接口；
- **旧 heartbeat 误判**：healthy 按当前时间动态计算，新旧 instance 同时保留审计；
- **测试开关误入生产**：Settings 在非 test 环境直接拒绝启动；
- **Fixture 绕过 SSRF**：adapter 仅由 test provider 创建，生产代码没有 disable flag；
- **容器残留**：唯一 project name，CI always down -v；本地不操作 production project。

## 12. 回滚方案

Runtime provider 默认分支与当前构造行为一致；关闭 `BUSINESS_E2E_MODE` 即不加载 E2E 模块。Worker heartbeat 独立于任务状态，写入失败应记录告警但不阻断消费。若新门禁不稳定，可暂时关闭 workflow path trigger，不回退 Outbox、API/Worker 分离或现有业务 E2E。

## 13. 后续方向

1. 增加 Next.js BFF，API Key 仅保存在服务端，完成浏览器 → BFF → API → SSE 验证；
2. 将 SQLite runtime ledger 替换为 Postgres/Redis Streams 或托管 Queue，支持跨节点 Worker；
3. 增加多 Worker 抢占、幂等和水平扩缩容压力测试；
4. 注入 Neo4j/Qdrant 网络延迟、断连和容器重启；
5. 为 Worker heartbeat、queue lag、dead letter 建立 Prometheus/OpenTelemetry 指标和告警；
6. 增加租户 namespace、API key tenant claims 和 URL allowlist 隔离；
7. 把 Evaluation、Business E2E、Topology E2E、SBOM/provenance 绑定到同一 release candidate；
8. 形成最终系统设计、简历项目描述与面试问题文档。
