# 端到端运行可观测性与回归评测闭环 PRD

## 1. 迭代目标

为 Query-driven Web Agent 建立统一、持久化、可回归的运行度量：用同一 `run_id` 串联查询理解、PolyUQuest 检索、Web 探索、证据评估、回答生成、异步索引和一致性治理；提供并发安全的分阶段 LLM token 归因、延迟/SLO 聚合、单次运行详情和离线评测报告。

本轮不追求替换所有日志基础设施，而是在单机 MVP 上建立稳定 telemetry contract。未来接入 OpenTelemetry、Prometheus 或数据仓库时，业务字段和评测口径无需重写。

## 2. 业务问题

面向企业内网和机构门户，系统质量不只等于一次回答“看起来正确”。需要持续回答：

1. 一次查询为什么慢、贵或失败；
2. 是知识库未命中、Web 抓取失败、证据不足还是生成失败；
3. 在线发现的页面是否进入异步索引，最终是否发布成功；
4. 新版本是否提升了回答与证据质量，还是只增加成本；
5. 不同查询类型、路由模式和探索策略的质量/成本如何比较；
6. 系统是否满足约定的延迟、成功率、证据引用和持久化 SLO。

## 3. 修改前现状与问题

### 3.1 Token 统计无法在并发 API 中归因

`llm/client.py` 使用进程级 `_USAGE_TOTALS/_STAGE_TOTALS`。离线串行脚本可通过前后 diff 统计，但并发请求会互相污染，异步 `to_thread` 与流式回答也缺少稳定 run 归属。

### 3.2 Agent Action 只随响应返回

Action 已有 action/status/duration/details，但没有独立持久化。用户关闭页面、SSE 中断或服务重启后，无法按 run 查询完整轨迹，也无法做 P50/P95 聚合。

### 3.3 回答与异步入库链路断开

Agent run_id 已写入 Patch/Job，但 Index Worker 成功/失败、PageVersion 成本和 repair/reconciliation 结果没有回写统一运行视图。无法衡量“回答已返回后多久知识才可复用”。

### 3.4 评测缺少稳定输入输出契约

仓库没有可直接运行的统一 evaluation harness。论文离线指标与工程 Agent 场景的 evidence sufficiency、citation precision、web exploration gain、indexing eventual success、latency/tokens 还没有形成同一报告。

### 3.5 指标口径容易失真

- warm cache 与真实 provider 成本被混为一谈；
- abstain 可能被错误计为失败；
- 没有 gold 的样本不应输出伪 correctness；
- 仅测同步 HTTP 延迟会漏掉异步知识可见延迟；
- 平均值会掩盖尾延迟。

## 4. 统一 Telemetry 模型

### 4.1 RunTelemetry

- `run_id`、`run_type: agent_query|indexing|reconciliation|evaluation`；
- parent/root run、query hash（默认不持久化原始问题正文）；
- status、response status、route mode、stop reason；
- started/completed、duration、first evidence、answer complete；
- input/output tokens、LLM calls、估算成本；
- evidence/page/fetch/index counters；
- error category、error message（脱敏/截断）；
- config fingerprint、代码版本、模型/provider。

### 4.2 TelemetrySpan

- stable span ID、run/parent span；
- stage、operation、status；
- start/end/duration；
- token/call/cost；
-受控 attributes（不得写网页正文、prompt、API key）；
- link IDs：observation/patch/job/version/reconciliation。

### 4.3 LLM Usage Event

每次 LLM 调用通过 ContextVar 读取当前 run/stage，记录 provider/model、cache hit、真实/重放 token、duration 和 status。进程级累加器继续保留用于兼容，但生产指标读取 run-local event，避免并发污染。

## 5. 采集边界

### 5.1 Agent Answer Path

`QueryDrivenAgent.run()` 创建 root run；现有 `record()` 同时写 Action 与 Span。最终保存 response status、mode、stop reason、evidence/pages/fetches/queued jobs、总时延和 LLM usage。

即使异常或调用方取消，也必须以 error/cancelled 终态关闭 run。SSE 断开不得删除已写 span。

### 5.2 Async Indexing

Index Worker 使用 Job 中的 `run_id` 作为 root run link，新增 child `indexing` run/span；记录 queue wait、publish duration、attempt、Patch/PageVersion ID、embedding/extraction/fact counters 与结果。

### 5.3 Reconciliation

Reconciliation run 作为运维 run，记录 scan/execute/verification duration、finding/action 数和父 verification 关系；不与用户 query 强绑定，但 Patch replay 可 link 原 Agent run。

## 6. SLO 与聚合

默认工程目标（配置化，不作为论文结论）：

- Agent answered/partial/abstained 合法完成率 ≥ 99%；
- API query P95 ≤ 90s（受控探索预算内）；
- KB-only P95 ≤ 15s；
- citation-bearing answer 比例 ≥ 95%；
- accepted indexing job 15 分钟内 published ≥ 99%；
- dead-letter rate < 1%；
- telemetry write failure 不影响主回答，但必须计数/告警。

统计提供 count、mean、P50、P95、P99、min/max；样本数过小时仍返回真实值但标记 sample size，不假装具有统计显著性。

## 7. 离线评测契约

### 7.1 Dataset JSONL

每行：

- `case_id`、question；
- tags/task_type/domain；
- 可选 reference answer；
- 可选 expected source URL/pattern、required facts、forbidden claims；
- exploration expectation：kb_only/web_allowed/web_required；
- persistence expectation；
- budget/mode overrides。

