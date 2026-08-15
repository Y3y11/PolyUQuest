# PolyUQuest Trace Backend 落地与安全查询 PRD

## 1. 迭代背景与定位

上一迭代已经让 W3C Trace Context 穿过 Next.js BFF、FastAPI、Agent Run、独立 Worker、Index Outbox 与 Index Worker，并能选择性导出 OTLP。但“能够产生 trace”还不是一套可运营能力：仓库内没有 collector、存储后端、查询边界和真实 OTLP 门禁，运维仍无法凭 API 返回的 `trace_id` 找到阶段瀑布。

本迭代把 tracing 从代码埋点推进为可运行的观测数据面。PolyUQuest 提供一个默认关闭的 `observability` Compose profile：OpenTelemetry Collector 负责接收、限流、批处理与隐私清洗，Tempo 负责单节点存储与按 ID 查询；FastAPI 仅向 operator/admin 提供经过二次裁剪的安全瀑布接口。该 profile 是单机部署基线和验收环境，不宣称替代多节点对象存储或托管 APM。

## 2. 修改前现状与主要问题

### 2.1 OTLP 有出口但没有仓库内可运行接收端

- `OTEL_TRACING_MODE=otlp` 能创建 BatchSpanProcessor；
- endpoint 完全依赖外部环境，仓库无法证明 payload 真正到达；
- 当前门禁只用内存 exporter 验证 SDK 父子关系，没有覆盖 HTTP、Collector 和 Tempo。

### 2.2 `trace_id` 可返回但不可在产品边界内查询

- 用户或支持人员能拿到 32 位 `trace_id`；
- 运维需要进入第三方系统或直接调用 Tempo 原始 API；
- API RBAC、审计日志与故障语义无法覆盖这次查询。

### 2.3 原始 Tempo 响应不能直接暴露

- 自动 instrumentation 可能生成 URL、路由、异常事件和未知 resource/span attribute；
- 原始响应可能很大，包含动态 span/service 名称和供应商字段；
- 直接代理任意 TraceQL 会带来查询注入、成本放大和数据枚举风险。

### 2.4 观测组件可能反向影响业务可用性

- Collector/Tempo 启动失败不应阻塞问答 API 或 Worker；
- exporter 同步等待会放大请求延迟；
- 无内存限制、批处理和响应大小限制时，观测流量可能耗尽资源。

### 2.5 内置单机存储存在明确边界

本地文件系统 Tempo 适合单节点基线、开发和验收，但不具备跨节点高可用、对象存储耐久性和独立扩缩容能力。若不明确该边界，Compose 可运行容易被误解为企业级最终部署。

## 3. 迭代目标与非目标

### 3.1 目标

1. 新增可选的 Collector + Tempo 生产 Compose profile；
2. Collector 使用 memory limiter、fail-closed 隐私清洗和 batch；
3. Tempo 不发布宿主机端口，只能由 backend 网络访问；
4. 新增 operator-only `GET /api/telemetry/traces/{trace_id}`；
5. 把 Tempo OTLP JSON 解析为有上限、固定字段的阶段瀑布；
6. 拒绝非法 trace ID、任意搜索表达式和动态 backend URL；
7. backend disabled、404、超时、超限、非法 JSON 分别返回稳定安全语义；
8. 新增真实 Collector → Tempo → 查询 E2E 门禁和 JSON artifact；
9. 将第三方镜像版本、网络、只读和资源上限纳入部署策略；
10. 保持观测 profile 默认关闭、业务链路 fail-open。

### 3.2 非目标

- 本轮不部署 Grafana UI、Prometheus Server 或 Alertmanager；
- 不向 reader BFF 开放 operator trace 查询；
- 不开放 `/api/search`、TraceQL、tag 枚举或原始 Tempo response；
- 不实现多租户 Tempo、S3/GCS/Azure 对象存储或跨区高可用；
- 不把 trace 数据用于回答生成或 Agent 决策；
- 不记录问题、答案、网页正文、URL、凭据或模型思维链。

## 4. 用户角色与业务场景

