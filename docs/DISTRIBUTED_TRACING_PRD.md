# PolyUQuest 跨进程分布式链路追踪 PRD

## 1. 迭代背景与定位

PolyUQuest 已具备 Next.js BFF、FastAPI、持久化 Agent Run、独立 Worker、自主网页探索、增量知识入图、Prometheus 指标和容量基准。Prometheus 适合回答“系统是否异常”，但无法定位某一次问答在浏览器网关、API 入队、队列等待、Agent 执行、网页工具或 Index Worker 的哪一段变慢或中断。

本迭代引入基于 OpenTelemetry 与 W3C Trace Context 的跨进程追踪。它不改变检索算法，也不把隐藏思维链暴露给用户；目标是为每次业务执行生成可关联、可采样、可安全导出的工程诊断链路。

## 2. 修改前现状与主要问题

### 2.1 观测数据被进程和存储边界割裂

- BFF、FastAPI、Agent Run Worker 与 Index Worker 是不同执行边界；
- SQLite Run/Outbox 能保证任务耐久，却没有保存上游 trace context；
- API 返回 202 后，原 HTTP 上下文结束，Worker 无法继续原链路。

### 2.2 指标能发现异常，不能定位单次请求

- 队列年龄、重试数和阶段 P95 能说明总体趋势；
- 无法确定某个用户看到的慢请求对应哪个 Worker attempt；
- “回答已完成、入图失败”和“探索阶段失败”在总体错误率上容易混淆。

### 2.3 直接记录业务字段存在隐私与基数风险

问题正文、回答、URL、HTML、Run ID、用户标识、异常原文和凭据均不适合成为 span attribute。若未经约束地自动埋点，还可能把动态 URL、查询参数或供应商错误写入外部观测平台。

### 2.4 缺少可验证的传播合同

当前没有测试证明 trace context 能经过 BFF、API、SQLite、进程重启、Agent Worker、Index Outbox 和 Index Worker 保持一致，也没有 malformed context、关闭导出和隐私 canary 的回归合同。

## 3. 迭代目标与非目标

### 3.1 目标

1. 使用标准 `traceparent` 在 BFF、API 和耐久队列之间传播上下文；
2. Agent Run 与 Index Job 增量增加 trace 字段，并兼容旧 SQLite 数据库；
3. 生成固定名称的 producer、consumer、Agent 阶段和入图阶段 span；
4. 支持 `disabled`、`propagate`、`otlp` 三种运行模式；
5. 默认不向外部发送数据，仅显式配置 OTLP 后启用批量导出；
6. HTTP 响应只返回可用于客服/SRE 关联的 `trace_id`，不返回完整传播头；
7. 用跨进程、重启、非法输入、隐私和前端 BFF 测试证明合同；
8. 保持追踪 fail-open，观测后端故障不能阻断问答或入图。

### 3.2 非目标

- 不部署 Jaeger、Tempo、Grafana 或商业 APM；
- 不记录模型隐藏思维链、Prompt、问题、答案或网页正文；
- 不把 trace 当作认证、授权或幂等凭据；
- 不在本轮自动埋点所有第三方 SDK；
- 不改变 Agent 检索、证据判断或图更新算法。

## 4. 用户角色与业务场景

| 角色 | 典型问题 | 本轮能力 |
| --- | --- | --- |
| 用户/支持人员 | 某次问答为何长时间停在处理中 | 通过响应 `trace_id` 关联运行记录 |
| 后端工程师 | 时间花在排队、探索还是回答生成 | 查看固定阶段 span 与父子关系 |
| SRE | Worker 重启后链路是否断裂 | SQLite 中的 W3C context 恢复 consumer span |
| 算法工程师 | 某类工具阶段是否普遍变慢 | 在采样 trace 中比较安全的阶段耗时 |
| 安全负责人 | APM 是否泄露机构查询和网页内容 | 隐私 allowlist 与 canary 测试 |

## 5. 功能需求

### 5.1 传播模式

- `disabled`：不创建、不持久化、不导出 trace；
- `propagate`：创建和传播 trace，但不配置外部 exporter，适合本地与合同测试；
- `otlp`：使用 BatchSpanProcessor 向配置的 OTLP/HTTP endpoint 导出；
- 非法或不支持版本的 `traceparent` 被忽略并创建新根链路；
- 不持久化 `tracestate` 与 baggage，避免任意供应商元数据进入业务库。

### 5.2 BFF 行为

