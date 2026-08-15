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

### 迭代 10：端到端运行可观测性与回归评测闭环

前九轮已经让系统能够在线探索、异步增量入库、更新事实并治理跨存储漂移，但单次问答的 Action 只存在于响应中，进程级 token 累加器在并发请求间会互相污染，Agent 返回后也无法继续追踪对应 Index Job 的真实发布结果。本轮建立稳定的 `RunTelemetry / TelemetrySpan` 契约，以 root/parent run 串联 Agent、异步索引和 Reconciliation，并用 ContextVar 将 `to_thread` 中的 LLM 调用准确归属到当前请求。

缓存命中同时记录 logical usage 与 billable usage，避免“复现实验成本”和“实际供应商账单”混为一谈；SQLite 仅保存 query hash、长度、状态、耗时、受控计数和 allowlist 标识，不保存问题、回答、Prompt 或网页正文。Telemetry 写失败采用 fail-open 并单独计数，不能拖垮核心问答。API 提供运行列表、详情、P50/P95/P99 聚合和配置化 SLO 判定；样本不足时返回 `insufficient_data`，不伪装成达标。

离线侧新增版本化 JSONL 评测合同和 `run / score / compare` CLI，覆盖状态、必要事实、来源、禁止声明、探索/持久化行为和延迟/页面预算。没有 gold 的指标严格输出 N/A；不同数据集快照或 evaluator contract 的报告禁止直接比较。

解决的问题：系统不仅能“做完一次回答”，还能够定位一次查询慢在哪里、花费在哪个模型阶段、在线发现是否最终入库、发布失败发生在哪次 attempt，并在同一冻结业务集上判断新版本究竟改善质量还是只增加成本。

### 迭代 11：业务评测治理与自动发布门禁

迭代 10 的 `score / compare` 能产出指标，但“有差值”不等于“能作发布决策”：示例 Smoke Case 可能被误当成质量证据，数据集没有审批、版本和文件 Hash，总体均值会掩盖 freshness/abstention 等关键切片回退，CI 也不会因有害版本自动失败。

本轮引入 `EvaluationDatasetManifest`，冻结 dataset ID、版本、Owner、状态、Case 文件 SHA-256、最低样本量、Semantic Gold 比例、必需标签和审批记录。Case 明确区分 `semantic_gold / behavioral_contract / smoke`，并保留 task、tag 与 blocker/critical 等级。评分器只让真实语义 Oracle 产生 quality score；行为合同中的响应状态、探索、持久化和预算进入 operational score；Smoke 不再伪装成答案质量。

Release Gate 同时检查 candidate 绝对下限、baseline 回退、延迟/Token/抓页成本比例、blocker/critical Case 和业务 Slice。缺少样本或指标返回 `insufficient_evidence`，已知硬回退返回 `fail`，两者都不能被当作通过。决策保留 policy、dataset、baseline/candidate report 和 decision fingerprint；wall-clock 时间变化不改变 gate ID。CLI 使用 `0/1/2/3` 区分 pass/fail/证据不足/输入错误，GitHub Actions 通过不访问 LLM、Neo4j、Qdrant 或真实网站的合成 Fixture 执行确定性门禁并上传审计 Artifact。

解决的问题：评测不再只是一张人工阅读的平均分报表，而成为有数据治理前提、能识别长尾回退和成本劣化、能够实际阻止不安全变更进入发布流程的工程控制面。同时明确仓库 Sample 仍是 Draft 示例，不能冒充生产业务 Gold。

### 迭代 12：API 安全边界、RBAC 与隐私安全审计

系统具备在线抓取、增量入图、任务重试、刷新暂停和一致性修复后，匿名 API 已不再符合企业内网部署要求。CORS 只能约束浏览器跨域，`confirm=true` 只能防止误操作，都不能证明调用者身份或权限。本轮建立单租户服务边界：生产环境禁止 `API_AUTH_MODE=disabled` 启动；API Key 只配置 SHA-256 摘要，使用恒定时间比较；Principal 按 `reader / operator / admin` 形成层级授权。

Query/Agent/Graph 需要 reader，Telemetry 与运维状态需要 operator，Retry、Refresh/Pause/Resume、Reconciliation Execute 和 Security Audit 需要 admin；健康探针保持公开。每次受保护请求记录服务生成的 request ID、Principal、角色、route template、状态和授权结果，但不保存原始 Key、Key 请求摘要、Query String、问题、回答、正文或 Client IP。审计 SQLite 使用显式连接关闭、retention purge 和 fail-open recorder，磁盘故障只累计 dropped write，不影响业务响应。

