# PolyUQuest Runtime Metrics 与容量基准 PRD

## 1. 迭代背景与定位

PolyUQuest 已具备持久化 Agent Run、独立 Worker、租约接管、运行健康与原子准入控制。上一迭代解决了突发请求可以无限进入 SQLite 队列的问题，但当前运行状态主要通过 JSON 运维接口和日志查看，尚未形成 Prometheus 可抓取、可聚合、可告警的机器合同；并发验证也停留在单元测试，没有可重复的容量基准报告。

本迭代面向企业内网/机构网站问答系统的运行控制面：把既有状态转换为无业务内容、低基数、受鉴权保护的 Prometheus 指标，并提供确定性的准入容量基准。它验证的是 API/SQLite 准入控制面的并发正确性与性能，不把外部 LLM、网页、Neo4j 或 Qdrant 的吞吐混入结论。

## 2. 修改前现状与主要问题

### 2.1 有数据，但缺少标准机器接口

- `/api/agent/runs/stats`、`/health` 和 `/api/telemetry/stats` 已能返回队列、租约和延迟数据；
- Prometheus 不能直接消费这些业务 JSON，运维侧还需要自行编写转换器；
- 不同接口分别查询 SQLite，缺少统一快照时刻，指标之间可能出现瞬时不一致。

### 2.2 直接高频抓取会放大存储压力

- `AgentRunStore.stats()` 每次都会查询运行状态与准入计数；
- 应用重试和租约接管当前通过逐条解析历史 `run_attempt_started` 事件计算；
- 如果 Prometheus 每 5～15 秒抓取一次，历史事件增长后会形成不必要的 O(N) 解析成本。

### 2.3 指标若携带动态标签会泄露业务并造成基数爆炸

- `run_id`、问题、URL、worker instance ID、principal 和错误原文都不应成为标签；
- 若按用户、网页或 Run 维度导出，时序数量会随业务量持续增长；
- `/metrics` 若公开，会泄露容量、故障和系统拓扑信息。

### 2.4 缺少可复现的容量证据

- 已有并发测试证明 `BEGIN IMMEDIATE` 下不会超收，但没有统一报告结构；
- 没有验证“突发达到上限 → 新请求被拒绝 → 释放容量 → 后续请求恢复接纳”的完整闭环；
- 没有 P50/P95/P99、吞吐和错误预算门槛，后续改动无法做回归比较。

## 3. 迭代目标与非目标

### 3.1 目标

1. 新增 operator 受保护的 Prometheus exposition 端点；
2. 指标只使用固定枚举标签，不包含任何业务正文或身份标识；
3. 通过线程安全 TTL 快照缓存限制 scrape 对 SQLite 的压力；
4. 将 attempt reason 聚合改为数据库计数器，避免每次扫描历史事件；
5. 新增确定性容量基准 CLI 和 JSON 报告；
6. 门禁验证准入上限、拒绝原因、容量释放和恢复接纳；
7. 文档明确指标语义、告警建议、基准边界和回滚方案。

### 3.2 非目标

- 本轮不引入 Prometheus Server、Grafana 或 Alertmanager 部署；
- 不宣称基准结果代表真实问答端到端 QPS；
- 不在指标中导出单个 Run、用户、问题、网页或 Worker 实例；
- 不在本轮实现跨进程 OpenTelemetry trace context；
- 不改变 Agent 检索、回答或增量入图算法。

## 4. 用户角色与业务场景

| 角色 | 需求 | 系统行为 |
| --- | --- | --- |
| SRE/平台工程师 | 发现队列拥塞和 Worker 失联 | 抓取固定指标并配置告警 |
| 后端工程师 | 判断 429 是容量保护还是程序故障 | 查看 rejection counter、utilization 和 worker gauge |
| 测试/发布负责人 | 比较改动前后的准入控制性能 | 运行同一 CLI 并保存 JSON artifact |
| 安全负责人 | 确认监控不泄露业务数据 | operator 鉴权、低基数标签和敏感词合同测试 |

