# 跨存储一致性治理与可审计修复 PRD

## 1. 迭代目标

在在线抓取、DOM Diff、增量实体关系抽取和事实时态已经跑通后，建立 Neo4j、Qdrant、SQLite PageVersion/FactVersion、Patch 与 Index Outbox 之间的周期性一致性治理能力。

本轮交付可持久化的 `ReconciliationRun` 与 `RepairAction`：扫描默认只生成报告和修复计划，不自动修改生产数据；运维人员明确确认后，仅执行可证明幂等、可重放的修复动作，并保留动作前后状态、错误和关联证据。

## 2. 业务背景

Web 知识库不是一次性离线构建：网页抓取、向量生成、图写入、事实历史和异步任务会长期并发运行。即使单次发布有读后校验，仍可能因为进程退出、依赖超时、人工操作、历史版本迁移或存储恢复产生长期漂移。

企业内网与机构门户更关心以下问题：

1. 当前回答使用的图和向量是否来自同一版网页；
2. 失败任务是否持续积压或进入死信；
3. 当前关系与审计历史是否一致；
4. 修复是否会误删仍有业务价值的数据；
5. 谁在什么时间发现、确认并执行了什么修复。

因此一致性治理不是普通 health check，而是一条独立、可审计的运维工作流。

## 3. 修改前现状与问题

### 3.1 检查范围过窄

现有 `GraphVectorStore.check_consistency()` 只比较 Neo4j 与 Qdrant 的 Block/Entity ID 集合，缺少：

- WebPage 与 Relation；
- Relation 的 `source_block_ids` payload；
- SQLite active Fact 与 Neo4j 当前关系；
- PageVersion、Patch、Outbox 状态之间的对应关系；
- 差异对象明细和可追踪 run ID。

### 3.2 检查结果不可持久化

当前方法只返回即时字典和日志。进程重启后无法回答何时开始漂移、是否重复出现、是否已经修复。

### 3.3 发现与修复没有安全边界

如果把“仅存在于某个存储”直接理解为垃圾并删除，会误伤：

- 原始离线 PolyUQuest 图中的历史数据；
- 正在发布但尚未完成跨存储写入的数据；
- 没有 Observation 可重放的旧批处理数据；
- Neo4j 当前事实与 SQLite 历史账本发生语义冲突的数据。

### 3.4 现有恢复入口相互割裂

- startup recovery 扫描 Patch；
- Outbox API 手工 retry dead letter；
- 单次 Publish 负责读后校验；
- 没有一个统一运行把 finding、repair action、执行状态和验证结果串联起来。

## 4. 权威源矩阵

| 数据 | 当前态权威源 | 派生/审计源 | 安全恢复方式 |
|---|---|---|---|
| 页面与 DOM Block | 已发布 Observation + PageVersion | Neo4j / Qdrant | 重放持久化 Patch |
| Entity mention / Relation | PageVersion.KnowledgeDelta + Neo4j 当前态 | Qdrant Relation payload | 重放持久化 Patch |
| 事实历史 | SQLite FactVersion | Neo4j 当前 RELATES_TO | 冲突时人工复核，不自动改历史 |
| 向量 | 对应文本/KnowledgeDelta | Qdrant | 重放 Patch 重新生成或补齐 |
| 发布任务 | Index Outbox | Patch/PageVersion 状态 | 显式 retry / replay |

原则：Qdrant 是可再生索引，但删除仍必须避开运行中任务；FactVersion 是审计账本，不能根据一次扫描结果自动删除或改写。

## 5. 领域模型

### 5.1 ReconciliationRun

- `run_id`、`status: scanning|planned|executing|completed|failed`；
- 扫描开始/结束、执行开始/结束时间；
- findings/actions/succeeded/failed/skipped 计数；
- 扫描范围、摘要、错误；
- 完整结果持久化到 SQLite。

### 5.2 ConsistencyFinding

- 稳定 `finding_id`；
- category、severity、object_type、object_id；
- source URL、Patch/PageVersion/Job 关联；
- expected/actual、可读原因；
- `repairability: automatic|manual_review|informational`；
- 推荐动作。

### 5.3 RepairAction

- `action_id`、run/finding 关联；
- `replay_patch` 或 `retry_index_job`；
- `planned|running|succeeded|failed|skipped`；
- target ID、执行前后状态、错误；
- `requires_confirmation=true`；
- 每个 run + action type + target 唯一，避免同一 Patch 因多个 finding 被重复执行。

## 6. 扫描规则

### 6.1 跨存储对象

扫描 WebPage、Block、Entity、Relation 的 Neo4j/Qdrant ID 集合；Relation 额外核对 `source_block_ids`。报告完整 ID 明细但限制 API 返回规模，摘要保留总数。

### 6.2 当前事实与历史账本

