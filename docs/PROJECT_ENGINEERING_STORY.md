# PolyUQuest 到实时 Web 知识 Agent：项目工程化演进总览

> 本文是持续更新的项目主叙事。每轮详细需求与验收以对应 PRD 为准。

## 1. 项目业务背景

企业内网、大学/研究机构官网、政府门户和产品文档站有一组共同特征：信息源相对可信，但网页数量大、层级深、跨页关联多，而且招生、政策、人员、公告和产品版本持续变化。传统做法要么依赖站内关键词搜索，要么周期性全量抓取并重建 RAG 索引。

全量离线建库适合构建稳定基线，却难以覆盖刚发布页面和长尾查询；完全依赖在线搜索又会重复抓取，不能积累组织知识，也缺乏引用与检索轨迹。因此系统采用“离线结构化底座 + 查询驱动在线探索 + 增量知识更新”的混合模式：

```text
可信机构 Web
  -> 离线/历史结构化知识底座
  -> Query-driven Agent 在线探索缺口
  -> Observation 立即支撑本次回答
  -> 异步增量入图供未来查询复用
```

## 2. 原始 PolyUQuest 算法底座

PolyUQuest 首先是一个结构感知图增强 RAG 算法框架，而不是一个“需要修复固定 Pipeline”的旧系统。它针对 HTML 扁平分块丢失页面层级、跨页关系和证据溯源的问题，构建：

- WebPage：页面及超链接导航关系；
- DOM Block：标题路径、父子层级和原文证据；
- Entity：跨页实体与语义关系；
- Dense、BM25、图遍历和 Reranker 融合检索；
- 直接检索、页面导航、实体推理及混合模式路由；
- 来源 URL、DOM 标题路径和实体链接的细粒度引用。

该阶段验证的是“网页结构和图关系是否改善复杂机构网站问答”。工程化阶段保留这一算法能力，将其封装为 Agent 可调用的 Search/Expand/Snapshot/Publish 工具。

## 3. 为什么从 Web 场景构建 Agent

Web 天然适合作为在线知识来源：它是组织信息对外/对内发布的最终载体，有明确 URL、链接结构、更新时间和 HTTP 缓存协议。基于 Web 的 Agent 不需要先假设所有页面都已离线完成建图，而是可以在已有知识不足时沿可信入口探索。

但在线探索引出新的工程问题：

1. 如何判断已有证据是否足够，避免无界浏览？
2. 如何约束探索域名、轮次、深度、页面数和耗时？
3. 新抓取内容只服务本次回答，还是应该进入长期知识库？
4. Neo4j 与 Qdrant 无法共享事务，部分写入如何恢复？
5. 相同页面未变化时如何避免重复抓取和向量化？
6. 入图是否应该阻塞用户回答？

后续迭代围绕这些问题逐步演进，而不是一次性堆叠复杂中间件。

## 4. 系统演进

### 迭代 1：查询驱动的自主检索问答 MVP

将原算法模块封装成 Search、Expand、Fetch 等工具，引入 Evidence Evaluator 和预算控制 Agent Loop。知识库命中不足时，Agent 沿图关系和可信站点入口继续探索，并向前端输出可审计的动作摘要，而非暴露模型隐藏思维链。

解决的问题：从 `question -> answer` 的算法调用，扩展为能识别知识缺口、在线找证据、受预算约束并可观测的业务程序。

### 迭代 2：探索内容增量入图

将 Fetch 产生的临时 Observation 通过 Stage/Publish Patch 写入 Neo4j/Qdrant，区分 create、update、unchanged、repair；通过 content hash、URL 锁、快照对账和读后校验实现幂等与最终一致。

解决的问题：在线探索不再是一次性成本，新知识能服务后续查询。

### 迭代 3：可靠性与可运维性

使用 SQLite 持久化 Observation/Patch Ledger；启动时恢复中断 Patch；使用 ETag/Last-Modified 和 HTTP 304 复用已入库快照；拆分 liveness/readiness/dependencies；图统计区分 fetched page 与 link stub。

解决的问题：服务重启不丢恢复线索、未变化页面不重复计算、运行状态能够被部署系统正确判断。

### 迭代 4：异步增量入图

在 Stage 与 Publish 之间加入 SQLite Outbox。Agent 只提交任务即可继续生成回答；Index Worker 通过 lease 消费，失败指数退避，达到阈值进入 dead letter，并提供任务查询和人工重试接口。

解决的问题：用户延迟不再被 Embedding 和双存储写入尾延迟绑架，同时保留可恢复、可审计的最终一致更新。

### 迭代 5：通用页面质量门控

