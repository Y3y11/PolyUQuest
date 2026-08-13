# Agent 可靠性与可运维性优化 PRD（迭代 3）

## 1. 背景与定位

迭代 1 在 PolyUQuest 图结构增强 RAG 算法框架之上实现了面向实时 Web 的自主检索问答 Agent MVP；迭代 2 将探索得到的可信页面以 Patch 形式增量写回 Neo4j/Qdrant。

当前链路已经能够“搜索—探索—抓取—回答—入图”，但 Patch 状态仍主要保存在进程内，服务重启会丢失待修复任务；相同页面即使没有变化也会重复下载；健康检查无法区分“进程存活”和“依赖可用”；图谱统计也无法区分已抓取页面与仅由链接产生的占位节点。本迭代以稳定运行和故障可恢复为目标，不扩展租户权限、内容审批等治理能力。

## 2. 目标与非目标

### 2.1 本迭代目标

1. Observation 与 GraphPatch 持久化，服务重启后仍可审计和恢复。
2. 启动时扫描 `publishing/repair_required` Patch，并以幂等发布流程进行有限重试。
3. 使用 ETag/Last-Modified 条件请求；收到 304 时复用已入库快照，不重复分块、向量化和写库。
4. 拆分 liveness、readiness、dependency health，便于本地部署及容器编排定位故障。
5. 图谱统计区分 fetched page 与 link stub，避免“页面很多但没有正文”的误判。
6. 生产启动默认关闭 auto-reload，所有运行参数通过环境变量配置。

### 2.2 非目标

- 企业账号、租户、RBAC、页面审批与敏感内容治理。
- 分布式任务队列、跨实例分布式锁、Exactly-once 事务。
- 通用互联网搜索引擎接入与不可信站点质量过滤。
- 替换 Neo4j/Qdrant 或重构已有检索算法。

## 3. 修改前现状与主要问题

### 3.1 Patch 账本只存在于 API 进程

`ObservationStore/PatchStore` 使用有界内存字典。若 Neo4j 已写入但 Qdrant 写入或读后校验失败，Patch 会变成 `repair_required`；此时一旦进程重启，Observation 与 Patch 同时丢失，无法自动补偿。

### 3.2 已抓取页面缺少条件重验证闭环

Fetch Tool 已支持 `If-None-Match/If-Modified-Since` 和 304，但页面的 validator 没有完整持久化并传回 Agent，304 后也没有复用已入库 Block，因此真实链路仍会重复下载、解析和向量化。

### 3.3 健康状态语义混合

原 `/api/health` 同时检查 Neo4j 与 Qdrant。依赖短暂故障时会显示 degraded，但无法回答“API 进程是否活着”“是否完成启动预热”“具体哪个依赖异常”。

### 3.4 WebPage 统计会被链接占位节点放大

Agent 抓取一个页面时会为其外链建立 WebPage stub。原统计只返回 WebPage 总数，前端容易把 stub 当成已经抓取、已经有正文和向量的页面。

### 3.5 开发启动参数进入生产路径

`agent-rag-serve` 固定 `reload=True`，会产生额外进程和文件监听器，不适合作为默认生产行为。

## 4. 方案设计

### 4.1 SQLite Observation/Patch Ledger

新增本地 SQLite 账本，分别存储 Observation 与 Patch：

- Observation：运行 ID、规范 URL、content hash、压缩 HTML、结构化 Blocks、发现链接与元数据。
- Patch：完整状态机、operation、attempts、last_attempt_at、error 与更新时间。
- SQLite 使用 WAL、busy timeout 和原子 upsert；单元测试仍可使用内存实现。
- `AGENT_LEDGER_PATH` 配置文件位置，运行数据目录不进入 Git。

该账本是写入过程的可恢复日志，不替代 Neo4j/Qdrant 作为知识查询源。

### 4.2 启动恢复

FastAPI lifespan 完成基础预热后执行一次有限扫描：

1. 查询 `publishing`、`repair_required` Patch；
2. 验证对应 Observation 仍存在；
3. 调用同一个 `PublishPatchTool`，通过 source URL 锁和 content hash 实现幂等重放；
4. 每次尝试更新 attempts/last_attempt_at；
5. 超过 `AGENT_PATCH_MAX_ATTEMPTS` 后保留错误并停止自动重试，避免坏数据拖慢每次启动。

