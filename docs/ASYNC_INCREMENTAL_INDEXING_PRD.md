# 异步增量入图与任务治理 PRD（迭代 4）

## 1. 业务背景

PolyUQuest 以机构网站为知识来源。Agent 在回答长尾或时效性问题时在线探索网页，并将新页面增量写回 Neo4j/Qdrant，后续问题即可复用。迭代 3 已解决 Patch 持久化、失败恢复和条件抓取，但入图仍在用户请求中同步执行：分块后的页面需要等待 Embedding、Neo4j、Qdrant 与读后校验全部结束，回答延迟受到写链路尾延迟影响。

业务上，“本次回答使用刚抓取的 Observation”与“将 Observation 发布为可复用知识”是两个不同的一致性边界：前者必须在请求内完成，后者允许最终一致。因此本迭代将回答路径与知识更新路径解耦。

## 2. 目标与非目标

### 2.1 目标

1. Agent 抓取并验证页面后，只在请求内 Stage Patch 和提交 Outbox Job，不等待入图。
2. 后台 Index Worker 持久消费任务，执行原有幂等 PublishPatch 流程。
3. 任务支持 lease、超时回收、指数退避、最大重试和 dead letter。
4. 使用规范 URL + content hash 做幂等合并，避免相同快照重复向量化。
5. 提供任务列表、详情、统计和 dead-letter 手动重试 API。
6. Agent Trace 明确区分“证据已用于回答”和“知识更新已排队”。
7. 服务重启后 pending/retry/running-expired 任务可继续处理。

### 2.2 非目标

- 本轮不引入 Redis、Kafka、Celery 等额外基础设施；SQLite Outbox 对应单实例/本地部署阶段。
- 不承诺 Neo4j/Qdrant 跨存储 Exactly-once；依赖已有幂等 Patch 和读后校验实现 At-least-once + 最终一致。
- 不实现租户权限、任务审批和敏感内容治理。
- 不在本轮执行 Entity/Topic 的异步抽取。

## 3. 修改前现状与问题

### 3.1 用户请求等待写路径

`QueryDrivenAgent` 在 Fetch 后同步调用 Stage 和 Publish。Embedding API、本地模型、Neo4j 或 Qdrant 任一抖动都会增加回答耗时。

### 3.2 恢复以启动扫描为主

迭代 3 仅在启动时扫描 `repair_required` Patch。运行期间的临时故障没有独立调度、退避与死信语义。

### 3.3 缺少任务级可观测性

系统能看到 Patch，但无法直接回答：有多少知识更新正在排队、等待多久、失败几次、何时重试、是否已进入死信。

### 3.4 重复快照可能产生重复任务

多个相近查询可能同时抓取同一 URL。虽然 Publish 能以 content hash 判定 unchanged，但仍可能创建多个等待执行的 Patch/Job。

## 4. 架构设计

```text
Agent Request
  -> Search / Explore / Fetch
  -> Observation (immediately usable evidence)
  -> Stage GraphPatch
  -> SQLite Outbox enqueue   <- request consistency boundary
  -> Compose Answer / return

Index Worker
  -> atomic claim + lease
  -> PublishPatchTool
  -> Neo4j + Qdrant + read-after-write
  -> succeeded
     or retry (exponential backoff)
     or dead_letter
```

### 4.1 Job 状态机

```text
pending -> running -> succeeded
                  -> retry -> running
                  -> dead_letter

running -- lease expired --> retry
dead_letter -- manual retry --> pending
```

Job 记录：`job_id`、`dedupe_key`、`patch_id`、`run_id`、`source_url`、`content_hash`、`status`、`attempts`、`max_attempts`、`available_at`、`lease_until`、`worker_id`、`last_error` 与各阶段时间。

### 4.2 幂等与并发