解决的问题：部署方可以阻止匿名生产实例、实施最小权限并追溯有副作用操作，同时不会为了审计扩大敏感数据面。浏览器端明确通过企业 SSO/BFF/API Gateway 代理，不把静态服务 Key 暴露在 Next.js Bundle。本轮不冒充完整 IAM：终端用户认证、多租户行级隔离、OIDC、Key 托管与分布式限流仍属于后续演进。

### 迭代 13：生产部署基线与独立 Worker

补齐 API、Worker、Next.js、Neo4j、Qdrant 的单机生产 Compose；API 与 Worker 使用同一
后端镜像、不同 entrypoint，关闭 API 内嵌后台任务。容器采用 non-root、read-only、能力
裁剪和 internal network，生产配置缺认证、数据库密码或模型凭据时 fail-fast。Worker
停止领取后在 grace period 内完成当前工作；冷备份包含校验和、确认式恢复与恢复后验证。

解决的问题：算法与业务能力从开发机启动方式演进为可重复部署、独立扩缩、可停机、
可备份恢复和可回滚的生产基线。

### 迭代 14：容器制品与供应链 CI 门禁

GitHub Linux runner 使用 BuildKit 真实构建前后端镜像，在 UID/GID 10001、只读根文件系统
下运行容器合同和前端 smoke；生成 CycloneDX SBOM、漏洞报告、inspect 与 build metadata。
Actions 和扫描器均固定版本/SHA，对有修复版本的 CRITICAL 漏洞阻断，并上传可追溯制品。

解决的问题：静态 Dockerfile 检查不再被当作“镜像可构建、可运行、安全”的证据，发布
结果能够关联具体 commit 并由机器复验。

### 迭代 15：运行能力分层与默认镜像瘦身

远程 embedding/reranker 的企业 serving 节点无需携带完整 Torch 训练/评测依赖。系统将
后端拆为默认 `remote` 与显式 `local-ml` 两种 capability profile；移除未使用的
FlagEmbedding，镜像写入不可伪装的 profile marker，配置与制品能力不匹配时启动前失败。
CI 分别构建、运行、扫描两个 profile，并为默认 remote 镜像设置体积和依赖禁入预算。

解决的问题：可选本地模型能力不再放大默认镜像、构建时间、分发成本、SBOM 与攻击面，
同时保留无外部 embedding 服务场景的显式制品。

### 迭代 16：在线知识闭环业务 E2E 门禁

使用真实 Neo4j、Qdrant、SQLite 和生产 Agent/Worker 类，模型与网页边界替换为确定性
Fixture。场景覆盖冷查询在线探索、临时证据回答、异步入库、首次 Qdrant 故障、进程重建
后 retry、热查询零抓取、页面 v2 更新、changed-only embedding、旧事实退休、新事实生效
及 Patch 幂等重放，输出机器可读 artifact。

解决的问题：单元测试和镜像 smoke 之外，首次形成“知识缺口 → Web 探索 → 回答 → 入库
→ 复用 → 更新 → 恢复”的真实双存储业务证据。

### 迭代 17：生产拓扑 E2E 与 Worker 租约接管

进一步把场景拆为独立 API、Worker、Fixture、Neo4j、Qdrant 容器。查询必须经真实
HTTP/SSE 和 reader RBAC；API 只 enqueue，Worker 跨进程消费共享 SQLite WAL。门禁在
任务被 claim 后 SIGKILL Worker，等待 lease 过期，由新实例二次 claim 并完成发布；同时
验证 Worker heartbeat/health、热查询复用、页面刷新与安全审计关联。

解决的问题：直接调用 Python 类的 E2E 被提升为生产网络、进程、鉴权、流协议和故障接管
证据，证明 API/Worker 分离不是只存在于 Compose 声明中。

### 迭代 18：浏览器安全 BFF 与 SSE 交付

浏览器固定访问同源 `/api`；Next.js Route Handler 使用显式 reader route/method allowlist，
校验 Origin、实际 body 大小和 timeout，从 `root:10001/0440` 的 file secret 注入 reader
key，不转发浏览器 Cookie/Authorization/X-API-Key。BFF 逐 chunk 传递 SSE、关联请求 ID、
清洗上游错误，并把 downstream cancel 传播到 FastAPI。