### 4.3 条件抓取与 304 快照复用

1. WebPage 节点持久化 `etag` 与 `last_modified`。
2. Agent 选择页面后先读取已入库页面快照与 Blocks。
3. Fetch 请求携带 validator。
4. HTTP 200：正常生成 Observation，回答并按配置发布 Patch。
5. HTTP 304：直接把现有 Blocks 转成 Evidence，跳过 Stage/Publish、Embedding 与图写入；trace 标记 `not_modified` 和复用块数量。
6. 本地 embedding 优先使用 Hugging Face 本地缓存，避免模型已缓存时仍因 metadata HEAD 请求失败而误判未就绪；缓存缺失时才尝试下载。

### 4.4 健康检查拆分

- `GET /api/health/live`：只证明进程事件循环可响应，不访问外部依赖。
- `GET /api/health/ready`：检查启动预热状态及核心依赖，未就绪返回 503。
- `GET /api/health/dependencies`：返回 Neo4j、Qdrant 的独立状态。
- `GET /api/health`：保留兼容入口，语义等同 dependencies。

### 4.5 数据质量统计

`/api/graph/stats` 新增：

- `fetched_webpages`：有非空 content_hash 的页面。
- `stub_webpages`：仅由 LINKS_TO 建立、尚未抓取正文的页面。

前端在 Pages 下方直接展示 fetched/stub 分布。

## 5. 配置项

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `API_RELOAD` | `false` | 是否启用 Uvicorn 热重载 |
| `AGENT_LEDGER_PATH` | `data/runtime/agent_ledger.sqlite3` | 持久化账本 |
| `AGENT_REPAIR_ON_STARTUP` | `true` | 启动时是否执行恢复扫描 |
| `AGENT_REPAIR_MAX_PATCHES` | `25` | 单次启动最多恢复 Patch 数 |
| `AGENT_PATCH_MAX_ATTEMPTS` | `5` | 自动恢复最大尝试次数 |

## 6. 验收标准

1. 写入 SQLite 后新建 Store 实例，仍能读回完整 Observation 与 Patch。
2. 模拟发布失败后 Patch 为 `repair_required`；重建进程级对象后能成功重放为 `published`。
3. 已入库页面带 ETag/Last-Modified 时 FetchInput 携带 validator；模拟 304 后复用证据且 Patch publish 调用次数为 0。
4. `/health/live` 不依赖 Neo4j/Qdrant；依赖故障时 `/health/ready` 返回 503，并指出故障依赖。
5. Graph stats 满足 `webpages = fetched_webpages + stub_webpages`。
6. Python 测试、Ruff、前端 TypeScript 检查通过；真实服务完成 health、graph stats 与一次 Agent 查询冒烟测试。

## 7. 风险与回滚

- SQLite 文件损坏：停止恢复并记录错误，不影响 Neo4j/Qdrant 已有知识读取；可备份后重建 ledger。
- 启动恢复耗时：受最大 Patch 数和最大尝试次数约束；可通过 `AGENT_REPAIR_ON_STARTUP=false` 关闭。
- 304 但本地快照缺失：不生成空答案，记录一致性错误并在下一轮执行无 validator 的完整抓取。
- 新 health 路径影响监控：保留 `/api/health` 兼容入口。
- 回滚代码不会删除已写入图数据；SQLite ledger 可保留供审计或移动到备份目录。

## 8. 未来优化方向

1. 使用 Redis Streams/Kafka/数据库 Outbox 将抓取回答与异步入图解耦，并增加死信队列。
2. 多实例部署时使用 Redis/数据库 advisory lock 替代进程内 URL 锁。
3. 为 Observation 增加 TTL、容量配额、压缩比和清理作业，避免长期运行磁盘无上限增长。
4. 引入 OpenTelemetry trace、Prometheus 指标与结构化告警，覆盖抓取成功率、Patch 延迟、恢复次数、304 命中率和索引一致性。
5. 增加定时 freshness scheduler，根据页面类型、历史变化频率与业务价值自适应刷新。
6. 在权限优先级恢复后，增加租户隔离、域名策略、写入审批和敏感字段脱敏。
