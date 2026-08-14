# PolyUQuest 在线知识闭环业务 E2E 门禁 PRD

> 迭代 16 · 2026-08-14 · 状态：实施中

## 1. 业务背景

PolyUQuest 最初解决的是机构网站 HTML 被扁平化后，页面层级、跨页关联和证据来源丢失的问题。前十五轮已在这一算法底座上完成受控在线探索、质量门禁、增量入图、DOM Diff、知识时态、Outbox、知识刷新、跨存储修复、Telemetry、安全与生产制品门禁。

这些能力分别有单元测试和局部故障注入，但企业交付真正关心的是一条完整业务承诺：当知识库没有答案时，系统能否探索受信任网站、立即用临时证据回答、可靠地把新知识写入图与向量库；之后相同问题能否直接复用；来源页面变化和部分存储失败时，最终状态能否恢复且不重复。

因此本轮把上述承诺变成可重复、无外部模型费用、能在 GitHub Linux runner 自动执行的业务 E2E 门禁。

## 2. 修改前现状与主要问题

### 2.1 单元测试证明分支，不证明组合结果

`test_agent_orchestrator.py`、`test_graph_patch.py`、`test_index_worker.py` 和 `test_freshness_worker.py` 使用 Fake Store/Fake Tool 精确覆盖边界条件。这种测试必要且快速，但 Fake 返回的“写入成功”不能证明真实 Cypher、Qdrant payload、向量维度、SQLite 状态迁移和 read-after-write 合同能够同时成立。

### 2.2 容器合同不执行业务查询

迭代 14/15 的镜像门禁证明镜像能以 non-root/read-only 运行、模块与 Profile 一致、没有阻断级漏洞；它不执行 Query → Explore → Persist → Reuse，因此“容器可启动”不能替代“业务可完成”。

### 2.3 现有评测不验证写路径

确定性评测门禁验证回答状态、探索行为和离线质量指标，但输入是冻结 Response，不会启动 Neo4j/Qdrant，也不验证 Observation、Patch、Index Job、PageVersion、Entity/Fact 和向量是否一致。

### 2.4 真实模型会让工程门禁不确定

直接在 CI 调用远程 LLM、Embedding 和 Reranker 会引入 API Key、费用、限流、模型升级和网络抖动。工程 E2E 的目标是验证编排与持久化合同，而不是再次测量模型语义质量；两类门禁必须分离。

### 2.5 缺少知识复用与增量更新的可执行证据

当前没有一个制品同时证明：

- 首次查询确实抓取并入库；
- Worker 重启后可恢复部分写失败；
- 第二次查询不抓网页；
- 页面变化只重算变化 Block；
- 旧 Fact 退休、新 Fact 生效；
- Patch 重放不会产生重复对象。

## 3. 迭代目标

1. 使用真实 Neo4j、Qdrant 与 SQLite 账本执行完整业务场景；
2. 使用真实 `QueryDrivenAgent`、`FreshnessWorker`、`IndexWorker`、`PublishPatchTool` 和检索链路；
3. 仅将 LLM/Embedding/页面来源替换为确定性、无密钥的测试边界；
4. 覆盖冷启动、部分失败恢复、知识复用、页面更新、局部向量化、事实时态与幂等重放；
5. 输出机器可读 JSON artifact，失败时也保留已完成阶段与错误；
6. 在相关代码变更的 PR/push 上自动执行，成为发布前业务门禁。

## 4. 非目标

- 不用工程 E2E 取代 CIKM 论文指标或冻结集评测；
- 不在 CI 调用 DeepSeek、SiliconFlow、Qwen 或其他收费模型；
- 不放松生产 `fetch_httpx_document` 的 SSRF/私网地址防护；
- 不为测试新增生产 API 的匿名入口；
- 不在共享本地数据库执行清库；
- 不验证浏览器 UI、GPU/CUDA、本地模型权重和公网搜索引擎；
- 不把测试站点内容写成 PolyU 特定规则。

## 5. 场景与验收故事

### 5.1 冷启动探索

