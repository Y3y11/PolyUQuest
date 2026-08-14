# PolyUQuest 持久化 Agent Run 与 SSE 断线恢复 PRD

> 迭代 19 · 2026-08-14 · 状态：已完成本地与 GitHub Linux 门禁验证

## 1. 业务背景

PolyUQuest 已完成 Browser → Next.js BFF → FastAPI → Agent/Worker 的生产交付链。当前在线
探索可能包含知识库检索、网页发现、可信抓取、证据判断、回答生成和异步知识更新，单次运行
通常持续数秒到数分钟。企业内网中的反向代理重启、移动网络切换、浏览器刷新和前端发布都
可能中断 SSE；这些短暂连接故障不应该终止已经产生模型费用和网页抓取成本的业务任务。

因此本轮把“HTTP 请求”与“Agent Run”拆开：提交请求只负责创建持久化任务；独立 Worker
领取并执行；SSE 只是可以随时断开、重连和重放的观察通道。最终回答、引用、状态与审计事件
均可通过 `run_id` 再次获取。

## 2. 修改前现状与主要问题

### 2.1 Agent 生命周期绑定单次 SSE

`agent_router.py` 在请求内创建 `asyncio.Task`。StreamingResponse 结束或浏览器断开后，
`finally` 直接 `task.cancel()`。网络连接既是观察通道，也是任务所有者。

### 2.2 Telemetry 不能代替任务状态

现有 Telemetry SQLite 保存 run/span 指标，用于延迟、Token 和 SLO 聚合；它不保存可执行请求、
完整事件、最终响应、lease、重试或取消状态，不能作为调度真相源。

### 2.3 Index Outbox 只保护知识发布

探索后的 GraphPatch 已经支持 lease、heartbeat、retry 和 dead letter，但从查询理解到回答生成的
Agent 本身没有持久化任务。断线后可能丢掉本次回答，同时后台增量入库也可能尚未 enqueue。

### 2.4 前端无法从事件游标恢复

当前 SSE 没有 `id:` 字段，客户端解析器忽略 Last-Event-ID。短暂网络错误后只能重新提交整个
问题，造成重复 LLM、重复抓取和界面轨迹丢失；页面刷新后也不知道正在运行的 `run_id`。

### 2.5 取消语义不明确

“Stop generating”只 abort 浏览器 fetch。它可能取消服务器任务，也可能只关闭连接，无法查询
最终状态；恢复后无法区分用户取消、网络断开和 Worker 故障。

### 2.6 持久化内容扩大隐私面

可恢复执行必须保存 query/history、最终回答和事件 payload。与只保存 hash/计数的 Telemetry
不同，这些是业务内容，必须有明确目录权限、retention、API 授权和后续加密迁移边界。

## 3. 迭代目标

1. `POST /api/agent/runs` 在持久化后返回 202 和稳定 `run_id`；
2. Agent Run 由独立 Worker lease 执行，生产 API 不运行任务循环；
3. Worker/API 重启后，queued/retry/expired-running Run 能继续；
4. 每个事件分配单调 `event_id`，SSE 支持 `Last-Event-ID` 精确补发；
5. 连接断开不改变 Run 状态，浏览器自动重连且不重复显示已确认事件；
6. 页面刷新可从 sessionStorage 的 `run_id` 查询状态并重放完整轨迹；
7. 显式 cancel 进入持久化状态，Worker 协作停止，不再把网络 abort 当作业务取消；
8. `Idempotency-Key + request fingerprint` 防止重复提交；
9. 保留原 `/agent/query` 与 `/agent/query/stream` 兼容接口；
10. 形成 Store/Worker/API/BFF/前端/跨实例恢复的自动化证据。

## 4. 非目标

- 本轮不引入 PostgreSQL、Redis Streams、Kafka 或 Celery；
- 不宣称跨节点 exactly-once；执行采用 at-least-once，副作用依赖现有 Patch/Outbox 幂等；
- 不实现任意 Agent 步骤级 checkpoint，Worker lease 失效后从一次完整 attempt 重跑；
- 不实现多租户 run ownership；当前 reader 是单租户共享工作负载身份；
- 不把 Telemetry 表改造成任务队列；两者只通过相同 `run_id` 关联；
- 不在本轮实现业务内容字段级加密；生产依赖受限 volume/磁盘加密并设置短 retention；
- 不展示模型隐藏思维链，只重放已有可审计动作摘要、证据和判断。

## 5. 用户故事与状态机

### 5.1 正常运行