| 角色 | 场景 | 系统行为 |
| --- | --- | --- |
| 支持人员 | 用户提供一次问答的 trace ID | operator 查询阶段瀑布，判断卡在排队、Agent 还是入图 |
| 后端工程师 | Worker 重试或进程接管 | 查看同一 trace 下不同 consumer attempt 的父子关系 |
| SRE | Collector/Tempo 故障 | 问答继续，查询接口返回稳定 503，exporter 后台失败 |
| 安全负责人 | 验证 APM 中无机构敏感问题 | 检查 Collector allowlist 与二次响应裁剪 canary |
| 平台工程师 | 迁移托管 Tempo/其他 OTLP 后端 | 替换 endpoint/backend URL，不改业务传播合同 |

典型流程：用户问答响应携带 `trace_id`；operator 使用受保护 API 查询；API 只向配置好的 Tempo 内网地址发起固定路径请求；响应转换为按开始时间排序的 span 列表和相对时间，支持构建瀑布视图。

## 5. 功能需求

### 5.1 Collector 数据面

- OTLP/gRPC 监听 `0.0.0.0:4317`，OTLP/HTTP 监听 `0.0.0.0:4318`；
- `memory_limiter` 必须是第一个 processor；
- `redaction` 只保留代码中定义的低基数安全属性；
- `transform` 只保留 `service.name` resource attribute，清空 scope/event/link 动态属性；
- 非固定 span name 统一为 `runtime.unknown`；
- status message 清空，禁止异常正文；
- `batch` 后通过 OTLP/HTTP 写入 Tempo；
- Collector 自身日志不得使用 debug payload 输出。

### 5.2 Tempo 数据面

- 单体 `target=all`，开放内部 `3200` 查询和 `4318` ingestion；
- 使用命名卷本地存储 WAL/blocks，设置有限 retention；
- 关闭匿名 usage reporting；
- 不发布宿主机端口；
- 只加入 internal backend 网络；
- 数据目录可写，其余根文件系统只读；
- profile 未启用时不创建容器或卷写入。

### 5.3 安全查询接口

`GET /api/telemetry/traces/{trace_id}`：

- 仅 operator/admin；
- trace ID 必须为 32 位小写/大写十六进制，内部转小写；
- backend 未启用返回 503 `trace_backend_disabled`；
- Tempo 404 返回业务 404；
- timeout/connect/5xx 返回 503 `trace_backend_unavailable`；
- 超过响应字节上限返回 502 `trace_backend_response_too_large`；
- 非法 JSON/结构返回 502 `trace_backend_invalid_response`；
- 设置 `Cache-Control: no-store`；
- 不接受 backend URL、TraceQL、start/end 或任意 header 参数。

### 5.4 安全瀑布模型

返回字段：

- `trace_id`；
- `services`：固定 `polyuquest-api|worker|bff|e2e`，未知映射为 `unknown`；
- `span_count`、`returned_span_count`、`truncated`；
- `duration_ms`；
- 每个 span 的 `span_id`、`parent_span_id`、固定 `name`、固定 `service`、`kind`、`status`、`start_offset_ms`、`duration_ms` 和 allowlisted scalar attributes。

禁止返回 resource 原文、scope、event、link、status message、URL、异常、原始 payload 和未知 attribute。

## 6. 总体架构与数据流

```text
Next.js BFF / FastAPI / Worker
  -> OTLP HTTP (background batch)
  -> OTel Collector [backend only]
       memory_limiter
       redaction + name/status normalization
       batch
  -> Tempo monolith [backend only]
       WAL + local blocks volume

Operator
  -> X-API-Key(operator)
  -> FastAPI /api/telemetry/traces/{trace_id}
  -> fixed Tempo /api/v2/traces/{trace_id}
  -> byte/JSON/schema limits
  -> second privacy allowlist
  -> bounded waterfall JSON
```

数据面故障与控制面故障都不进入问答关键路径。Trace 查询只用于诊断，不反向调用 Agent、Neo4j、Qdrant 或 LLM。