典型过程：Prometheus 使用 operator API Key 抓取 `/api/metrics`；系统最多每个缓存周期查询一次状态；告警根据队列年龄、容量利用率、Worker 可用性与拒绝增量触发。发布前运行容量基准，若上限被突破、恢复失败或 P95 超过门槛则非零退出。

## 5. 功能需求

### 5.1 Prometheus 指标

端点返回官方 `prometheus_client.CONTENT_TYPE_LATEST`（当前依赖版本为 Prometheus text 1.0.0），不在业务代码中硬编码协议版本。指标前缀统一为 `polyuquest_`：

| 指标 | 类型 | 标签 | 语义 |
| --- | --- | --- | --- |
| `agent_runs` | gauge | `status` 固定枚举 | 各运行状态数量 |
| `agent_run_active` / `agent_run_waiting` | gauge | 无 | 当前 active/waiting 数量 |
| `agent_run_oldest_seconds` | gauge | `state=waiting|running` | 最老任务年龄 |
| `agent_run_admission_limit` | gauge | `kind=active|waiting` | 部署准入上限 |
| `agent_run_admission_utilization_ratio` | gauge | `kind=active|waiting` | 当前利用率 |
| `agent_run_admission_total` | counter | `outcome` 固定枚举 | 接纳、幂等重放与各类拒绝累计值 |
| `agent_run_attempts_total` | counter | `reason=all|application_retry|lease_reclaim` | 执行尝试与恢复累计值 |
| `worker_instances` | gauge | `capability`、`health` 固定集合 | 各能力的健康/不健康实例数 |
| `telemetry_runs` | gauge | `status=running|completed|error` | 观测窗口中的运行数 |
| `telemetry_duration_milliseconds` | gauge | `quantile=p50|p95|p99` | 已完成问答延迟分位数 |
| `metrics_snapshot_age_seconds` | gauge | 无 | 当前缓存快照年龄 |
| `metrics_snapshot_refresh_total` | counter | `outcome=success|error` | 快照刷新结果 |

未知数据库状态不能直接成为新标签值；应映射到预定义集合或忽略并记录安全错误计数。

### 5.2 快照缓存

- 默认 TTL 为 5 秒，可通过环境变量配置，取值 1～60 秒；
- 同一进程并发 scrape 只允许一个线程刷新；
- TTL 内返回同一不可变快照；
- 刷新失败时若存在旧快照，返回旧快照并增加 error counter；没有旧快照则返回 503；
- 响应设置 `Cache-Control: no-store`，避免代理缓存鉴权后的运行数据。

### 5.3 容量基准 CLI

命令 `agent-rag-capacity-benchmark` 使用临时 SQLite 数据库，执行四阶段：

1. `burst`：多线程同时提交唯一请求；
2. `reject`：确认 active/waiting 上限与 429 等价拒绝计数；
3. `release`：取消已接纳 Run 释放容量；
4. `recovery`：再次提交并确认恢复接纳。

CLI 支持 requests、concurrency、max-active、max-waiting、max-p95-ms、output 参数。报告包含 schema version、配置、每阶段数量、延迟 P50/P95/P99、吞吐、最终 store counters、断言和总体 status。报告不得包含问题、请求 JSON、idempotency key 或本机绝对路径。

## 6. 总体架构与数据流

```text
Prometheus
  -> X-API-Key(operator)
  -> FastAPI /api/metrics
  -> RuntimeMetricsSnapshotCache (single-flight + TTL)
       -> AgentRunStore.metrics_snapshot()  -- SQL aggregates/counters
       -> WorkerStatusStore.list()          -- bounded rows
       -> TelemetryStore.metrics_snapshot() -- fixed window aggregates
  -> prometheus_client CollectorRegistry
  -> text exposition

Capacity CLI
  -> temporary SQLite AgentRunStore
  -> ThreadPoolExecutor burst
  -> atomic admission
  -> cancel accepted runs
  -> recovery submissions
  -> contract assertions + JSON artifact + exit code
```

指标层只组合既有持久化状态，不调用 Neo4j、Qdrant、LLM 或网页抓取，避免监控反向影响业务依赖。

## 7. 数据模型与接口合同

### 7.1 attempt 聚合计数器

