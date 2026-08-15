# PolyUQuest Agent Runtime 容量治理与准入控制 PRD

> 迭代 21 · 2026-08-14～15 · 状态：实现、本地回归与 GitHub Linux 门禁全部完成

## 1. 业务背景

PolyUQuest 已从一次性 Web RAG 演进为可在线探索、增量入图、持久化执行并在 Worker 故障后恢复的企业知识 Agent。任务一旦进入 Durable Run，会消耗模型 Token、网页访问额度、CPU、向量检索和知识发布资源。迭代 20 解决的是“任务进入系统后不能因断流或 Worker 崩溃丢失”，但尚未回答“系统应该允许多少任务进入”。

企业内网发布、政策调整或集中报名期可能瞬间产生大量相似查询。如果 API 无上限接受任务，SQLite 中 queued/retry 会持续增长，等待时间、模型费用和网页压力会在故障恢复后集中释放。服务即使仍返回 202，也已经无法在可接受时间内兑现。因此容量治理必须在任务持久化之前执行，并向调用方提供明确、可重试、可监控的拒绝合同。

## 2. 修改前现状与主要问题

### 2.1 Durable Run 无条件接受新任务

`POST /api/agent/runs` 校验请求和 Idempotency-Key 后直接插入 queued。只要磁盘仍可写，active Run 数和等待队列均没有上限。

### 2.2 API 层先查后写会产生并发穿透

如果 API 先调用 stats 判断未满，再调用 Store.create，多个请求可同时读取同一个旧计数并全部通过。容量约束必须和 Run 插入共享同一个写事务。

### 2.3 预算只受公共 Schema 上限约束

公共 `AgentBudget` 允许最多 8 iterations、20 pages、300 seconds。这是防止异常输入的产品边界，不等于特定部署的成本策略。小型企业部署可能只允许每次 5 pages/120 seconds，但当前只能改代码中的 Schema。

### 2.4 幂等重试与新请求没有明确优先级

客户端可能已成功提交但丢失 202 响应。此时队列恰好满，如果相同 Idempotency-Key 被当作新请求执行容量判断并返回 429，客户端无法确认原任务是否存在。幂等查询必须先于准入拒绝。

### 2.5 拒绝没有持久指标

被拒绝的请求没有 Run 行，因此现有 Run stats 无法统计 capacity/budget rejection。只看当前队列无法区分“没有流量”和“大量流量已被入口挡住”。

### 2.6 过载没有标准恢复提示

缺少 HTTP 429、稳定错误 code 和 `Retry-After`。调用方只能把拒绝当一般 5xx 重试，可能形成同步重试风暴。

## 3. 迭代目标

1. 新 Run 的幂等检查、部署预算校验、容量判断、计数和插入在同一个 SQLite 写事务内完成；
2. 同 key/同 fingerprint 始终返回原 Run，不受当前容量和新预算策略影响；
3. 同 key/不同 fingerprint 始终返回 409，不被 429 遮蔽；
4. 支持全局 active 上限和 waiting（queued + retry）上限；
5. 支持每请求 iterations/pages/seconds 部署级上限，且不得超过公共 Schema 上限；
6. 容量拒绝返回 429、机器 code、无内容容量快照及整数 `Retry-After`；
7. 预算拒绝返回 422 和具体超限字段，不建议客户端原样重试；
8. 持久统计 accepted、idempotent replay、capacity rejection、waiting rejection 和 budget rejection；
9. health 暴露容量利用率，并在接近上限时 degraded、达到上限时 critical；
10. 并发测试证明上限不会因 TOCTOU 竞态被突破；
11. 配置进入 env/production Compose，并由启动校验阻止矛盾阈值；
12. 保持旧 Run、SSE replay、Worker lease、BFF 和在线知识闭环兼容。

## 4. 非目标

- 本轮不实现最终用户/租户级配额；当前 BFF reader 是共享 workload identity，缺乏可信 user/tenant ownership；
- 不实现任务优先级、抢占、加权公平队列或多个 Worker 并行 slot；
- 不预测精确 Token 或模型费用；只治理可在提交时确定的探索预算和 Run 数；
- 不自动扩容 Worker；health/metrics 为后续 HPA/告警提供合同；
- 不迁移 PostgreSQL；本轮保持单宿主 SQLite 原子性边界；
- 不让 429 创建失败 Run 或 SSE 事件，避免把未接受请求伪装成任务；
- 不对旧同步 `/agent/query/stream` 宣称同等容量保护；它仍是兼容路径；
- 不把 query/history/answer 写入拒绝日志或容量指标。

## 5. 用户故事与验收场景

### 5.1 正常提交

容量未满且预算符合策略时返回 202，`created=true`，accepted counter +1。

### 5.2 并发突发