## 7. 数据模型与接口合同

### 7.1 配置

- `TRACE_BACKEND_ENABLED=false`；
- `TRACE_BACKEND_URL=http://tempo:3200`；
- `TRACE_BACKEND_TIMEOUT_SECONDS=3`；
- `TRACE_BACKEND_MAX_RESPONSE_BYTES=2097152`；
- `TRACE_BACKEND_MAX_SPANS=500`；
- 已有 `OTEL_TRACING_MODE`、`OTEL_EXPORTER_OTLP_ENDPOINT` 和 sampling 配置继续生效。

Backend URL 只允许 http/https origin，不允许 credentials、query、fragment 或额外 path。该值仅在启动期解析，不能由 HTTP 请求覆盖。

### 7.2 Trace span view

所有时间使用非负毫秒。`start_offset_ms` 以 trace 最早 span 为 0；trace duration 取最早 start 到最晚 end。无效 ID、负时间和畸形 span 被忽略并计入原始 `span_count`，不会抛出原始解析异常。

### 7.3 响应上限

先检查 `Content-Length`，再检查实际 body 长度；解析后最多返回 `TRACE_BACKEND_MAX_SPANS`。排序和截断均为确定性：`start_unix_nano`、`service`、`name`、`span_id`。

## 8. 安全、隐私与合规

1. Collector 与 API 各自执行 allowlist，任一层配置失误不应直接泄露原始字段；
2. Tempo/Collector 不发布宿主机端口，也不加入外部 egress 网络；
3. 查询接口继承 operator RBAC 与 route-template 安全审计；
4. 不代理用户提供的 URL、header、query language 或时间范围；
5. Tempo response 不写日志，异常只记录固定类别；
6. trace/span ID 是相关标识而非凭据，授权仍由 API key 决定；
7. Collector processor 配置用 canary OTLP payload做真实验证；
8. `service.name` 只显示预定义集合；
9. 内置本地卷应纳入备份/retention 与访问控制，但不作为长期合规归档；
10. 多租户场景必须使用独立 tenant/auth 网关，本轮单租户 profile 不应直接外网暴露。

## 9. 性能、稳定性与可用性

- exporter 使用现有 BatchSpanProcessor，业务线程不等待 Tempo；
- Collector memory limiter 提供反压，batch 减少写入次数；
- Collector/Tempo 设定 CPU、内存、PID 与重启策略；
- 查询 timeout 默认 3 秒，最大响应 2 MiB、最大 500 spans；
- 查询失败不影响 `/health/ready` 与 Agent Run；
- profile 默认关闭，零 tracing 场景不新增运行资源；
- 单机 Tempo 允许短时不可用，生产 HA 应迁移对象存储和多副本；
- E2E 使用轮询等待最终一致，不用固定长 sleep。

## 10. 测试与验收标准

1. parser 同时支持 Tempo V1/V2 常见 OTLP JSON 包装；
2. hex/base64 trace/span ID 均规范化；
3. 只输出允许的 service/name/attribute；
4. parent、offset、duration、排序和截断正确；
5. disabled、404、timeout、connect、5xx、oversize、invalid JSON 映射正确；
6. reader 403，operator/admin 200；
7. BFF allowlist 不包含 trace operator 路由；
8. 生产 Compose profile 默认不启动，启用后 Collector/Tempo 只在 internal backend 且不发布端口；隔离 E2E 仅向 runner 的 `127.0.0.1` 发布测试端口；
9. Collector 配置验证通过，真实 OTLP trace 可由 Tempo 按 ID查询；
10. canary query、URL、secret attribute、status message 不出现在 Tempo 查询和安全 view；
11. 问答 API 在 Collector/Tempo 停止时仍可创建/执行 Run；
12. Python、前端、Ruff、deployment/supply-chain、Compose 与 GitHub Linux 门禁通过。

### 10.1 本轮验收证据