### 7.2 Case Result

- response status、answer、evidence URLs；
- retrieval hit、source recall/precision、required fact coverage；
- citation coverage（回答引用是否有对应 evidence）；
- exploration gain（初始 KB evidence → final evidence）；
- latency/token/LLM calls/pages/fetch failures；
- indexing queued/published（可选等待窗口）；
- evaluator status：scored/not_applicable/error。

### 7.3 指标原则

- 无 reference/gold 的指标记 N/A，不填 0；
- 默认使用确定性规则指标，不调用 judge LLM；
- LLM-as-judge 作为显式可选项，记录 judge 模型、prompt version、token 和原始结构化评分；
- correctness/faithfulness 不能用 citation 数量代替；
- baseline/variant 必须使用同一 dataset snapshot 和 config fingerprint。

## 8. API 与 CLI

- `GET /api/telemetry/runs`；
- `GET /api/telemetry/runs/{run_id}`；
- `GET /api/telemetry/stats?run_type=&since=`；
- `GET /api/telemetry/slo`；
- `python -m agent_rag.evaluation.cli run --dataset ... --output ...`；
- `python -m agent_rag.evaluation.cli compare --baseline ... --candidate ...`。

CLI 输出 JSON（机器读取）和 Markdown（简历/实验记录）。报告包含 config/code/model fingerprint，禁止只保留汇总数字而丢失 case result。

## 9. 隐私、安全与成本

1. 默认只保存 `sha256(normalized_query)` 和长度，不保存原始 query；本地开发可显式开启；
2. span attributes 使用 allowlist，禁止 prompt、answer 正文、网页正文和 secret；
3. error 截断并脱敏常见 API key/Bearer token；
4. token 使用 provider response；无 usage 时标记 unknown，不伪造精确值；
5. cache hit 同时记录 logical usage 和 billable usage=0；
6. SQLite 写入失败不阻塞用户响应，进入内存 drop counter；
7. retention 与采样后续配置化；评测 case 结果默认由 CLI 显式保存。

## 10. 失败分类

- `dependency`：Neo4j/Qdrant/HTTP/LLM；
- `policy`：域名、质量门控、持久化权限；
- `budget`：时间/页面/迭代；
- `evidence`：无命中或证据不足；
- `generation`：答案生成失败；
- `indexing`：Patch/Outbox/Version；
- `consistency`：reconciliation；
- `cancelled`；
- `internal`。

Abstain 是受控产品结果，只有异常终止才计 error。

## 11. 验收标准

1. 两个并发 run 的 LLM tokens/stages 不互相污染；
2. cache hit 区分 logical 与 billable usage；
3. Agent 成功、abstain、异常、取消均形成持久化终态；
4. Action 与 span 一一映射且不存正文；
5. Index Worker 通过 job.run_id 回接 root query，并记录 queue/publish 指标；
6. telemetry 写失败不影响 Agent 响应；
7. stats 正确计算 P50/P95/P99 和状态比例；
8. SLO 返回目标、实测、样本数和 pass/fail/insufficient_data；
9. evaluation dataset schema 拒绝重复 case_id 与无效 expectation；
10. 无 gold 指标输出 N/A；
11. compare 只比较 dataset/config 兼容报告，否则明确拒绝；
12. 完整测试、Ruff、TypeScript、真实 Agent run 与 API 冒烟通过。

### 11.1 本轮实现映射

| 需求 | 实现 |
|---|---|
| Run/Span 持久化 | `telemetry/models.py`、`telemetry/store.py`，复用 SQLite WAL ledger |
| 并发 LLM 归因 | `telemetry/recorder.py` ContextVar + `llm/client.py` usage event |
| Agent 终态与 Action | `QueryDrivenAgent.run()` 生命周期包装；Action 完成时立即写 Span |
| 异步入库关联 | Index Worker child run，保留 root Agent run、Job/Patch/attempt/queue wait |
| 一致性治理关联 | scan run、execute child run、verification child scan |
| 聚合与 SLO | `/api/telemetry/runs|stats|slo` + `configs/observability.yaml` |
| 回归评测 | `evaluation/models.py`、`scoring.py`、`reporting.py`、`cli.py` |
| 隐私边界 | query 只存 SHA-256/长度；Span attributes allowlist；正文不入账本 |
| 降级边界 | TelemetryRecorder 捕获写入异常、累计 dropped_writes 并结构化告警 |

实现中进一步将 `quality_score` 与 `operational_score` 分离。只有探索、持久化、延迟和页面预算而没有事实/来源 gold 时，不生成 overall，防止“流程符合预期”被误读为“回答正确”。

## 12. 非目标

- 本轮不部署 Prometheus/Grafana/Jaeger；
- 不用 LLM judge 重新宣称论文指标；
- 不收集用户身份、IP 或完整对话正文；
- 不构建前端分析 dashboard；
- 不引入外部付费 observability SaaS；
- 不承诺单机 SQLite 是最终高吞吐时序数据库。

## 13. 后续方向

1. OpenTelemetry OTLP exporter 与 trace propagation；
2. Prometheus recording rules、Grafana SLO dashboard 与告警；
3. Postgres/ClickHouse telemetry warehouse；
4. 在线 canary、A/B、shadow evaluation 和自动回滚；
5. 领域 gold set、人工标注平台和 judge calibration；
6. 成本预算、provider routing 与质量—延迟—成本 Pareto 优化。