- SQLite active fact 在 Neo4j 缺失：critical；若 introduced PageVersion 对应 Patch 与 Observation 可用，计划 replay，否则人工复核；
- 带 `page_version_id` 的 Neo4j 在线事实不在 SQLite active：critical，人工复核，避免自动改写历史；
- Neo4j 与 Qdrant 关系证据集合不一致：warning/critical，可定位 Patch 时计划 replay。

### 6.3 版本与任务

- PageVersion `repair_required` 或 Patch `repair_required`：计划 replay_patch；
- Outbox `dead_letter`：计划 retry_index_job；
- running Job 在 lease 内：视为 in-flight，扫描报告但不生成冲突修复；
- 缺失 Observation、Patch 或不可定位来源：manual_review。

## 7. 执行与并发控制

1. `POST /reconciliation/runs` 只扫描并形成 planned run；
2. `POST /reconciliation/runs/{run_id}/execute?confirm=true` 才执行；
3. Store 以 SQLite compare-and-set 将 run/action claim 为 running，并用 execution owner + lease heartbeat 防止并发执行；
4. replay 前检查同 Patch 是否存在 running Outbox job；存在则 skipped；
5. replay 使用原 PublishPatchTool 与持久化 KnowledgeDelta，不另写第二套修复逻辑；
6. retry 只允许 retry/dead_letter Job；状态已变化则 skipped；
7. 进程中断后过期 execution lease 可被回收，running action 回到 planned；旧执行者无法覆盖新 owner 的结果；
8. 每个动作独立记录结果，一个失败不阻断其他动作；
9. 执行完成后再次扫描，形成独立验证 run，而不是篡改原始 finding。

## 8. API

- `POST /api/indexing/reconciliation/runs`：生成 dry-run；
- `GET /api/indexing/reconciliation/runs`：运行列表；
- `GET /api/indexing/reconciliation/runs/{run_id}`：finding/action 明细；
- `POST /api/indexing/reconciliation/runs/{run_id}/execute?confirm=true`：确认执行；
- `GET /api/indexing/reconciliation/stats`：累计运行、漂移、成功率与待人工复核数。

默认 API 不提供“按 finding 直接删除图数据”。删除型动作留待具备租户、审批和备份能力后加入。

## 9. 指标与告警

- reconciliation run success/failure；
- findings by category/severity；
- automatic/manual review 数量；
- repair succeeded/failed/skipped；
- unresolved findings；
- dead-letter jobs；
- repair_required PageVersions；
- 最近成功扫描时间与扫描耗时。

建议告警：critical finding > 0、dead letter > 0、连续两次 scan failed、repair failure rate > 5%、24 小时无成功扫描。

## 10. 安全边界

1. 扫描只读，默认不会修改外部存储；
2. 执行必须 `confirm=true`；
3. 不记录 API key、页面敏感正文或模型 prompt；
4. 不自动修改 FactVersion 历史；
5. 不自动删除无法证明来源的离线图/向量；
6. 所有写动作复用已有幂等入口；
7. 单个 run/action 可重复读取，但不可重复 claim；
8. API 上线到多用户环境前需要再加管理员 RBAC。

## 11. 验收标准

1. 漂移扫描覆盖 Block、Entity、Relation、active Fact、PageVersion 和 dead-letter Job；
2. scan 默认零外部写入；
3. 同一 Patch 的多个 finding 只生成一个 replay action；
4. 缺少 Observation 的 finding 标记 manual_review；
5. 未 confirm 的 execute 返回 400/422，不执行动作；
6. Patch replay 与 Job retry 复用现有实现且记录 before/after；
7. running Job 不被 reconciliation 并发重放；
8. action 通过 CAS 防止重复执行；
9. 执行租约过期后可恢复，旧 owner 不能写回新执行者的动作；
10. 历史 Patch 不得覆盖同 URL 的最新 PageVersion；
11. 运行和动作历史重启后仍可查询；
12. 单测、故障注入、Ruff、TypeScript、API 冒烟和真实只读扫描通过。

## 12. 非目标

- 本轮不建设完整前端运维控制台；
- 不自动修复事实语义冲突；
- 不实现跨租户审批流；
- 不替代 Neo4j/Qdrant 自身备份恢复；
- 不在扫描中调用 LLM；
- 不默认周期调度，先提供可被 Cron/Kubernetes Job 调用的稳定 API。

## 13. 后续方向

1. OpenTelemetry 串联 query/fetch/index/reconcile；
2. 管理端 Diff、审批、批量执行和回滚；
3. 定时调度、连续漂移告警和 SLO dashboard；
4. PostgreSQL advisory lock 与多实例 reconciliation leader election；
5. 删除型动作的快照备份、soft-delete 和恢复窗口；
6. 按租户/数据域的 policy、RBAC 与审计导出。