- Next.js 使用根目录 `instrumentation.ts` 注册服务端 OpenTelemetry；
- BFF 只向固定 backend origin 注入 trace context；
- 浏览器传入的原始 `traceparent` 不直接复制到上游；
- 没有活动 SDK span 时，`propagate` 模式仍生成合法 W3C context；
- 请求体、查询参数、API Key 和动态 URL 不进入自定义 attribute。

### 5.3 Agent Run 链路

- API 创建 `agent.run.submit` producer span；
- 在同一 span 内提取并持久化 `traceparent`；
- Worker claim 后创建 `agent.run.execute` consumer span；
- 重试 attempt 产生新的 consumer span，但继续属于同一 trace；
- Agent 现有阶段 telemetry 同步生成安全的 OTel child span；
- submission/snapshot 暴露 32 位十六进制 `trace_id` 或 `null`。

### 5.4 增量入图链路

- Outbox enqueue 捕获当前 Agent span context；
- Index Job 持久化独立 `traceparent`；
- Index Worker 创建 `knowledge.index.execute` consumer span；
- queue wait、publish 与验证阶段保持当前 telemetry，同时映射为 OTel child span；
- 去重命中保留首次任务的 context，避免幂等重放改写归属。

## 6. 总体架构与数据流

```text
Browser
  -> Next.js BFF server span
     -> traceparent
  -> FastAPI agent.run.submit [PRODUCER]
     -> SQLite agent_runs.traceparent
  -> Agent Run Worker agent.run.execute [CONSUMER]
     -> route / retrieve / explore / answer child spans
     -> IndexOutbox.enqueue captures current context
        -> SQLite index_jobs.traceparent
     -> HTTP answer/SSE + trace_id
  -> Index Worker knowledge.index.execute [CONSUMER]
     -> queue / graph publish / read-after-write child spans
  -> optional OTLP/HTTP collector
```

耐久存储是传播桥梁，而不是 trace 的数据仓库。完整 span 由 SDK/collector 管理；SQLite 仅保存恢复父上下文所需的受校验 `traceparent`。

## 7. 数据模型与接口合同

### 7.1 SQLite 增量字段

- `agent_runs.traceparent TEXT NOT NULL DEFAULT ''`；
- `index_jobs.traceparent TEXT NOT NULL DEFAULT ''`；
- 启动时通过 `PRAGMA table_info` 检测并 `ALTER TABLE`；
- 字段只接受规范的小写 W3C version 00 格式，最大 55 字符；
- 旧行为空字符串，Worker 自动创建新根或在 disabled 模式不追踪。

### 7.2 HTTP 合同

`POST /api/agent/runs` 的响应增加：

```json
{"trace_id":"0123456789abcdef0123456789abcdef"}
```

`GET /api/agent/runs/{run_id}` 同样返回 `trace_id`。不返回 span ID、采样位、OTLP endpoint 或存储的完整 header。

### 7.3 Span 命名和属性

允许的名称由代码枚举：`agent.run.submit`、`agent.run.execute`、`agent.stage`、`llm.chat`、`knowledge.index.execute`。属性仅允许布尔值、数值和固定枚举，例如 attempt、status、route mode、cache hit、计数和 token 数；动态标识与正文全部丢弃。

## 8. 安全、隐私与合规

1. 禁止 query、answer、prompt、URL、HTML、实体名、Run/Job/Patch ID、worker ID、principal、幂等键、API Key 和异常原文进入 span；
2. OTLP endpoint 与认证 header 仅从 Secret/环境变量读取；
3. `traceparent` 只用于相关性，不授予任何权限；
4. 不接受或持久化 baggage/tracestate；
5. exporter 失败只记录固定错误类别并 fail-open；
6. 采样率限制成本，生产环境不得默认 100% 采样；
7. 测试写入敏感 canary 后检查 span、响应和传播字段均不包含 canary。

## 9. 性能、稳定性与可用性

- `disabled` 模式应接近零额外开销；
- `propagate` 仅执行本地 context 与随机 ID 操作；
- `otlp` 使用批量后台导出，不在业务请求同步等待 collector；
- exporter 不可用、队列满或上下文非法时业务仍继续；
- context 字段随任务同事务持久化，不增加额外数据库往返；
- span 名称和属性基数固定，避免 APM 成本失控；
- SDK 在进程退出时按受控超时 flush/shutdown。

## 10. 测试与验收标准