```text
Browser POST /agent/runs + Idempotency-Key
  -> API INSERT queued + submission event -> 202 run_id
  -> Agent Worker claim(lease) -> running/attempt=1
  -> append run_started/action/evidence/assessment...
  -> complete_with_event(done + response) -> completed
  -> Browser SSE reads id/event/data and renders answer
```

### 5.2 断线与恢复

```text
SSE disconnected after event 17
  -> Run remains running
  -> Browser reconnects with Last-Event-ID: 17
  -> API emits event_id > 17
  -> completed closes stream after final event
```

页面刷新后从 `sessionStorage` 读取 `run_id/query`，从游标 0 重放完整审计轨迹；若 Run 已终止，
事件流补发最终 `done/error/cancelled` 后关闭。GET snapshot 仍供状态页和运维客户端直接恢复最终结果。

### 5.3 状态机

```text
queued -> running -> completed
   |         |  \-> retry -> running
   |         |  \-> failed
   |         \----> cancelled
   \--------------> cancelled

running + lease expired -> running(new worker, attempt+1)
```

状态：`queued | running | retry | completed | failed | cancelled`。`completed/failed/cancelled`
为终态。cancel queued/retry 可立即终止；cancel running 先设置 `cancel_requested_at`，由当前 Worker
观察并结束。

## 6. 技术方案与选型

### 6.1 独立 Agent Run Store

新增独立 SQLite WAL 文件 `AGENT_RUN_STORE_PATH`，避免与 Observation/Patch 大表互相耦合。

`agent_runs` 至少保存：

- `run_id`、`idempotency_key`、`request_fingerprint`；
- `request_json`、`result_json`；
- `status`、`attempts`、`max_attempts`、`available_at`；
- `lease_until`、`worker_id`、`cancel_requested_at`；
- `last_error_code`、有界 `last_error`；
- created/updated/started/completed timestamps。

`agent_run_events` 保存：

- 全局自增 `event_id`、`run_id`、`attempt`；
- `event_type`、`payload_json`、`created_at`；
- `(run_id, event_id)` 索引，删除 Run 时级联删除事件。

SQLite 适合当前单机 Compose：API 与 Worker 通过同一 `runtime_data` volume 访问 WAL。吞吐、多副本
写竞争或跨主机部署后迁移 PostgreSQL transaction table + Redis Streams，不改变 HTTP/Event 契约。

### 6.2 幂等提交

客户端生成 URL-safe `Idempotency-Key`。Store 对 key 建唯一约束，并对规范化 request JSON 求
SHA-256：

- key 不存在：创建新 Run；
- key 存在且 fingerprint 相同：返回原 Run，`created=false`；
- key 存在但 fingerprint 不同：409，阻止误复用。

BFF 只转发通过长度/字符校验的 `Idempotency-Key`，不转发浏览器自带 service auth/request ID。

### 6.3 Lease 与 at-least-once Worker

AgentRunWorker 进入现有 standalone Worker `AsyncExitStack`，与 Index/Freshness loop 独立。claim 使用
`BEGIN IMMEDIATE + compare-and-set`：选择 queued/retry 或 lease 已过期的 running Run，增加 attempt，
写入 worker_id/lease。执行期间周期 heartbeat；lease 丢失的旧 Worker不能写终态。

Worker crash 后新实例从完整 request 重跑。探索写路径已由 content hash、Patch ID、Outbox dedupe 和
read-after-write 保护，因此允许 at-least-once；本轮不伪装步骤级 exactly-once。

### 6.4 事件与终态原子边界

普通事件即时 append。Agent 内部 `done` 暂存在 Worker 内存；Agent 返回后 Store 在同一 SQLite
事务中写 `done` event、`result_json` 和 completed 状态，避免“客户端看到 done、任务仍 running”。

异常写有界 `error` event 后进入 retry/failed。每个 attempt 先写 `run_attempt_started`；旧 attempt
事件不删除，保留审计。前端遇到更高 attempt 时重置临时活动视图或标记重试，不把隐藏思维链暴露。

### 6.5 取消与关闭

Cancel API 持久化 `cancel_requested_at`：

- queued/retry：原子改 cancelled 并写 `cancelled` event；
- running：写 `cancel_requested` event；Worker monitor 取消 asyncio Agent task并写终态；
- terminal：幂等返回当前状态。

Python `to_thread` 不能强杀已开始的同步调用，所以 cancel 是 best-effort cooperative；任何迟到副作用
仍由既有幂等发布机制保护。Worker shutdown 先 stop intake，再按 grace period 等当前 Run；强杀后由
lease 接管。

