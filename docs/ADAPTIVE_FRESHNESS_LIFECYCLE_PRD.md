# 自适应知识新鲜度与页面生命周期 PRD

## 1. 迭代目标

在查询驱动探索、质量门控和异步增量入图已经完成的基础上，为长期知识库增加主动页面再验证能力。系统需要根据页面历史变化、访问热度、质量价值和失败状态动态计算下一次检查时间，并优先使用 HTTP 条件请求完成低成本刷新。

本迭代把系统从“查询发现新内容后写入”推进到“已入库内容能够被持续维护”。

## 2. 业务背景

企业内网、大学/研究机构官网、政府门户和产品文档都不是静态语料库：招生要求、人员、政策、公告和版本文档会变化，但不同页面的变化速度差异很大。

- 固定周期全量重建浪费抓取、Embedding 和双存储写入成本；
- 只在用户提出“最新”问题时刷新，会让普通查询长期命中旧数据；
- 所有页面使用同一 TTL，会频繁刷新稳定页面，也可能错过高频变化页面；
- 页面返回 304 时若不更新验证时间，系统仍会误判证据过期；
- 定时任务若没有持久化、lease、退避和运维接口，服务重启或多实例运行时会丢任务或重复抓取。

## 3. 产品原则

### 3.1 再验证不等于重新索引

- `304 Not Modified`：只更新 `last_validated_at` 和下一次检查时间，不创建 Observation/Patch/Embedding；
- `200 + hash unchanged`：按未变化处理，不重新索引；
- `200 + hash changed`：重新执行通用质量门控，只有 `index` 才进入 Outbox；
- `evidence_only/discard`：停止使用旧快照回答并进入 quarantine，防止陈旧内容继续被召回。

### 3.2 动态 TTL 只使用通用信号

策略基于：

- 历史 `changed/unchanged` 次数；
- 页面质量分数；
- 近期检索访问次数；
- 可配置业务优先级；
- 连续失败次数。

禁止根据 PolyU、院系或招生路径写特殊规则。

### 3.3 查询刷新与后台维护互补

- 用户明确要求最新信息时，Agent 仍可立即执行受控在线刷新；
- 后台 Freshness Worker 负责在没有用户查询时维护长期页面；
- 后台刷新不能阻塞回答路径。

### 3.4 可恢复与多实例安全

刷新目标、租约、失败和下一次运行时间持久化在 SQLite。Worker 使用 compare-and-set lease；崩溃后 lease 过期可被其他 Worker 回收。

## 4. 生命周期状态

| 状态 | 含义 | 是否参与检索 |
|---|---|---|
| `active` | 已索引且允许返回 | 是 |
| `checking` | Worker 正在条件验证 | 继续使用当前快照 |
| `indexing` | 新版本已抓取并进入 Outbox | 继续使用旧快照，直至新版本发布成功 |
| `retry` | 抓取失败，等待退避 | 是，但保留旧验证时间 |
| `quarantined` | 新页面不再通过质量门控 | 否 |
| `paused` | 运维人员暂停主动刷新 | 是 |

## 5. 自适应间隔策略

配置参数：`min_ttl_hours`、`default_ttl_hours`、`max_ttl_hours`、`unchanged_multiplier`、`changed_multiplier`、`hot_access_threshold`、`hot_access_multiplier`、`failure_backoff_hours`。

规则：

1. 初次入库使用 default TTL；高质量/高优先级页面适度缩短；
2. 每次未变化，TTL 乘以 `unchanged_multiplier`，上限为 max TTL；
3. 每次发生变化，TTL 乘以 `changed_multiplier`，下限为 min TTL；
4. 最近访问达到热点阈值时缩短下一周期；
5. 连续失败按指数退避，但不超过 max TTL；
6. 所有计算结果和策略版本持久化，便于回放与调参。

## 6. 数据模型

SQLite 新增 `page_lifecycle_targets`：