Linux production E2E 使用受控 upstream 验证 403/404/413 不触达后端、3 chunk 事件顺序、
5xx 清洗、取消、完整 key 的 hash 匹配以及 9 个客户端 script 无 canary/内部地址/public
API 变量。门禁最终 6/6 checks 通过；排查过程同时发现并修复 Compose file secret 权限、
测试进程误读 secret 和换行导致 digest 不一致三类真实交付问题。

解决的问题：生产用户终于经过 Browser → Next.js BFF → FastAPI SSE 的真实交付链；长期
服务凭据不进入 bundle，frontend 能通过 internal network 调用 API，但终端用户身份仍明确
交给企业 SSO/ingress，未把共享 reader key 包装成完整 IAM。

### 迭代 19：持久化 Agent Run 与 SSE 断线恢复

把长耗时 Agent 执行从单次 StreamingResponse 生命周期中拆出：提交接口以 Idempotency-Key 创建
SQLite WAL Run，独立 Worker 通过 lease/heartbeat/attempt 执行，普通事件持续追加，最终 done、
result 与 completed 在同一事务提交。浏览器只把 SSE 作为可重连观察通道；同页断网携带
Last-Event-ID 补发，刷新后按 run_id 重放完整可审计轨迹，显式 Stop 才写持久化取消。

生产 Compose 强制 API 关闭 Agent Run loop、Worker 开启并共享 runtime volume；BFF 只开放严格
run_id 路径，并只转发校验后的 Idempotency-Key/Last-Event-ID。实现保留旧同步/流接口作为回滚
面，明确采用 at-least-once，并复用 GraphPatch/Outbox 幂等控制重试副作用。

解决的问题：代理重启、网络切换、页面刷新和前端发布不再直接丢失已经发生费用的在线探索；
任务状态、事件、取消和最终结果有稳定 run_id，可恢复执行与可审计交付首次形成闭环。

## 5. 当前总体架构