当 `max_active=3` 时并发提交 20 个不同 key，最多 3 个新 Run 成功，其余返回容量拒绝；数据库 active 永远不超过 3。

### 5.3 等待队列保护

存在 running slot 但 queued/retry 已达到 waiting 上限时，新任务返回 waiting-capacity 429，防止无限排队。

### 5.4 响应丢失后的幂等确认

队列已满时，用已存在 key 和相同请求重提，仍返回原 run_id、`created=false`；不新增 accepted，不返回 429。

### 5.5 Key 冲突

队列已满时，用已存在 key 提交不同 request，仍返回 409，避免把客户端 bug 伪装成暂时过载。

### 5.6 超预算请求

请求 20 pages，而部署上限为 5 pages，返回 422 `agent_run_budget_exceeded`，指出 `max_pages` 的 requested/allowed；不创建 Run。

### 5.7 容量恢复

Run 完成/失败/取消进入 terminal 后，active 下降，新请求可立即被接受；无需手工重置 admission 状态。

## 6. 技术方案与选型

### 6.1 Admission Policy

新增不可变策略对象，由 Settings 构造：enabled、max_active、max_waiting、retry_after_seconds、warn_ratio，以及 iterations/pages/seconds ceilings。策略只包含部署规则，不保存用户内容。

### 6.2 原子 Store.create

`AgentRunStore.create` 执行顺序：

```text
BEGIN IMMEDIATE
  lookup idempotency key
    same fingerprint -> replay counter + return existing
    different fingerprint -> conflict
  validate deployment budget
    reject -> budget counter + commit + raise after transaction
  count active and waiting
    reject -> reason counter + commit + raise after transaction
  insert Run + run_queued + accepted counter
COMMIT
```

拒绝异常必须在事务提交计数之后抛出；如果在 context 内直接 raise，SQLite rollback 会让 rejection 指标丢失。

### 6.3 持久计数表

新增 `agent_run_admission_counters(metric PRIMARY KEY, value, updated_at)`，用 UPSERT 原子递增。它不记录 key、query、principal、IP 或请求正文。Stats 缺失 metric 时返回 0，保持旧数据库平滑迁移。

### 6.4 过载 HTTP 合同

容量拒绝：

```json
{
  "detail": {
    "code": "agent_run_capacity_exceeded",
    "reason": "active_limit",
    "retry_after_seconds": 5,
    "capacity": {"active": 100, "max_active": 100, "waiting": 80, "max_waiting": 80}
  }
}
```

同时返回 `Retry-After: 5`。Budget 拒绝为 422，code 为 `agent_run_budget_exceeded`，只包含字段、requested、allowed。

### 6.5 Health 利用率

计算 active/max_active 与 waiting/max_waiting。达到 100% 为 critical/503；达到 warn_ratio 为 degraded/200。与 Worker availability、queue age 原因合并，保留多原因数组。

## 7. 数据、API 与安全合同

- active 定义为 queued + running + retry；waiting 定义为 queued + retry；
- terminal 完成后自然释放容量；cancel queued/retry 同样立即释放；
- admission disabled 只关闭容量拒绝，部署预算仍执行，避免误把开关当无限成本授权；
- 429 detail 不包含 idempotency key、query、history、principal 或数据库路径；
- rejection counter 只记录聚合原因；
- stats/health 保持 operator-only；创建 Run 保持 reader；
- BFF 应透传后端 status、Retry-After 和安全 JSON，不在浏览器侧复制容量算法；
- Idempotency-Key fingerprint 语义保持不变；
- 不修改 AgentRunRecord schema，避免无必要数据库迁移。

## 8. 配置与默认值

```dotenv
AGENT_RUN_ADMISSION_ENABLED=true
AGENT_RUN_ADMISSION_MAX_ACTIVE=100
AGENT_RUN_ADMISSION_MAX_WAITING=80
AGENT_RUN_ADMISSION_RETRY_AFTER_SECONDS=5
AGENT_RUN_ADMISSION_WARN_RATIO=0.8
AGENT_RUN_BUDGET_MAX_ITERATIONS=5
AGENT_RUN_BUDGET_MAX_PAGES=10
AGENT_RUN_BUDGET_MAX_SECONDS=120
```

启动校验：active/waiting/retry-after/预算上限为正；waiting <= active；0 < warn_ratio < 1；预算不能超过 Pydantic 公共上限。开发和测试可显式传 policy 构造极小容量，不依赖全局 Settings。

## 9. 可观测性与运维

Stats 新增：

- `admission_accepted_total`；
- `admission_idempotent_replays_total`；
- `admission_rejected_active_total`；
- `admission_rejected_waiting_total`；
- `admission_rejected_budget_total`。

