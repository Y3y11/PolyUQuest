# PolyUQuest Durable Agent Run 生产故障 E2E 与运行健康 PRD

> 迭代 20 · 2026-08-14 · 状态：本地实现与回归完成，等待 Linux 生产拓扑门禁

## 1. 业务背景

迭代 19 已将一次在线探索从浏览器长连接中拆出：浏览器创建持久化 Run，独立 Worker 执行，SSE
按事件游标观察。单元与集成测试证明 SQLite Store、lease、retry、cancel 和 Last-Event-ID 的局部合同，
现有 Linux 门禁也证明原在线知识闭环、生产拓扑、BFF 和镜像没有回归。

但企业场景真正关心的是一条组合事实：终端用户经生产 Next.js BFF 创建任务后，即使网络断开、Worker
容器在执行中被强杀、API 与 Worker 独立运行，任务仍应由新 Worker 接管，浏览器继续同一 run_id，最终
只收到一个完成结果。只有把 Browser/BFF、FastAPI、共享 Run Store、独立 Worker、SSE 游标与故障注入
放进同一场景，才能证明“可恢复 Agent”不是多个局部测试的推断。

同时，可恢复任务引入 queued/running/retry/lease reclaim 等运行状态。如果运维只能看 HTTP 200 和容器
存活，就无法判断 Worker 未消费、队列持续积压或反复接管。本轮同步补齐最小健康信号。

## 2. 修改前现状与主要问题

### 2.1 生产拓扑仍走兼容接口

`topology_driver.py` 的冷/热/更新查询均调用 `/api/agent/query/stream`。该接口的 Agent task 仍与 SSE
连接绑定，因此门禁成功不能证明 Durable Run API、Run Store 或事件重放。

### 2.2 BFF 与真实 Run Worker 从未处于同一 E2E

- Browser BFF Gate 使用受控 Node upstream，能证明 allowlist、secret、流转发和取消；
- Production Topology Gate 使用真实 FastAPI/Worker/Neo4j/Qdrant，但没有 Next.js BFF；
- 两者之间的 route、Origin、Idempotency-Key、Last-Event-ID 和共享 reader 身份没有组合证据。

### 2.3 Agent Run 的 SIGKILL 接管只在进程内测试

现有测试能让 Store lease 过期后被第二个 Worker 对象 claim，但没有杀死真实容器、等待 heartbeat stale、
重建新 PID 并从相同 SQLite volume 接管。

### 2.4 测试拓扑的进程职责不够明确

`compose.topology-e2e.yml` 的 API 已关闭 Index/Freshness Worker，却没有显式设置
`AGENT_RUN_WORKER_ENABLED=false`。test 环境不会触发 production fail-fast，因此 API 与 standalone Worker
可能同时消费 Durable Run，削弱“独立 Worker 执行”的证据。

### 2.5 Run stats 不能解释重试来源

当前 stats 只有状态计数和 oldest waiting age：

- 无 total/active/terminal；
- 无 attempts_total/retried_runs；
- 无法区分普通 application retry 与 expired lease reclaim；
- 无 Worker 是否具备 `agent-run` capability 的综合健康结果；
- 队列积压没有 warning/critical 阈值。

### 2.6 E2E 若无可控暂停会产生竞态

确定性 Agent 很快完成。Driver 观察到 running 再 SIGKILL 时，任务可能已 completed，形成偶发门禁。故障
注入需要只在 topology mode 生效、只延迟第一次 attempt、由独立 marker 去重的确定性暂停点。

## 3. 迭代目标

1. Production Topology Compose 加入生产 Next.js BFF，真实 Browser HTTP 路径使用同源 `/api`；
2. API 显式关闭 Agent Run Worker，standalone Worker 显式开启；
3. 通过 BFF 创建 durable Run，记录 202、run_id、idempotency key和 BFF request ID；
4. 读取首个持久事件后主动断开，证明 snapshot 仍 queued/running 且未 cancel；
5. Worker claim 后 SIGKILL，旧 heartbeat 变 stale，Run 保持 running/attempt=1；
6. lease 到期后启动新 Worker，同一 run_id 以 attempt=2 完成；
7. 使用 `Last-Event-ID` 经 BFF 恢复，事件严格递增、只出现一个 done；
8. 相同 key/相同请求重提返回 `created=false` 和原 run_id，不产生第二次任务；
9. 报告记录旧/新 Worker、event cursor、attempt、lease reclaim、BFF/API request IDs；
10. stats 暴露 total/active/terminal/attempts/retried/lease reclaim/queue age；
11. health 综合 standalone/in-process Worker capability 与积压阈值，异常返回 503；
12. 保留现有在线知识闭环与 Index Job SIGKILL 场景，避免为新门禁牺牲旧证据。

## 4. 非目标