在 Fetch 与 Stage 之间增加与站点和业务无关的质量决策层，将“本轮回答能否使用”与“是否值得长期入库”拆成两个维度。门控基于正文规模、结构块、HTML 文本密度、链接密度、重复块、查询覆盖率和来源元数据输出 `index / evidence_only / discard`，不增加 LLM 调用。

解决的问题：在线探索不再把每个成功响应都无条件写入知识库；薄内容可临时支撑回答但不污染长期索引，空壳或无相关证据页面被丢弃。所有决策进入 SQLite 审计账本和前端探索轨迹，策略阈值可配置、可回放。

### 迭代 6：自适应知识新鲜度与页面生命周期

为已索引页面建立持久化 lifecycle target，通过页面变化历史、访问热度、质量价值与失败状态动态计算 TTL。Freshness Worker 使用 ETag/Last-Modified 条件请求后台再验证：304 或相同 hash 只更新 `last_validated_at`，内容变化才重新经过质量门控和异步 Outbox。

解决的问题：知识更新不再依赖用户提出“最新”问题，也不需要固定周期全量重建；稳定页面自动降低刷新频率，变化或热点页面更快复查。刷新任务具有 lease、失败退避、重启恢复、quarantine、pause/resume 和运维统计，检索会过滤不再可信的旧快照。

### 迭代 7：DOM Block Diff 与局部增量索引

Freshness Worker 发现页面变化后，不再默认重做整页所有向量。发布器先对当前图快照和新 Observation 生成确定性 Diff，区分 unchanged、metadata changed、modified、relocated、added 和 deleted；只为 modified/added 或缺失向量的块生成 Embedding，DOM 路径位移但语义未变时复用旧向量。页面标题和描述不变时同样复用页面向量。

每次 Patch 对应 SQLite PageVersion，记录 Diff、状态、Embedding/复用/写入计数和失败原因。发布仍按“先写新数据、后清理旧数据、最后读后校验”执行，Version 与 Patch 任一失败都会进入 repair_required，避免出现知识已部分改变但审计显示成功的情况。

解决的问题：局部网页变化不再放大全页模型调用与双存储写入；更新成本可量化，DOM 位移可识别，失败版本可追踪并可幂等修复。

### 迭代 8：增量实体关系更新与事实时态

Block 当前态更新后，进一步让 Entity/Relation 层与网页证据同步演进。系统只对 modified/added Block 执行一次页面级抽取；deleted/modified Block 撤销旧事实支持，relocated Block 迁移证据 ID 而不调用 LLM。关系只有在最后一个支持块消失时才从 Neo4j/Qdrant 当前态退休，避免局部更新误删仍由其他页面或段落支持的事实。

抽取核心从 PolyU/大学固定 ontology 改为领域无关 typed contract，支持企业产品、服务、文档、政策、组织与站点扩展类型。Neo4j/Qdrant 继续只服务当前事实；SQLite `fact_versions` 保存 valid time、transaction time、来源 Block 和引入/退休 PageVersion，并用事件表保证重试后的指标幂等。PageVersion 持久化 KnowledgeDelta，跨存储中途失败时无需重新调用 LLM；Index Worker heartbeat 保护长抽取任务的 lease 所有权。

解决的问题：页面文本、结构块和知识图关系不再出现版本错层；当前检索不会读到已失效事实，同时历史演进、故障重放和成本指标可审计。

### 迭代 9：跨存储一致性治理与可审计修复

单次发布的读后校验不能覆盖长期运行中的人工操作、存储恢复、历史迁移和极端中断。系统新增独立 Reconciliation 工作流，扫描 Neo4j/Qdrant 的 Page、Block、Entity、Relation，核对 Relation 证据 payload、SQLite active Fact、repair-required PageVersion 与 dead-letter Job，并把 finding 和 repair action 持久化。

扫描默认是无副作用 dry-run。可定位到持久化 Observation/Patch 的漂移生成去重后的 replay action，dead letter 生成 retry action；缺少来源或涉及事实历史冲突的项目进入 manual review。执行必须显式确认，以 SQLite CAS claim 防止重复执行，并在真正执行前再次检查 running Job，避免运维修复与在线 Worker 竞争。

解决的问题：系统不再只知道“某次写入是否成功”，而能持续回答当前知识是否跨存储一致、哪些差异可安全自动恢复、哪些必须人工判断，以及每次修复的 before/after 和失败原因。

## 5. 当前总体架构