给定一个包含随机场景标识的“生产数据库访问申请流程”问题，空知识库检索无结果。Agent 从配置的受信任测试域选择唯一入口，通过真实 HTTP 获取结构化页面，经 Page Quality Gate 判为 `index`，使用临时 Evidence 回答，并生成一个持久化 Index Job。

### 5.2 双存储部分失败与进程重启

第一次发布在 Neo4j 已写页面、Qdrant 首次 upsert 前注入一次性故障。Job 必须进入 `retry`，Patch/PageVersion 保持 `repair_required`。随后用相同 SQLite 文件重建 Observation/Patch/Outbox/Version Store 和新的 Index Worker，第二次执行必须完成修复，而不是重新抓取或丢失任务。

### 5.3 热查询知识复用

发布完成后再次提交相同问题。真实 Block ANN 与 Neo4j enrichment 必须返回已入库 Evidence；Agent 回答时 `pages_fetched=0`，测试站点请求计数不增加，也不生成新的 Index Job。

### 5.4 来源页面变化

测试站点切换到版本 2，ETag 改变，审批方由 Platform Team 变为 Security Review Board。`FreshnessWorker` 读取真实 Page Snapshot、条件请求页面、通过质量门禁并生成增量 Job。

### 5.5 DOM Diff 与知识时态

更新发布后必须满足：

- 页面版本为 `published`；
- 至少一个稳定 Block ID 与向量保持不变；
- `block_embeddings < new_count`，证明没有全页重算；
- 旧审批关系进入 retired 历史，新审批关系成为 active；
- Neo4j Block/Entity 和 Qdrant Block/Entity 集合一致。

### 5.6 幂等重放

对已经 `published` 的更新 Patch 再执行一次。调用必须立即成功，前后图/向量 inventory 和 Fact 计数保持一致。

## 6. 技术方案

### 6.1 测试拓扑

```text
Deterministic HTTP Site
          │
          ▼
QueryDrivenAgent ──► Observation / Patch / Outbox (SQLite WAL)
          │                              │
          │                              ▼
          │                         IndexWorker
          │                              │
          ▼                              ▼
  temporary Evidence              Neo4j + Qdrant
          │                              │
          └──────── second query ◄───────┘
                                         ▲
Version 2 HTTP Site ──► FreshnessWorker ─┘
```

### 6.2 确定性模型边界

- Embedding：对规范化 token 做稳定哈希投影并 L2 归一化；query 与文档使用同一实现；
- Query Profile：保留生产确定性 baseline，不做 LLM enrichment；
- Frontier arbitration：场景只有一个受信任入口，不触发模型选择；
- Entity/Relation extraction：根据夹具中的业务事实生成带 Block provenance 的严格结构；
- Answer：只从传入 Evidence 组合答案和来源 URL；
- Reranker：CI 使用既有 `first_stage_only` 诊断模式，不访问远程 API。

这些替换只控制非确定边界。检索、图查询、向量读写、质量评分、状态机、Diff、事实时态和恢复逻辑均为生产实现。

### 6.3 测试站点与 SSRF 边界

进程内 `ThreadingHTTPServer` 绑定随机 loopback 端口，提供 v1/v2、ETag、304 和请求计数。Agent 看到的规范 URL 是无端口的受信任测试域；E2E Fetch Adapter 只在测试进程中把该域映射到 loopback origin。

生产 `fetch_httpx_document` 仍拒绝 loopback/private resolution，本轮不会增加关闭 SSRF 检查的环境变量。

### 6.4 隔离策略

- 每次运行生成唯一 scenario token、URL、Entity 和查询；
- 不删除或清空数据库；
- CI 使用全新 service containers；
- SQLite ledger 位于独立 runtime 目录；
- artifact 不记录 API Key、生产查询或用户内容。

## 7. 数据合同与证据制品

`business-e2e-report.json` 包含：

- schema/scenario/code version；
- started/completed/duration/status；
- 每个阶段的状态、耗时和非敏感指标；
- 每项 check 的 expected/actual/ok；
- Agent run、Patch、Job、PageVersion 等审计 ID；
- 首次/二次/更新抓取数；
- Block/Entity/Fact/向量与版本统计；
- 意外异常的类型和脱敏摘要。

任何失败仍通过 `if: always()` 上传 artifact，避免只剩一段截断日志。