`agent_run_attempt_counters(metric PRIMARY KEY, value, updated_at)` 保存 `attempts`、`application_retry`、`lease_reclaim`。每次 claim 在同一个 `BEGIN IMMEDIATE` 事务内更新，因此任务状态、事件和指标不会部分提交。旧数据库在首次读取时可从事件账本回填一次，并写入 schema metadata 标记，之后只读计数器。

### 7.2 内部快照模型

快照只允许数值和固定枚举：

- `generated_at_monotonic`：只用于计算缓存年龄，不导出墙钟时间；
- `run_stats`：状态、队列年龄、准入累计值；
- `limits`：active/waiting 上限；
- `workers`：按 capability/health 聚合后的计数；
- `telemetry`：固定 24 小时窗口的状态和分位数；
- `refresh_counters`：进程内刷新成功/失败累计值。

### 7.3 HTTP 合同

- `GET /api/metrics`；
- reader key 返回 403，无凭据返回 401；
- operator/admin 返回 200 和 Prometheus Content-Type；
- 快照首次构建失败返回 503，detail 只包含稳定错误码，不包含异常原文或路径。

## 8. 安全、隐私与合规

1. 端点继承 operator RBAC 和安全审计中间件；
2. 指标禁止出现 query、answer、URL、HTML、run ID、idempotency key、principal、API Key、异常原文和本机路径；
3. 标签值只能来自源码中的固定集合；
4. 不将 `worker instance_id` 暴露为 label；
5. 测试使用 canary 敏感字符串写入请求，再断言 exposition 和报告均不包含；
6. Prometheus 配置中的原始 operator key 应使用 Secret 文件或受控 secret manager，不写入仓库；
7. 指标接口仅绑定内网或通过反向代理网络策略限制来源。

## 9. 性能、稳定性与可用性

- 默认 scrape TTL 5 秒，把 SQLite 聚合查询限制为单 API 实例每 5 秒最多一次；
- 单飞锁防止并发 scrape 形成缓存击穿；
- 所有 SQL 使用现有索引与聚合计数器，不逐条反序列化事件；
- Worker 列表有固定上限，并在内存中按固定 capability 聚合；
- 指标失败不影响查询、Worker 或 readiness；
- 容量基准默认规模应在开发机数秒内完成，不产生外部 API 费用；
- 基准延迟门槛可配置，CI 使用宽松但有意义的上界减少硬件噪声。

## 10. 测试与验收标准

### 10.1 单元与合同测试

1. attempt counter 在 initial、application retry、lease reclaim 下原子递增；
2. 老库事件回填只执行一次且保持幂等；
3. exposition 包含全部固定指标与 HELP/TYPE，不包含敏感 canary；
4. reader/缺失凭据不能访问，operator 可以访问；
5. TTL 内并发请求只刷新一次，过期后刷新；
6. 首次失败 503，有旧快照时 stale-on-error；
7. 标签集合不能被数据库中的未知值扩展；
8. CLI 报告 schema、分位数、断言与非零退出符合合同。

### 10.2 容量验收

- 任意时刻 `accepted <= max_active` 且 `waiting <= max_waiting`；
- 超额请求全部得到明确 active/waiting rejection；
- 取消后 active/waiting 归零；
- recovery 接纳数量等于可用容量；
- persisted accepted/rejected counters 与实际结果一致；
- P95 不超过 CLI 设定门槛；
- 重复运行结果在数量合同上完全一致。

### 10.3 回归验收

- 全量 Python、前端测试与 TypeScript 检查通过；
- Ruff、部署策略、生产 Compose 和 topology Compose 校验通过；
- GitHub Linux 门禁通过并保留容量报告 artifact。

### 10.4 本地实现证据（2026-08-15）

- Python 全量：248 passed，另有 64 subtests passed；
- metrics/capacity/admission/API 定向：27 passed；
- 前端：18 passed；TypeScript no-emit 通过；
- 本轮文件 Ruff、deployment policy、supply-chain policy、uv lock、workflow YAML、生产与拓扑
  Compose config 全部通过；