Health 新增 admission enabled、limits、utilization、warn_ratio。运维应同时看当前利用率和累计拒绝增量：利用率低但 rejection 激增可能表示突发流量已被成功削峰，而不是系统空闲。

## 10. 测试与验收标准

| 边界 | 必须证明 |
|---|---|
| Policy | 配置顺序、范围和预算 ceilings 合法 |
| Store normal | accepted counter、Run/event 一致 |
| Concurrency | 20 并发下不突破 max_active |
| Waiting | queued+retry 达限独立拒绝 |
| Idempotency | 满载时同请求 replay 成功，冲突仍 409 |
| Budget | 每个超限字段返回 422 且不创建 Run |
| Counters | rejection 事务提交后可跨 Store 重建读取 |
| API | 429 detail + Retry-After；无敏感内容 |
| Recovery | terminal/cancel 后立即恢复接收 |
| Health | warning/degraded、full/503、多 capability Worker |
| BFF | 429/Retry-After 透明传递，不暴露 service key |
| Regression | Run worker/SSE/chaos、Python、Vitest、tsc、Compose、Linux gates 全绿 |

本地验收结果（2026-08-14）：

- Admission/Store/API/Config 定向测试：39 passed；
- 20 并发请求、`max_active=3`：3 accepted、17 rejected，数据库 active=3；
- Python 全量：239 passed，63 subtests passed；
- 前端 Vitest：18 passed；TypeScript 无增量类型检查：通过；
- 本轮修改文件 Ruff、deployment policy、production/topology Compose config：通过；
- 本机 Docker daemon 未运行，真实 BFF/Worker/Neo4j/Qdrant 组合回归交由 GitHub Linux 门禁。

远端验收结果（提交 `c3a28e6`，2026-08-15）：

- Production Topology E2E Gate `31858353715`：success；
- Linux topology/admission/API/worker 定向测试：32 passed；
- Topology Driver：`status=passed`、14 checks、129,525 ms；
- artifact `production-topology-e2e-c3a28e6b03494f30fc5ed0b3ed3fe4d129582412`，18,826 bytes；
- Business E2E Gate `31858353648`：success；
- Browser BFF SSE Gate `31858353650`：success；
- Container Supply Chain Gate `31858353740`：success；
- 四条门禁共同证明原知识闭环、生产 BFF、Durable chaos 和镜像合同在准入改造后无回归。

## 11. 文件级修改计划

已新建：

- `docs/AGENT_RUNTIME_ADMISSION_CONTROL_PRD.md`；
- `src/agent_rag/runs/admission.py`；
- admission 专项测试文件（如有必要）。

已修改：

- `runs/store.py`：原子准入与持久 counters；
- `api/routes/agent_router.py`：429/422 与 health utilization；
- `config.py`、env examples、production/topology Compose；
- Store/API/deployment/BFF contract tests；
- Durable Run Runbook、README、工程故事、本地迭代记录；
- 相关 GitHub workflow path/lint 覆盖。

## 12. 风险、回滚与降级

- **默认值误伤现有流量**：默认 100 active/80 waiting，远高于当前单 Worker MVP常态；上线前用真实 P95 调整；
- **SQLite 写锁增加**：count 与 insert 在短事务内，计数走 status index；不在事务内做网络或模型调用；
- **rejection counter 热点**：单机 SQLite 当前可接受；高吞吐迁移 Prometheus + PostgreSQL；
- **终态释放慢**：Worker 必须原子提交 terminal；已有 lease/retry/health 负责异常恢复；
- **重试风暴**：Retry-After 为下限，客户端仍应指数退避+jitter；
- **配置缩容**：active 已超过新上限时不取消既有任务，只拒绝新任务，直到自然恢复；
- **回滚**：设置 admission enabled=false 可关闭容量拒绝；保留 counters 表和预算策略，不破坏旧 Run；
- **BFF 缓存错误**：429 不得缓存；Route Handler保持动态代理。

## 13. 后续优化方向

1. 引入可信 OIDC subject/tenant 后实现 per-tenant active、waiting、Token 和网页预算；
2. 任务 priority、weighted fair queue、老化提升与管理员紧急通道；
3. Worker slot/concurrency semaphore 与 admission capacity 联动；
4. 根据历史 P95 服务时间动态计算 Retry-After，而非固定值；
5. 模型/搜索/目标域名 circuit breaker、bulkhead 与供应商独立预算；
6. Token/费用预估、执行中实际扣减与日/月预算账本；
7. PostgreSQL `SKIP LOCKED` + Redis rate limit，支持多 API/Worker 主机；
8. Prometheus/OpenTelemetry 指标、Grafana 容量面板和自动告警；
9. k6/Locust 阶梯压测、容量模型、SLO/error budget 与自动扩缩容；
10. shadow/canary 中按 rejection、queue P95、错误率和费用自动阻断发布。