## 8. CI 设计

新增独立 workflow：

1. 使用固定版本 Neo4j 与 Qdrant service containers；
2. checkout/setup-uv 均使用供应链策略批准的完整 commit SHA；
3. 安装 locked core + dev 依赖，不安装 `local-ml`；
4. 先执行 E2E 合同单测和 Ruff；
5. 运行 `agent-rag-business-e2e`；
6. 无论成功失败均上传 JSON artifact；
7. Job 设置有限超时和只读 repository permission。

Workflow 对 E2E、Agent、工具、索引、刷新、存储、版本、知识、配置与锁文件相关变更触发，不把昂贵 service job 加到纯文档变更。

## 9. 验收标准

| 合同 | 标准 |
|---|---|
| 冷启动检索 | 初始 Evidence=0 |
| 在线探索 | 首次抓取 1 页，Quality=`index` |
| 即时回答 | 首次状态 answered，包含来源 |
| 异步入库 | 恰好新建 1 个 Job |
| 故障注入 | 第一次发布为 retry/repair_required |
| 重启恢复 | 新 Worker 使用同一 ledger 后 succeeded |
| Read-after-write | WebPage/Blocks/Entities/Facts 与向量存在 |
| 热查询 | 第二次抓取 0 页、Origin 请求增量 0 |
| 页面更新 | Freshness Worker 生成新 content hash 与 Job |
| 局部更新 | 至少 1 个稳定向量相同，embedding 数小于新 Block 数 |
| 事实时态 | 旧关系 retired，新关系 active |
| 一致性 | Block/Entity 跨存储 ID 一致 |
| 幂等 | 重放前后 inventory 与事实计数相同 |
| 外部模型调用 | 0 |

## 10. 文件级修改计划

新建：

- `docs/BUSINESS_E2E_GATE_PRD.md`；
- `docs/BUSINESS_E2E_GATE_RUNBOOK.md`；
- `configs/business_e2e.yaml`；
- `compose.e2e.yml`；
- `src/agent_rag/e2e/`；
- `tests/test_business_e2e_contract.py`；
- `.github/workflows/business-e2e-gate.yml`。

修改：

- `pyproject.toml`：增加 E2E CLI entrypoint；
- `README.md`：说明门禁定位与运行方式；
- 本地迭代总文档：记录问题、修复、实际结果和后续方向。

## 11. 风险与应对

- **服务启动慢**：Runner 在总超时内重试真实 schema 初始化并记录 dependency 阶段；
- **向量误召回**：查询与页面带唯一 token，避免共享环境中的旧数据干扰；
- **测试替身越界**：模块文档和 contract 明确只有模型/测试域映射可替换，真实 Store/Worker 类型写入报告；
- **故障注入位置漂移**：检查第一次 Qdrant mutation 确实失败且 Neo4j 已留下可修复部分状态；
- **全量回归变慢**：真实 service E2E 放独立 workflow，本地单测不要求 Docker；
- **artifact 泄密**：报告只保存测试 ID、计数和脱敏错误，不保存环境变量；
- **镜像版本漂移**：service image 使用明确版本，后续进一步升级为 digest pin。

## 12. 回滚方案

E2E 模块与 workflow 不改变生产请求路径。若门禁基础设施本身故障，可单独回滚 workflow/CLI；不得通过关闭 Agent 持久化合同、放松 SSRF 或删除现有单元测试让门禁变绿。若某个生产变更有意改变合同，应先更新 PRD、配置中的期望值和迁移断言，再修改实现。

## 13. 后续方向

1. 把同一场景扩展到 API 容器与独立 Worker 容器，验证进程间共享 volume；
2. 增加浏览器级 SSE/引用交互 smoke，但不重复数据库断言；
3. 增加 Neo4j/Qdrant 服务重启和网络分区演练；
4. 对 E2E artifact 生成趋势：阶段 P50/P95、失败类型、重试次数；
5. 将通过门禁的 remote 镜像推送 GHCR digest 并生成 provenance/SBOM attestation；
6. 建立 release candidate 晋级规则，把评测、业务 E2E、供应链三类证据绑定到同一 commit；
7. 增加多租户 namespace 和越权隔离 E2E。