### 6.6 内容保护与 retention

Run Store 会保存 query/history/answer/evidence，目录必须只对应用 UID/GID 和备份账号可读；API 仅
reader 角色访问，Security Audit 仍只记录 route template，不复制内容。默认 terminal Run 保留 7 天，
failed Run 保留用于排障；Worker maintenance 分批级联清理。生产 volume 应启用宿主机/云盘加密。

## 7. API、SSE 与 BFF 合同

### 7.1 API

- `POST /api/agent/runs`：header `Idempotency-Key`，body `AgentQueryRequest`，返回 202；
- `GET /api/agent/runs/{run_id}`：状态、attempt、时间、terminal result/error；
- `GET /api/agent/runs/stats`：队列状态计数与 oldest waiting age；
- `GET /api/agent/runs/{run_id}/events`：SSE，可带 `Last-Event-ID` 或 `after`；
- `POST /api/agent/runs/{run_id}/cancel`：幂等请求取消。

全部需要 reader。run_id 使用服务生成格式；未知返回 404；key/fingerprint 冲突返回 409；事件游标
非法返回 422。共享 reader 暂不提供 per-user ownership，必须由外部 SSO 限制应用用户。

### 7.2 SSE

```text
id: 18
event: action
data: {"sequence":3,"action":"web.fetch_trusted_page",...}

```

无新事件时定期发送 `: keepalive`，不分配 event_id。读取顺序固定为 event_id ASC；终态且已发送全部
事件后关闭。客户端只在成功 dispatch 后推进游标。

### 7.3 BFF

BFF allowlist 新增四类 durable run route；GET events 转发受校验的 `Last-Event-ID`，POST submit
转发受校验的 `Idempotency-Key`。Cookie、Authorization、浏览器 X-API-Key、Forwarded 仍禁止。
错误正文继续清洗，SSE no-store/no-transform/no-buffering 和 cancel propagation 保持不变。

## 8. 前端交互与恢复

`agentQueryStreamAPI` 迁移为两阶段：

1. `createAgentRun()` 生成/复用 idempotency key并拿到 run_id；
2. `streamAgentRunEvents()` 以 fetch SSE 读取事件 ID，网络错误指数退避重连。

active run 的 `{run_id, query, lastEventId}` 写入 sessionStorage，不保存答案、service key 或后端地址。完成、失败、
取消后删除。页面 mount 时：

- 页面刷新：从 0 重放事件，重建完整动作与证据视图；
- 同一页面网络中断：携带最后确认的 `Last-Event-ID` 继续订阅；
- failed/cancelled：展示明确终态，不自动重提问题。

Stop 按钮调用 cancel API，再关闭本地 reader；“New chat”若有 active run先请求 cancel。UI 显示的是
“恢复连接/任务重试/已取消”等可审计状态，不显示模型内部 Chain-of-Thought。

## 9. 配置与生产拓扑

新增配置：

- `AGENT_RUN_STORE_PATH=data/runtime/agent_runs.sqlite3`；
- `AGENT_RUN_WORKER_ENABLED=true`（开发默认可启）；
- `AGENT_RUN_WORKER_POLL_SECONDS=0.25`；
- `AGENT_RUN_LEASE_SECONDS=120`；
- `AGENT_RUN_MAX_ATTEMPTS=2`；
- `AGENT_RUN_RETRY_BASE_SECONDS=2`；
- `AGENT_RUN_RETENTION_DAYS=7`；
- `AGENT_RUN_EVENT_POLL_SECONDS=0.25`；
- `AGENT_RUN_SSE_KEEPALIVE_SECONDS=15`。

production Compose：API 设置 `AGENT_RUN_WORKER_ENABLED=false`，Worker=true；两者共享 runtime_data。
如果 API production 错误启用 Agent Run Worker，启动校验失败，继续保持进程职责分离。

## 10. 测试与验收标准

| 边界 | 验收标准 |
|---|---|
| Store | 幂等 create、key 冲突、claim CAS、heartbeat、retry、lease reclaim、purge |
| 事件 | event_id 单调；after 精确补发；payload/attempt 保留 |
| 原子终态 | done event、result 与 completed 同事务可见 |
| API | 202/404/409/422、snapshot、cancel、SSE id/event/data/keepalive |
| 断线 | client disconnect 不 cancel Run；重新连接只接收游标后的事件 |
| Worker | 独立实例执行；expired lease 被新实例接管；旧 owner 不能覆盖终态 |
| 取消 | queued 立即取消；running 协作取消；terminal 幂等 |
| BFF | allowlist、Idempotency-Key/Last-Event-ID 校验与凭据隔离 |
| Frontend | 网络中断自动重连；页面刷新重放；Stop 调 cancel |
| 兼容 | 原 `/agent/query/stream` 行为与既有 E2E 不回退 |
| 隐私 | Audit 不保存内容；browser storage 只有 run_id/query；retention 可执行 |
| 回归 | Python 全量、Vitest/tsc、生产拓扑、BFF、供应链门禁全绿 |