- 不在本轮引入 Playwright；Driver 作为无 service key 的 Browser HTTP client，验证真实 Next/BFF bundle；
- 不把共享 reader key包装成终端用户 IAM；仍依赖企业 SSO/ingress；
- 不做跨主机 SQLite；Compose 服务共享单机 named volume；
- 不承诺 exactly-once；只验证同一 Durable Run 与幂等副作用；
- 不在健康端点返回 query、answer、exception 或 event payload；
- 不把 queue warning 直接接入 PagerDuty/Sentry；只提供稳定机器合同；
- 不删除旧 stream E2E；它继续覆盖兼容路径与在线知识闭环。

## 5. 用户故事与验收场景

```text
Browser client
  -> Next.js BFF POST /api/agent/runs + Origin + Idempotency-Key
  -> FastAPI inserts queued + event id=1
  -> Browser GET events, consumes id=1, disconnects
  -> snapshot: queued, cancel_requested=false
  -> standalone Worker A claim attempt=1, topology-only delay
  -> SIGKILL Worker A, heartbeat becomes stale
  -> Run remains running; SSE disconnect did not cancel it
  -> start Worker B, expired lease claim attempt=2
  -> deterministic hot RAG answer, atomic done/result/completed
  -> Browser reconnects with Last-Event-ID: 1
  -> receives only id>1, ordered attempts/actions/done
  -> same Idempotency-Key resubmit returns original completed Run
```

场景安排在现有 cold → Index Worker takeover → hot → freshness v2 → updated hot 之后。此时知识已入库，
Durable Run 是零网页抓取的稳定热查询，不改变 Fixture request count；故障焦点只落在 Agent Run runtime。

## 6. 技术方案与选型

### 6.1 在 Production Topology 中加入 BFF

复用生产 `frontend/Dockerfile` 和 Next Catch-all Route Handler，不建立第二套代理。frontend 同时加入可出站的
fixture network与 internal backend，`BACKEND_API_URL=http://api:8000/api`。reader raw key通过临时 host file
以 root:10001/0440 挂载；workflow 结束无论成功失败都安全删除。

Driver 仍以 admin key直连 loopback FastAPI 查询运维状态，因为生产 BFF 刻意不开放 operator/admin 路由；
终端用户路径的 create/snapshot/events/idempotent replay 全部必须经 BFF且不能发送 X-API-Key。

### 6.2 topology-only 单次 Agent delay

新增 `BUSINESS_E2E_AGENT_RUN_DELAY_SECONDS` 与独立 marker，仅允许 `BUSINESS_E2E_MODE=topology`。Runtime
用包装器在 delegate Agent 第一次 run 前异步等待并原子创建 marker：

- Worker 已完成 claim并写 `run_attempt_started`，Driver 可稳定观察 running；
- 第一次 Worker 被杀；
- marker 位于共享 volume，第二个 Worker不再等待；
- production/development disabled mode无法启用该注入。

### 6.3 claim reason 与指标

`run_attempt_started` payload 增加 `claim_reason`：

- `initial`：queued -> running；
- `application_retry`：retry -> running；
- `lease_reclaim`：expired running -> running。

Store stats从 Run 表与 attempt event汇总，不解析或输出业务内容：状态计数、total、active、terminal、
attempts_total、retried_runs、application_retries、lease_reclaims、oldest_waiting_seconds、
oldest_running_seconds。

### 6.4 运维健康合同

新增 operator-only `/api/agent/runs/health`：

- standalone Worker 最新 heartbeat healthy 且 capabilities 包含 agent-run，或开发 API 内进程 worker running；
- oldest waiting 达 warning阈值时 degraded，达 critical阈值或无 Worker 时 HTTP 503；
- 无 active Run 时仍要求 Worker available，防止部署后第一次请求才发现没有消费者；
- 返回状态、原因、worker instance和无内容 stats，不影响 `/health/ready` 的 API进程存活语义。

配置 `AGENT_RUN_QUEUE_WARN_SECONDS`、`AGENT_RUN_QUEUE_CRITICAL_SECONDS`，要求 0 < warning < critical。

## 7. API、事件与安全合同

- Durable 浏览器路由仍为 create/snapshot/events/cancel；
- BFF 不新增 operator route，health/stats 只允许受控运维客户端直连 FastAPI；
- Driver 的 BFF POST 必须带 exact Origin；GET events只带数字 Last-Event-ID；
- BFF response 必须有 X-BFF-Request-ID，后端 X-Request-ID单独关联；
- event ID严格递增，但它是全库序列，不要求单个 run从 1 开始；
- terminal result中的 run_id必须等于 submission run_id；
- idempotent replay不能创建新 telemetry root或新知识任务；
- report和日志不得保存 raw reader/admin key或 secret file内容。

## 8. 可观测性与报告

Topology artifact新增：

- durable_run、idempotency key hash（不保存 raw key）；
- first_event_id、reconnect_from、replayed_event_ids/types；
- attempts、lease_reclaims、old/new Worker instance；
- disconnect snapshot、killed snapshot、completed snapshot；
- BFF/API request IDs；
- fixture request delta；
- Run stats与health摘要。

验收不仅检查 status=success，还检查 report required check 名称，防止场景被跳过后门禁仍绿。

