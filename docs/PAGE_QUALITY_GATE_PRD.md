# 页面质量门控与知识库污染控制 PRD

## 1. 迭代目标

在查询驱动的 Web Agent 已具备“实时抓取”和“异步增量入图”能力后，增加一层与站点、机构和业务领域无关的页面质量门控。系统不再把所有成功抓取的页面直接写入长期知识库，而是分别判断：

1. 页面能否作为本轮回答的临时证据；
2. 页面是否具有长期检索价值，值得进入异步索引队列。

本迭代解决的是知识库长期运行后的内容污染、低价值页面膨胀和错误页面传播问题，不改变 PolyUQuest 原有结构感知图增强 RAG 算法。

## 2. 背景与主要问题

前一迭代已经将在线抓取与知识写入解耦为：

`Fetch -> Observation -> Stage Patch -> Durable Outbox -> Index Worker`

该链路保证了写入可靠性，但“抓取成功”并不等于“值得长期入库”。实际机构网站和企业内网中常见以下页面：

- 只有菜单、页脚或链接列表的导航页；
- 正文过短、缺少标题或内容哈希不完整的空壳页；
- 模板块高度重复、正文占比过低的页面；
- 与当前查询存在少量词面重合，但没有稳定复用价值的临时公告；
- 可用于回答当前问题，却不适合长期污染向量库和知识图谱的薄内容。

如果这些内容直接入库，会逐步降低 ANN/BM25 召回质量、增加图节点规模和重排成本，也会使后续回答反复引用低质量证据。

## 3. 产品原则

### 3.1 两个判断相互独立

- `evidence_usable`：本轮回答是否可以使用；
- `persistence_action`：长期存储动作，取值为 `index`、`evidence_only`、`discard`。

不能因为页面不值得长期入库，就自动否定它对当前问题的临时价值。

### 3.2 开放域规则

门控只能依赖通用页面特征：文本量、结构块、重复度、正文/HTML 比例、链接密度、元数据完整度和查询覆盖率。禁止写入 PolyU、院系、博士申请等站点或业务特例。

### 3.3 可解释、可回放、可调参

每次决策必须持久化：策略版本、分数、特征值、原因、页面快照标识和 Agent run_id。所有阈值集中在 `configs/agent.yaml`，不得散落在 Agent 流程代码中。

### 3.4 失败安全

质量门控自身异常时，不允许静默入库。页面仍可按已有证据规则参与当前回答，但长期写入降级为 `evidence_only`，并在 trace 中暴露错误。

## 4. 决策语义

| 决策 | 当前回答 | 长期入图/入库 | 典型情形 |
|---|---|---|---|
| `index` | 可用 | 创建 Patch 并进入 Outbox | 正文充分、结构可用、与查询相关 |
| `evidence_only` | 可用 | 不创建 Patch | 内容较薄但对本轮问题有帮助 |
| `discard` | 不可用 | 不创建 Patch | 无实质正文、无相关块或关键来源信息缺失 |

`304 Not Modified` 不重新执行门控，继续复用已入库快照。

## 5. MVP 质量特征

- `block_count`：结构块总数；
- `relevant_block_count`：本轮选中的相关块数；
- `total_text_chars` / `total_tokens`：可用正文规模；
- `text_html_ratio`：正文字符与原始 HTML 字符比；
- `link_count` / `links_per_1k_chars`：导航密度；
- `duplicate_block_ratio`：规范化后完全重复块占比；该项为评分弱信号，不单独否决入库，因为结构感知父子块可能保留重叠正文；
- `largest_block_share`：单块占正文比例，识别结构异常；
- `query_coverage`：抓取阶段给出的查询覆盖率；
- `title_present` / `content_hash_present`：来源元数据完整性。

MVP 采用确定性加权评分和少量硬性约束，不增加 LLM 延迟。策略输出必须包含命中的原因，而不是只输出总分。

## 6. 功能流程

```text
Fetch Observation
       |
       v
Extract generic quality features
       |
       v
Persist quality decision + emit auditable trace
       |
       +-- discard ------> do not use evidence; do not stage
       |
       +-- evidence_only -> answer may use temporary evidence; do not stage
       |
       +-- index --------> temporary evidence + Stage Patch + Outbox
```

## 7. 数据与接口

### 7.1 持久化记录

SQLite ledger 新增 `page_quality_decisions`，以 `observation_id` 幂等写入，保存：

- `decision_id`、`observation_id`、`run_id`；
- `source_url`、`content_hash`；
- `action`、`evidence_usable`、`score`；
- `policy_version`、`reasons_json`、`features_json`；
- `created_at`。

被接受入库的 Observation metadata 同步写入 `quality_action`、`quality_score` 和 `quality_policy_version`，使图页面节点具备质量来源信息。

### 7.2 运维 API

- `GET /api/indexing/quality/decisions`：查看最近决策，可按 action 过滤；
- `GET /api/indexing/quality/decisions/{decision_id}`：查看单条决策；
- `GET /api/indexing/quality/stats`：查看三类数量和平均分。

### 7.3 Agent 可观测性

新增动作 `polyuquest.evaluate_page_quality`，至少展示：

- action、score、evidence_usable；
- policy_version；
- reasons；
- 关键特征。

探索摘要增加 `pages_index_accepted`、`pages_evidence_only`、`pages_discarded`。

## 8. 验收标准

1. 高质量、相关的通用页面被判定为 `index` 并正常入队；
2. 相关但较薄的页面可参与当前回答，且不会创建 Patch/Job；
3. 空内容、无相关块或来源元数据不完整的页面不会参与回答，也不会入库；
4. 同一 Observation 重放不会产生重复质量记录；
5. 门控异常时降级为 `evidence_only`，trace 可见失败原因；
6. API 可查询决策明细和聚合统计；
7. 原有异步索引、Agent 与前端类型检查测试不回归；
8. 使用至少一个真实、非测试专用网页校验特征和阈值。

## 9. 非目标

- 不在本迭代引入 LLM 页面评分器或业务域分类模型；
- 不做恶意内容/提示注入的完整安全分类；
- 不实现历史页面自动淘汰、版本合并和 TTL 清理；
- 不改变离线全量建图流程；
- 不针对某一机构网站优化阈值。

## 10. 后续优化方向

1. 用人工标注、用户引用反馈和检索命中数据训练轻量质量分类器；
2. 增加近重复页面 MinHash/SimHash，处理镜像页和版本页；
3. 引入 source/page-level 质量画像和域级动态阈值；
4. 对长期低命中内容进行 TTL、降权和可恢复淘汰；
5. 增加提示注入、恶意脚本和异常重定向的安全门控；
6. 建立门控离线评测集，持续跟踪 precision、knowledge pollution rate 与 downstream retrieval gain；
7. 将高成本实体抽取和向量写入进一步按质量等级分层执行。