- 身份：`source_url`（主键）、`content_hash`；
- 时间：`indexed_at`、`last_checked_at`、`last_validated_at`、`last_changed_at`、`next_check_at`；
- 策略：`current_ttl_hours`、`quality_score`、`business_priority`、`policy_version`；
- 反馈：`change_count`、`unchanged_count`、`access_count`、`last_accessed_at`；
- 可靠性：`status`、`consecutive_failures`、`last_error`、`lease_until`、`worker_id`；
- 异步衔接：`pending_job_id`、`pending_content_hash`。

Neo4j WebPage 同步保存：`lifecycle_status`、`last_validated_at`、`next_check_at`、`current_ttl_hours`、`change_count`、`unchanged_count`。

## 7. 系统流程

```text
Index Worker publish success
  -> register/update lifecycle target
  -> compute next_check_at

Freshness Worker
  -> claim due target with lease
  -> load Neo4j snapshot + ETag/Last-Modified
  -> conditional Fetch
      +-- 304 --------------------> validate unchanged -> reschedule
      +-- 200 same hash ----------> validate unchanged -> reschedule
      +-- 200 changed -> Quality Gate
              +-- index ----------> Stage -> Outbox -> status=indexing
              +-- evidence/discard -> quarantine old snapshot
      +-- failure ----------------> exponential retry

Search Tool
  -> exclude quarantined pages
  -> use last_validated_at as evidence freshness
  -> record access signal for existing lifecycle targets
```

## 8. 运维接口

- `GET /api/freshness/targets`：按状态查询目标；
- `GET /api/freshness/target?url=...`：查看单页生命周期；
- `GET /api/freshness/stats`：状态数量、due 数和最老逾期时间；
- `POST /api/freshness/refresh-now?url=...`：立即到期；
- `POST /api/freshness/pause?url=...`：暂停主动刷新；
- `POST /api/freshness/resume?url=...`：恢复并立即调度。

URL 使用 query/body 参数而不是 path 捕获，避免斜杠和编码歧义。

## 9. 可观测性

日志至少记录：worker_id、URL、结果（unchanged/changed/quarantined/retry）、status code、旧/新 hash、TTL、next_check_at、job_id 和错误。

Readiness 增加 `freshness_worker`。聚合统计至少包含：

- active/checking/indexing/retry/quarantined/paused；
- due_targets；
- oldest_overdue_seconds；
- total_changes / total_unchanged；
- average_ttl_hours。

## 10. 验收标准

1. 索引成功后自动注册刷新目标；
2. Worker 只领取已到期目标，lease 过期后可安全回收；
3. 304 和相同 hash 不创建 Patch/Job，并更新 `last_validated_at`；
4. 内容变化且质量通过时只提交一次异步索引 Job；
5. 新内容质量不通过时旧页面进入 quarantine，Search Tool 不再返回；
6. 失败按指数退避，成功后清零失败计数；
7. 热点、变化和长期不变页面得到不同 TTL；
8. pause/resume/refresh-now 和统计 API 可用；
9. 服务重启后目标、租约和调度时间仍可恢复；
10. 全量回归、Ruff、TypeScript、隔离端口 API 冒烟和真实数据校准通过。

## 11. 非目标

- 本轮不实现 DOM Diff 和局部 Block Embedding；
- 不自动物理删除 quarantined 页面；
- 不实现跨租户 ACL 或审批流；
- 不迁移到 PostgreSQL/Redis/Kafka；
- 不承诺外部网站一定提供 ETag/Last-Modified。

## 12. 后续优化

1. DOM Diff 与增量向量更新；
2. 页面版本历史、差异展示和可恢复回滚；
3. 基于实际 Exploration-to-Reuse Hit Rate 学习业务价值；
4. Sitemap/Lastmod、Webhook、CMS 事件驱动刷新；
5. 分布式调度、速率限制、域级并发和抓取熔断；
6. quarantined 内容的人工审核、可恢复删除和 retention policy；
7. 以 Staleness SLA、304 Hit Rate、Change Detection Delay 和 Refresh Cost 为核心建立运营面板。