| 验收项 | 结果 |
|---|---|
| Trace/RBAC 定向测试 | 28 passed，61 subtests passed |
| Python 全量回归 | 273 passed，65 subtests passed |
| Frontend Vitest / TypeScript | 20 passed / passed |
| Ruff / deployment / supply-chain / Compose | passed |
| Distributed Tracing E2E | run `31862991912` passed |
| 真实 trace | 3 spans；3 个标准化名称；2 条父子关系；16.006s 完整可查询 |
| 双层隐私 canary | 6/6 raw Tempo 与安全 view 均无泄漏 |
| E2E artifact | ID `9241158185`；SHA-256 `3e1181c1a064e547d1607d7186423a78d96bf67e47e43bebfd61123e0a52eeb3` |
| Production Topology / Business / Container Supply | runs `31862831819` / `31862831800` / `31862831799` passed |
| Browser BFF SSE | run `31862991789` passed |

真实 E2E 过程中发现并修复了测试 internal 网络阻断、Tempo 部分结果窗口、后置 redaction 删除 `service.name`、Collector exporter 弃用别名以及 validator 缺失文件异常；完整过程只记录在本地迭代文档，不随仓库发布。

## 11. 配置、部署与运维

单机启用顺序：

1. 将 `OTEL_TRACING_MODE=otlp`；
2. 将 `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`；
3. API 设置 `TRACE_BACKEND_ENABLED=true` 与 `TRACE_BACKEND_URL=http://tempo:3200`；
4. `docker compose --profile observability ... up -d`；
5. 检查 Collector/Tempo readiness、export failure 和 operator trace query；
6. 建立 Tempo 卷使用率与 retention 告警。

关闭顺序：先恢复 `OTEL_TRACING_MODE=disabled`，再停止 profile。即使直接停止 Collector/Tempo，应用也必须继续服务。

多节点生产不得使用本地块存储，应切换 S3/GCS/Azure/兼容对象存储或托管 Tempo，并在反向代理/网络策略处提供认证、TLS、tenant isolation 和限流。

## 12. 发布、回滚与风险控制

### 12.1 发布

1. 先发布 parser/query endpoint，backend disabled；
2. 启动 Collector/Tempo profile，不启用应用 exporter；
3. 用 E2E canary 验证隐私处理；
4. API/Worker/BFF 先以低采样率开启；
5. 观察内存、export failure、卷增长和查询 P95；
6. 再决定采样率和 retention。

### 12.2 回滚

- 设置 `TRACE_BACKEND_ENABLED=false` 立即关闭查询；
- 设置 `OTEL_TRACING_MODE=disabled` 停止新 trace；
- 停止 observability profile 不影响业务容器；
- Collector/Tempo 配置和卷均为附加结构，不需要数据库回滚；
- 删除 trace 卷属于破坏性数据操作，必须单独审批，不随应用回滚执行。

### 12.3 风险

- 单机 Tempo 不高可用；
- processor 语法随 Collector 版本变化，必须锁版本并做 config/E2E 门禁；
- head sampling 可能遗漏偶发问题；
- 长 Agent trace 可能在查询时仍不完整；
- 第三方镜像扩大供应链面，需要版本锁定、SBOM/CVE 与升级节奏；
- 自动 instrumentation 可能新增未知 span，Collector/API 都必须默认归一为 unknown。

## 13. 未来优化与改进方向

1. 使用对象存储和 Tempo 多副本实现跨节点高可用；
2. 引入认证网关、mTLS、tenant header 与租户级 retention；
3. 部署 Grafana operator 控制台和 trace-to-metrics/logs 跳转；
4. 在 Agent 超时、错误和重试场景使用 tail-based sampling；
5. 用 span metrics connector 生成 RED 指标和服务依赖图；
6. Collector 增加持久化 sending queue，承受 Tempo 短时故障；
7. 用 object lifecycle、删除 API和审计支持数据保留合规；
8. 前端实现 operator-only waterfall，但不把 operator key置于 reader BFF；
9. 为 collector/tempo 镜像增加 digest pin、签名验证、SBOM 与独立漏洞预算；
10. 将当前固定阶段 span 与 SLO/error budget 自动关联，形成异常 trace 示例集合。