- `dedupe_key = SHA256(normalized_url + "\n" + content_hash)`，数据库唯一约束。
- Enqueue 冲突时返回已有 Job，不创建第二个任务。
- Claim 使用 SQLite `BEGIN IMMEDIATE`，同一任务只会被一个 Worker 获得。
- Worker 通过 lease 防止进程崩溃后任务永久停留在 running。
- PublishPatchTool 继续使用 URL 进程锁、content hash、读后校验和 repair operation。

### 4.3 重试策略

- Claim 时 attempts + 1。
- 失败后按 `base_delay * 2^(attempts-1)` 退避，并限制最大延迟。
- 达到 max attempts 后进入 dead_letter，不再自动消费。
- 管理 API 可将 dead_letter 重置为 pending，同时保留历史 attempts 和错误用于审计。

### 4.4 API

- `GET /api/indexing/jobs?status=&limit=`：任务列表。
- `GET /api/indexing/jobs/{job_id}`：任务详情。
- `GET /api/indexing/stats`：各状态数量及最老等待时长。
- `POST /api/indexing/jobs/{job_id}/retry`：重试 dead-letter/retry 任务。

## 5. 配置

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `AGENT_ASYNC_INDEXING` | `true` | Agent 是否提交异步任务；关闭时回退同步发布 |
| `INDEX_WORKER_ENABLED` | `true` | 是否在 API 进程内启动 Worker |
| `INDEX_WORKER_POLL_SECONDS` | `0.5` | 空队列轮询间隔 |
| `INDEX_WORKER_LEASE_SECONDS` | `120` | running lease |
| `INDEX_JOB_MAX_ATTEMPTS` | `5` | 最大执行次数 |
| `INDEX_JOB_RETRY_BASE_SECONDS` | `2` | 指数退避基数 |
| `INDEX_JOB_RETRY_MAX_SECONDS` | `300` | 最大退避 |

## 6. 验收标准

1. Agent 开启持久化时 enqueue 一次且不调用同步 Publisher，回答 Evidence 可立即使用。
2. 相同 URL + hash 连续 enqueue 返回同一 job_id。
3. 两个消费者竞争时，一个 Job 只能被成功 claim 一次。
4. Worker 成功后 Job 和 Patch 均为 succeeded/published。
5. 可重试错误按 available_at 延迟；达到阈值进入 dead_letter。
6. 模拟 Worker 在 running 时退出，lease 到期后新 Worker 能回收任务。
7. API 可查询任务和统计，并能手动重试 dead letter。
8. 服务重启后 SQLite 中的任务仍存在并继续消费。
9. Python、Ruff、TypeScript 通过，并完成真实依赖冒烟验证。

真实依赖测试必须满足隔离约束：不得用合成 Observation 覆盖已存在的真实 WebPage URL。集成测试应使用独立数据库/Collection；无法提供隔离依赖时，只允许抓取并发布该 URL 的真实页面内容。

## 7. 风险与回滚

- API 进程内 Worker 仍不等价于独立 Worker 服务；配置 `INDEX_WORKER_ENABLED=false` 可只运行 API，后续用独立进程消费同一 Outbox。
- SQLite 适用于单机中小吞吐；写竞争或任务量上升后迁移 PostgreSQL Outbox/Redis Streams。
- 异步模式下回答返回时 Evidence 仍是 temporary；前端不得错误显示“已入库”。
- 回滚可设置 `AGENT_ASYNC_INDEXING=false` 恢复同步发布，已有 pending Job 不会被删除。

## 8. 未来优化

1. 将 Worker 独立为 `agent-rag-index-worker` 进程并提供优雅停机、并发度和站点级限流。
2. PostgreSQL transactional outbox 或 Redis Streams consumer group。
3. OpenTelemetry/Prometheus：queue depth、oldest age、claim latency、publish P95、retry/dead-letter rate。
4. 管理页面展示任务详情、Patch 读后校验和人工重试。
5. 页面质量门控决定 `index / evidence_only / discard`，减少低价值入库。
6. DOM Diff、批量 Embedding 和 Entity/Topic 二阶段异步 enrichment。