```text
Browser UI（无 service key）
  -> TLS / Enterprise SSO Ingress
  -> Next.js same-origin BFF
      -> reader route allowlist + Origin/body/timeout
      -> runtime file secret + SSE/cancel passthrough
  -> FastAPI API-key Principal + reader/operator/admin RBAC
      -> body-free Security Audit Ledger
  -> Durable Agent Run API / replayable SSE
      -> SQLite WAL Run + Event Store
      -> Idempotency-Key / Last-Event-ID / explicit cancel

Agent Run Worker
  -> lease / heartbeat / retry / terminal transaction
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

Telemetry + Evaluation
  -> root Agent run -> child Index/Reconciliation runs
  -> ContextVar stage-level logical/billable LLM usage
  -> SQLite run/span ledger -> stats + configurable SLO API
  -> governed Manifest + frozen JSONL scenarios
  -> semantic/behavior/smoke scoring + business slices
  -> deterministic baseline/candidate release gate + CI artifacts
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
- Manifest + Policy-as-Code：把 Gold 数据审批、样本充足性、质量/成本阈值和关键切片规则纳入版本控制；确定性 Fixture 负责验证门禁机制，真实业务 Gold 由部署方独立治理。
- Hash-only API Key + FastAPI Dependency：适合作为单租户服务到服务认证 MVP；角色依赖显式附着路由，安全审计只保存 route template 和授权元数据。终端用户身份交给 BFF/企业 SSO，后续再迁移 OIDC/JWT 与多租户 Scope。
- Next.js Server-only BFF：浏览器保持同源且不接触 raw service key；Route Handler 只开放
  reader 能力并透明传递 SSE。工作负载身份与最终用户身份分离，SSO/OIDC 仍由受控入口负责。
- Durable Agent Run + SQLite WAL：在单机 Compose 阶段用短事务、lease 和可重放事件把任务生命周期
  与 HTTP 连接解耦；明确采用 at-least-once，跨主机/多租户后迁移 PostgreSQL 与事件总线。
- Durable Chaos Topology：把真实 Next.js BFF、FastAPI、共享 Run Store 与独立 Worker 放入同一 Linux
  场景，验证浏览器断流不取消任务、Worker SIGKILL 后 lease reclaim、游标重放与幂等重提；health/stats
  将 Worker capability、队列年龄、应用重试和租约接管变成可告警的机器合同。
- Atomic Admission Control：将幂等查询、部署预算、active/waiting 计数、拒绝指标和 Run 插入放进同一
  SQLite 写事务，避免并发先查后写穿透；429/Retry-After 经 BFF 安全透传，Health 同时表达队列年龄与容量利用率。
- Docker capability profile + Policy-as-Code：默认 remote 镜像不携带本地 ML 栈；Linux CI
  同时验证 non-root/read-only 运行合同、SBOM/CVE 和真实业务/拓扑/BFF 场景。

## 7. 核心工程原则

1. 本次回答与长期入库分离：Observation 可立即回答，知识发布最终一致。
2. 工具输出均有来源、时间和 content hash，不把无溯源文本写入知识库。
3. Agent 探索必须有域名、深度、页面、轮次和耗时预算。
4. Neo4j/Qdrant 写入采用幂等 Patch、读后校验、周期 reconciliation 和可恢复任务，不伪装成跨库强事务。
5. 前端展示可审计决策摘要和工具结果，不展示隐藏思维链。
6. 每轮需求先写 PRD，代码后有测试、真实验证、迭代记录与 Git 提交。

## 8. 后续路线

- PageVersion 回滚、Reconciliation 审批与 Diff 可视化；
- PostgreSQL durable queue/Outbox、Redis Streams 和多副本分布式执行；
- 将现有稳定 telemetry contract 导出到 OpenTelemetry/Prometheus 与运营面板；
- OIDC/JWT、企业 SSO、Key 托管轮换、租户/Domain Scope 与端到端数据隔离；
- 事实冲突裁决、来源优先级与时态查询；
- Human-in-the-loop 审批、租户隔离和敏感实体治理。
- 建立 50～100 题以上经双人复核的真实业务 Gold、冻结网页证据快照和不可变 baseline registry，再将离线门禁衔接 shadow/canary 与自动回滚。

## 9. 对应文档

- `docs/QUERY_DRIVEN_GRAPH_AGENT_PRD.md`：Agent MVP；
- `docs/AGENT_INCREMENTAL_KNOWLEDGE_PRD.md`：增量入图；
- `docs/AGENT_RELIABILITY_OPTIMIZATION_PRD.md`：可靠性优化；
- `docs/ASYNC_INCREMENTAL_INDEXING_PRD.md`：异步入图；
- `docs/PAGE_QUALITY_GATE_PRD.md`：页面质量门控与知识库污染控制；
- `docs/ADAPTIVE_FRESHNESS_LIFECYCLE_PRD.md`：自适应刷新与页面生命周期；
- `docs/CONSISTENCY_RECONCILIATION_PRD.md`：跨存储一致性扫描、修复计划与执行审计；
- `docs/END_TO_END_OBSERVABILITY_EVALUATION_PRD.md`：端到端运行度量、SLO 与离线回归合同；
- `docs/EVALUATION_GOVERNANCE_RELEASE_GATE_PRD.md`：评测数据治理、业务切片和自动发布门禁；
- `docs/API_SECURITY_RBAC_AUDIT_PRD.md`：生产认证、三级 RBAC 与隐私安全审计；
- `docs/PRODUCTION_DEPLOYMENT_BASELINE_PRD.md`：生产 Compose、独立 Worker 与备份恢复；
- `docs/CONTAINER_SUPPLY_CHAIN_GATE_PRD.md`：镜像合同、SBOM/CVE 与供应链门禁；
- `docs/RUNTIME_PROFILE_IMAGE_OPTIMIZATION_PRD.md`：remote/local-ml 能力分层与镜像瘦身；
- `docs/BUSINESS_E2E_GATE_PRD.md`：真实双存储在线知识闭环；
- `docs/PRODUCTION_TOPOLOGY_E2E_PRD.md`：HTTP/SSE、独立进程与 Worker lease 接管；
- `docs/BROWSER_BFF_SSE_PRD.md`：浏览器同源 BFF、凭据隔离与 SSE 交付；
- `docs/DURABLE_AGENT_RUN_PRD.md`：持久化 Run、Worker lease、事件重放与显式取消；
- `docs/DURABLE_AGENT_RUN_CHAOS_E2E_PRD.md`：真实 BFF 到 Worker 的故障注入、lease 接管与运行健康；
- `docs/AGENT_RUNTIME_ADMISSION_CONTROL_PRD.md`：原子准入、部署预算、429 过载合同与容量指标；
- `docs/DOM_DIFF_INCREMENTAL_INDEXING_PRD.md`：DOM Diff、局部向量更新与页面版本；
- `docs/INCREMENTAL_KNOWLEDGE_TEMPORALITY_PRD.md`：增量实体关系与事实时态；
- 本地 `docs/ITERATION_QUERY_DRIVEN_AGENT_MVP.md`：逐轮问题、修改和验证记录。

简历写法和面试准备将在架构能力稳定、关键指标补齐后写入本文后续章节，避免把尚未验证的工程指标提前包装为成果。