```text
Next.js UI
  -> FastAPI Agent API / SSE
      -> Query Profile + Evidence Evaluator + Budget Controller
      -> PolyUQuest Search Tool
          -> Qdrant Dense + BM25 + Neo4j Graph + Reranker
      -> Expand / Trusted Fetch / Snapshot
      -> Generic Page Quality Gate
          -> index / evidence_only / discard
      -> Answer Composer + citations
      -> Observation Ledger
      -> GraphPatch -> SQLite Outbox

Index Worker
  -> lease heartbeat / retry / dead letter
  -> PublishPatchTool
  -> PageVersion + deterministic DOM Block Diff
  -> changed-only Embedding + relocated vector reuse
  -> changed-block extraction + KnowledgeDelta
  -> current Entity/Relation graph + bitemporal Fact Ledger
  -> Neo4j WebPage-Block-Entity graph
  -> Qdrant vectors
  -> read-after-write verification

Freshness Worker
  -> adaptive TTL + durable lease
  -> HTTP conditional revalidation
  -> unchanged: validate only / changed: quality gate + Outbox
  -> retry / quarantine / pause-resume lifecycle

Reconciliation Workflow
  -> read-only Neo4j/Qdrant/FactVersion/PageVersion/Outbox snapshot
  -> persisted findings + deduplicated RepairPlan
  -> explicit confirm + CAS action claim
  -> replay Patch / retry dead letter
  -> before/after audit + follow-up verification scan
```

## 6. 技术选型理由

- FastAPI：工具边界清晰、Pydantic 契约和 SSE 适合 Agent 服务。
- Neo4j：表达网页链接、DOM 归属、实体跨页关系与图遍历。
- Qdrant：承载 Page/Block/Entity 向量检索并支持 payload 过滤。
- BGE-M3 + BM25：兼顾语义匹配与机构名称、课程编号、政策术语等精确匹配。
- Reranker：在多路召回后统一相关性排序。
- SQLite WAL：当前单机 MVP 无需新增基础设施，适合作为 Observation/Patch/Outbox 持久日志；吞吐提升后再迁移 PostgreSQL/Redis Streams。
- Next.js/TypeScript：展示回答、引用、Agent 动作和知识图谱。
- HTTP Conditional Request：复用 Web 原生 ETag/Last-Modified，而不是自造刷新协议。
- Reconciliation Ledger：将数据漂移发现与修复执行分离；SQLite CAS 满足单机 MVP 的动作去重，未来可平滑迁移 PostgreSQL advisory lock。

## 7. 核心工程原则

1. 本次回答与长期入库分离：Observation 可立即回答，知识发布最终一致。
2. 工具输出均有来源、时间和 content hash，不把无溯源文本写入知识库。
3. Agent 探索必须有域名、深度、页面、轮次和耗时预算。
4. Neo4j/Qdrant 写入采用幂等 Patch、读后校验、周期 reconciliation 和可恢复任务，不伪装成跨库强事务。
5. 前端展示可审计决策摘要和工具结果，不展示隐藏思维链。
6. 每轮需求先写 PRD，代码后有测试、真实验证、迭代记录与 Git 提交。

## 8. 后续路线

- PageVersion 回滚、Reconciliation 审批与 Diff 可视化；
- 独立 Worker、PostgreSQL Outbox/Redis Streams 和分布式锁；
- OpenTelemetry/Prometheus 与运营面板；
- 权限优先级恢复后加入租户隔离、审批和敏感数据治理；
- 事实冲突裁决、来源优先级与时态查询；
- Human-in-the-loop 审批、租户隔离和敏感实体治理。

## 9. 对应文档

- `docs/QUERY_DRIVEN_GRAPH_AGENT_PRD.md`：Agent MVP；
- `docs/AGENT_INCREMENTAL_KNOWLEDGE_PRD.md`：增量入图；
- `docs/AGENT_RELIABILITY_OPTIMIZATION_PRD.md`：可靠性优化；
- `docs/ASYNC_INCREMENTAL_INDEXING_PRD.md`：异步入图；
- `docs/PAGE_QUALITY_GATE_PRD.md`：页面质量门控与知识库污染控制；
- `docs/ADAPTIVE_FRESHNESS_LIFECYCLE_PRD.md`：自适应刷新与页面生命周期；
- `docs/CONSISTENCY_RECONCILIATION_PRD.md`：跨存储一致性扫描、修复计划与执行审计；
- `docs/DOM_DIFF_INCREMENTAL_INDEXING_PRD.md`：DOM Diff、局部向量更新与页面版本；
- `docs/INCREMENTAL_KNOWLEDGE_TEMPORALITY_PRD.md`：增量实体关系与事实时态；
- 本地 `docs/ITERATION_QUERY_DRIVEN_AGENT_MVP.md`：逐轮问题、修改和验证记录。

简历写法和面试准备将在架构能力稳定、关键指标补齐后写入本文后续章节，避免把尚未验证的工程指标提前包装为成果。