### 10.1 验收结果（提交 `8cdf08e`）

- 本地 Python：219 passed，62 subtests passed；
- 本地 Durable Store/Worker/API：12 passed，包含租约过期后的跨 Worker attempt 接管；
- 前端：3 个 Vitest 文件、16 tests passed；TypeScript non-incremental check passed；
- deployment policy、目标 Ruff、diff check 和敏感 key pattern check passed；
- GitHub [Business E2E Gate #31812265978](https://github.com/Y3y11/PolyUQuest/actions/runs/31812265978)：success；
- GitHub [Production Topology E2E Gate #31812266055](https://github.com/Y3y11/PolyUQuest/actions/runs/31812266055)：success；
- GitHub [Browser BFF SSE Gate #31812266223](https://github.com/Y3y11/PolyUQuest/actions/runs/31812266223)：success；
- GitHub [Container Supply Chain Gate #31812265875](https://github.com/Y3y11/PolyUQuest/actions/runs/31812265875)：success。

本地 Docker daemon 未开启，因此没有把本地容器输出包装成证据；真实 Linux frontend build、BFF
容器交付、独立生产拓扑和供应链合同均由上述 GitHub runs 验证。

## 11. 文件级修改计划

新建：

- `src/agent_rag/runs/models.py`、`store.py`、`worker.py`；
- Run API schemas/routes 或扩展 `agent_router.py`；
- `tests/test_agent_run_store.py`、`test_agent_run_worker.py`、`test_agent_run_api.py`；
- 前端 durable stream tests；
- `docs/DURABLE_AGENT_RUN_PRD.md` 与 Runbook。

修改：

- `QueryDrivenAgent.run()`：允许注入外部 run_id；
- standalone Worker/API lifespan：按 process role 启动 Agent Run Worker；
- config、production Compose、deployment validator 与环境样例；
- BFF route/header contract；
- `frontend/lib/api.ts` 与 Ask 页面 session recovery/cancel；
- E2E/CI 合同、README、工程主叙事和本地迭代记录。

## 12. 风险、回滚与降级

- **SQLite 写竞争**：事务短小、busy_timeout/WAL、事件批量读取；写等待或多主机需求出现后迁移；
- **事件量增长**：payload 有界、terminal retention、分批清理，不保存 token-by-token 隐藏思维；
- **Worker 重放副作用**：依赖 Patch/Outbox 幂等，attempt 在事件中显式可见；
- **cancel 不可强杀线程**：best-effort + lease/幂等，文档不承诺瞬时停止外部供应商调用；
- **共享 reader 可查看所有 Run**：单租户边界明确；OIDC/tenant ownership 前不得多租户共用；
- **内容落盘**：受限 volume、短 retention、备份策略排除或加密 run store；后续 envelope encryption；
- **新接口故障**：旧 stream endpoint 保留；前端可临时 feature flag 回退，但生产不回退 raw key直连；
- **Worker 未启动**：Run 保持 queued，health/stats 暴露 oldest age，部署门禁必须验证消费。

回滚时前端恢复旧 `/agent/query/stream`，停止 Agent Run Worker；保留 SQLite 文件供审计，不删除 active
Run。已有知识索引、Index/Freshness Worker、Neo4j/Qdrant 不受影响。

## 13. 后续方向

1. PostgreSQL durable execution + `FOR UPDATE SKIP LOCKED`；
2. Redis Streams/Kafka event fan-out 与多区域 consumer；
3. OIDC subject/tenant ownership、行级隔离和 per-user cancel；
4. KMS envelope encryption、字段级密钥轮换与合规删除；
5. 步骤级 checkpoint、可恢复 Tool 调用与 compensating action；
6. SSE `Last-Event-ID` 扩展为 WebSocket/通知中心和历史 Run 页面；
7. per-tenant 并发、Token、网页数和费用预算；
8. OpenTelemetry trace link、queue age/lease reclaim/reconnect 指标和告警；
9. 多副本 chaos：API/Worker/ingress 滚动升级、网络分区与磁盘满演练。