- 正式 CLI 以 32 requests、16 concurrency、active 12、waiting 8 运行：burst 接纳 8、waiting
  拒绝 24；active probe 拒绝 32；释放 12 后容量归零；恢复接纳 8；9 项合同全部通过；
- 本机控制面 P50/P95/P99 为 77.466/279.448/439.885 ms，吞吐 64.146 ops/s；该结果不代表
  LLM/网页/图向量端到端容量。

GitHub Linux workflow 和 artifact 信息在推送后回填，不用本地结果替代远程证据。

## 11. 配置、部署与运维

新增配置：

- `RUNTIME_METRICS_ENABLED=true`；
- `RUNTIME_METRICS_CACHE_TTL_SECONDS=5`；
- `RUNTIME_METRICS_TELEMETRY_WINDOW_HOURS=24`；
- `RUNTIME_METRICS_WORKER_LIMIT=100`。

Prometheus scrape 示例只记录配置形式，不写原始 key：目标为 API 内网地址 `/api/metrics`，通过受保护的请求头文件注入 `X-API-Key`。建议 15 秒 scrape interval，告警持续 2～5 个周期以减少瞬时噪声。

建议告警：

- active/waiting utilization ≥ warn ratio 持续 5 分钟；
- oldest waiting 超过 warning/critical 阈值；
- healthy `agent-run` worker 为 0；
- admission rejection counter 在 5 分钟窗口持续增长；
- lease reclaim 或 application retry 比例异常增加；
- metrics refresh error counter 增长。

## 12. 发布、回滚与风险控制

### 12.1 发布顺序

1. 先上线 attempt 持久化计数器和测试；
2. 开启 `/api/metrics`，但暂不配置告警动作；
3. 观察一周基线后设置 warning；
4. 再根据实际 P95、队列年龄和外部供应商额度校准 admission limit；
5. 容量基准报告作为后续控制面改动的 CI artifact。

### 12.2 回滚

- `RUNTIME_METRICS_ENABLED=false` 可关闭端点，不影响原 JSON 接口和业务链路；
- 新计数表为附加结构，旧代码可忽略；
- snapshot cache 或 exposition 异常时可回滚路由，不回滚 admission；
- 容量基准只是只读发布门禁，可从 workflow 移除而不改变运行时。

### 12.3 风险

- 多 API 副本各自有缓存，抓取时刻可能相差一个 TTL；指标来源仍是共享 SQLite，因此数量最终一致；
- SQLite 是单机共享卷基线，不代表跨节点队列能力；
- telemetry 24 小时分位数是窗口聚合 gauge，不是 Prometheus histogram；
- stale-on-error 可短时隐藏存储故障，因此必须同时暴露 snapshot age 和 refresh error。

## 13. 未来优化与改进方向

1. 引入 OpenTelemetry W3C trace context，贯通 Next.js BFF、FastAPI、Agent Run、Index Job、网页抓取、Neo4j、Qdrant 与外部模型调用；
2. 使用 Prometheus histogram 或 OTel exponential histogram 记录请求、排队和工具阶段延迟，而非仅导出窗口分位数；
3. 将 SQLite runtime ledger 迁移到 Postgres/Redis Streams/托管队列，支持跨节点原子准入与全局配额；
4. 基于租户、优先级和成本预算实现加权公平队列，但采用受控 tenant class 而不是原始 tenant ID 标签；
5. 增加真实拓扑负载测试，分别测量 API admission、Worker 执行、LLM provider、抓取、Neo4j 和 Qdrant 的饱和点；
6. 建立自动容量校准：根据 arrival rate、service time、P95 queue age 和供应商限额给出 max-active/max-waiting 建议，不自动写生产配置；
7. 提供 Grafana dashboard 与 recording rules，将原始指标转换为容量、可靠性、成本和知识新鲜度四类 SLO；
8. 加入异常检测和 burn-rate 告警，区分短时突发与持续不可用；
9. 对指标生成路径做基准和内存分析，数据量增长后将更多聚合转为写时计数；
10. 把控制面容量报告与版本、镜像 digest、运行环境信息关联，形成可审计的发布趋势，而不保存业务请求。