1. BFF 只向 backend 注入合法 `traceparent`，且不复制浏览器伪造值；
2. API producer 与 Worker consumer trace ID 相同；
3. 关闭并重新打开 SQLite store 后 context 仍可恢复；
4. Agent 重试保持 trace ID，每次 attempt 使用不同 span ID；
5. Index Job 从 Agent child context 继续，Index Worker 属于相同 trace；
6. 旧数据库无字段时自动迁移且旧任务仍能执行；
7. malformed/全零 trace ID 或 span ID 被拒绝；
8. disabled 模式不写 trace 字段，propagate 模式不触发网络 exporter；
9. canary 不出现在 span attributes、响应或 exporter payload；
10. Python、前端、TypeScript、Ruff、Compose 与生产拓扑回归通过。

### 10.1 本地验收证据（2026-08-15）

- Python 全量：250 passed，另有 64 subtests passed；
- tracing/Run/API/Outbox 定向：29 passed；
- 前端 Vitest：20 passed；TypeScript `--noEmit --incremental false` passed；
- 本轮文件 Ruff、deployment policy、supply-chain policy 与 `git diff --check` passed；
- production 与 topology Compose config passed；
- 跨队列测试证明 producer → Agent consumer → Index consumer 共用 trace ID，且 SQLite 重开后上下文仍可恢复；
- 隐私 canary、非法/全零 traceparent、disabled 模式和 BFF 不复制浏览器 header 的合同均通过。

### 10.2 GitHub Linux 与容器验收证据（commit `195ddbd`）

- Production Topology E2E Gate：run `31861124729`，success；
- Browser BFF SSE Gate：run `31861124796`，success，证明 Next.js 14 instrumentation 可完成真实生产构建和同源流式代理；
- Container Supply Chain Gate：run `31861124828`，success，包含镜像构建、只读启动、runtime contract、SBOM 与漏洞门禁；
- Business E2E Gate：run `31861124776`，success；
- topology artifact `9240623745`，digest `sha256:bf3c4ee92b68bb37071b91bcc42d51e21e5f3b9b39c423ef428702f5f37bb6b8`；
- BFF artifact `9240624439`，digest `sha256:a783c11fb750408efd10596c265759887e02e8875baefe5b02dac8138a845703`；
- supply-chain artifact `9240655904`，digest `sha256:20c06e2377486ae74cc61ed2904c3ddb0e10922d76d63f44986885fde770e350`；
- business artifact `9240600615`，digest `sha256:83a9d3540ed5b62d521cf3d31f6fe5dae6d474c5f67417973f0224dfe759562c`。

## 11. 配置、部署与运维

- `OTEL_TRACING_MODE=disabled|propagate|otlp`；
- `OTEL_SERVICE_NAME=polyuquest-api|polyuquest-worker|polyuquest-bff`；
- `OTEL_TRACES_SAMPLER_ARG=0.1`；
- `OTEL_EXPORTER_OTLP_ENDPOINT`；
- `OTEL_EXPORTER_OTLP_HEADERS` 由部署 Secret 注入；
- API 与 Worker 可使用不同 service name，但必须发送到同一 collector；
- 本地默认 `propagate` 便于联调，生产示例默认 `disabled`，由运维显式开启。

## 12. 发布、回滚与风险控制

发布顺序：先上线兼容字段与 propagate 模式，再开启低比例采样 OTLP，观察 exporter 错误和资源占用后逐步扩大。回滚时将 mode 改为 `disabled` 即可；新增 SQLite 字段保留，不需要破坏性迁移。

主要风险包括自动 instrumentation 采集动态路由、collector 故障造成积压、跨租户 trace 关联和采样导致个别问题无 trace。控制方式分别是自定义 allowlist、批量 fail-open、认证边界不依赖 trace、以及对错误/慢请求使用后续 tail sampling。

## 13. 未来优化与改进方向

1. 引入 Tempo/Jaeger 并建立 trace-to-metrics、trace-to-logs 跳转；
2. 对慢请求、失败、重试和入图不一致实施 tail-based sampling；
3. 为 HTTP、LLM、Neo4j、Qdrant 和抓取器增加语义约定兼容的受控 instrumentation；
4. 将固定阶段延迟导出为 OTel exponential histogram，并与现有 Prometheus SLO 对齐；
5. 增加 collector 双出口、磁盘队列和租户级数据保留策略；
6. 在前端提供仅对 operator 可见的 trace ID 与阶段瀑布图；
7. 建立关键路径和非关键路径：回答完成不必等待入图，但入图失败必须在同一 trace 中可追踪；
8. 在生产拓扑门禁中加入真实 collector fixture，校验 OTLP payload、父子关系与 exporter 重试；
9. 用真实流量统计重新校准采样率、span 数预算和 APM 成本；
10. 当队列迁移到 Postgres/Redis Streams/托管消息系统时，沿用 W3C context 字段与 producer/consumer 合同。