## 9. 配置与部署拓扑

Topology API：

```text
AGENT_RUN_WORKER_ENABLED=false
AGENT_RUN_STORE_PATH=/app/data/runtime/agent_runs.sqlite3
```

Topology Worker：

```text
AGENT_RUN_WORKER_ENABLED=true
AGENT_RUN_LEASE_SECONDS=2
AGENT_RUN_RETRY_BASE_SECONDS=0
BUSINESS_E2E_AGENT_RUN_DELAY_SECONDS=20
```

Frontend：生产构建、server-only BACKEND_API_URL、BFF secret file、exact allowed origin。Workflow 增加独立端口
和 secret cleanup；所有服务继续 non-root/read-only/cap-drop。

## 10. 测试与验收标准

| 边界 | 必须证明 |
|---|---|
| Config | delay只允许 topology；queue threshold顺序合法 |
| Store | claim_reason准确；stats区分 retry/reclaim且无内容 |
| Health | standalone/in-process available；missing/stale/backlog critical=503 |
| Compose | frontend存在；API false/Worker true；共享 path/volume；secret file |
| BFF | create/events真实转发；Origin、Idempotency、Last-Event-ID生效 |
| Disconnect | 读端关闭后 Run不 cancelled |
| SIGKILL | 旧 heartbeat stale；Run仍 durable running |
| Takeover | 新 Worker、同一 run、attempt=2、lease_reclaims>=1 |
| Replay | 事件 ID > cursor且递增；done唯一；result run_id一致 |
| Idempotency | replay created=false且同 run_id |
| Compatibility | 原 cold/hot/update与 Index Worker takeover仍通过 |
| Security | bundle无 key；artifact无 raw secret；运维端点operator-only |
| Regression | Python/Vitest/tsc/Ruff/deployment/四类 Linux门禁全绿 |

本地验收结果（2026-08-14）：

- Durable/Topology/Config 定向测试：37 passed；
- Python 全量回归：226 passed，63 subtests passed；
- 前端 Vitest：16 passed；TypeScript `--noEmit --incremental false`：通过；
- 本轮修改文件 Ruff：通过；部署策略校验：通过；
- production/topology 两份 Compose 均可完整展开；
- 本机 Docker daemon 未运行，因此真实容器 SIGKILL 场景交由 GitHub Linux 门禁完成；
- 全仓 Ruff 仍有 37 个本轮之外的历史问题，作为独立静态债务迭代处理，不混入故障恢复改造。

## 11. 文件级修改计划

已新建：

- `docs/DURABLE_AGENT_RUN_CHAOS_E2E_PRD.md`；
- 必要时拆分 durable SSE parser/driver helper及对应测试。

已修改：

- `compose.topology-e2e.yml`：frontend、secret、Agent Worker职责与短 lease；
- `.github/workflows/production-topology-e2e.yml`：secret生成/权限/清理、frontend build与 artifact；
- `config.py` / env templates：queue阈值与 topology delay；
- `e2e/topology_runtime.py`：单次异步 Agent delay；
- `e2e/topology_driver.py`：BFF durable disconnect/SIGKILL/replay场景；
- `runs/store.py`：claim reason与扩展 stats；
- `agent_router.py`：operator stats/health；
- topology/store/API/worker tests；
- Runbook、工程故事、README和本地迭代记录。

## 12. 风险、回滚与降级

- **门禁耗时增加**：复用同一拓扑和镜像，Durable阶段只做热查询；delay被 SIGKILL提前终止；
- **frontend build放大时间**：Docker layer cache复用 Browser Gate基础依赖；仍以真实交付证据优先；
- **host secret权限错误**：复用已验证 root:10001/0440模式，always cleanup限制在 RUNNER_TEMP；
- **端口冲突**：CI项目和端口固定但 concurrency按 branch取消旧 run，本地可env覆盖；
- **kill错 Worker阶段**：通过 run snapshot + heartbeat instance双条件确认，不按固定 sleep；
- **第二 Worker被 Index loop抢占**：Durable hot query无新 Job；现有 Job已完成；Agent loop独立轮询；
- **health误报**：warning不返回503，critical才阻断；阈值可配置；ready端点不耦合业务积压；
- **回滚**：移除 durable阶段和 frontend service不影响原 topology；stats字段保持向后兼容；旧接口保留。

## 13. 后续方向

1. 独立 Durable Chaos workflow并行多 Worker、API滚动升级与 ingress reset；
2. Prometheus/OpenTelemetry导出 queue age、claim reason、attempt、reconnect；
3. admission control、per-tenant并发/Token/网页预算与 429 Retry-After；
4. PostgreSQL `SKIP LOCKED` 后验证多主机网络分区与 fencing token；
5. 历史 Run UI、通知中心、后台完成提醒和可恢复会话；
6. KMS字段加密、tenant ownership与合规删除 E2E；
7. 磁盘满、SQLite busy、WAL损坏和备份恢复演练；
8. shadow/canary 中按 queue latency与失败预算自动回滚。
