# 增量实体关系更新与事实时态 PRD

## 1. 迭代目标

在 DOM Block Diff 已能局部更新页面结构与向量后，补齐 Entity/Relation 层的增量知识演进：只对语义变化或新增的 Block 执行实体关系抽取，迁移 DOM relocation 的证据来源，撤销删除/修改块不再支持的事实，并保存可查询的事实版本历史。

本轮同时移除在线抽取核心契约中的 PolyU/大学领域硬编码，使同一机制可用于企业内网、政府门户、产品文档和机构网站。

## 2. 修改前现状

1. Agent 在线 Publish 只写 WebPage、Block 和 LINKS_TO，不更新 Entity/RELATES_TO；
2. 页面正文变化后，Block 检索是新内容，但实体导航仍可能命中旧人员、旧政策或旧产品关系；
3. 删除旧 Block 会移除结构节点，但关联的 EXTRACTED_FROM 和关系来源缺少版本级处理；
4. 现有抽取 Prompt 写死 university/PolyU 实体类型，无法作为通用 Web 知识组件；
5. Neo4j RELATES_TO 只保存当前聚合关系，没有“何时有效、何时被新页面撤销”的审计历史；
6. 全页重抽取会重复消耗 LLM、Entity Embedding 和 Relation Embedding。

## 3. 业务语义

知识来源是网页证据，而不是模型自由生成的事实。一个当前关系由一个或多个 Block 支持：

- 修改/删除 Block：先撤销该 Block 对旧 mention/fact 的支持；
- 新增/修改 Block：从新文本抽取 mention/fact 并添加支持；
- relocated Block：文本未变，只把来源 ID 从 old block 迁移到 new block，不调用 LLM；
- 关系仍有其他未变化 Block 支持时继续有效；
- 最后一个支持消失时，Neo4j 当前关系失效，SQLite 历史版本关闭；
- 相同事实以后重新出现时创建新的有效期，而不是覆盖旧历史。

## 4. 通用抽取契约

核心实体类型调整为开放字符串，Prompt 给出通用建议类型：PERSON、ORGANIZATION、PRODUCT、SERVICE、PROGRAMME、DOCUMENT、POLICY、EVENT、LOCATION、DATE、TOPIC、OTHER。关系类型使用简短 snake_case，不限定大学关系集合。

约束：

1. 所有关系端点必须同时出现在 entities；
2. entity/relation 必须带 `source_block_refs`；
3. 不从页面导航、模板或无正文区推断事实；
4. 描述必须由给定 Block 支持；
5. 现有 canonical entity 仅作为对齐候选，不能诱导抽取不存在的事实。

站点特定 ontology 可通过 connector 配置补充，核心代码不出现 PolyU 院系/学位规则。

## 5. 增量规划

```text
PageVersion BlockDiff
  -> affected old blocks = modified + deleted + relocated.old
  -> extraction blocks   = modified + added
  -> load old mentions/facts touching affected old blocks
  -> extract changed blocks once at page level
  -> exact/alias-first entity resolution against current graph
  -> build deterministic KnowledgeDelta
       mentions to replace
       current facts with final source_block_ids
       retired fact keys
       new entities
  -> persist delta in PageVersion before external writes
```

Diff 重放优先读取 PageVersion 中已保存的 KnowledgeDelta，避免重试再次调用 LLM。

## 6. 当前图与历史账本

### 6.1 Neo4j 当前态

- Entity：当前 canonical 实体；
- EXTRACTED_FROM：当前 mention 到当前 Block；
- RELATES_TO：当前仍有至少一个证据块支持的关系；
- `source_block_ids` 是去重后的当前支持集合；
- 关系增加 `valid_from`、`last_observed_at`、`page_version_id`；
- 最后一个支持消失时删除当前 RELATES_TO，避免检索读取失效事实。

### 6.2 SQLite 历史态

新增 `fact_versions`：

- `fact_version_id`、稳定 `fact_key`；
- source/target entity 与 relation type；
- description、keywords、weight、source block IDs；
- `valid_from` / `valid_to`（来源有效时间，使用 fetched_at）；
- `transaction_from` / `transaction_to`（系统记录时间）；
- introduced/retired page version；
- active/retired 状态。

同一 fact_key 的证据集合或内容变化时关闭旧版本并创建新版本；完全相同则幂等跳过。

## 7. 发布顺序与失败恢复

1. Block Diff 与 KnowledgeDelta 在任何外部写入前持久化；
2. 抽取失败时不发布新页面，Outbox 按既有策略重试，避免新 Block 与旧 Entity 层长期错配；
3. 先 upsert 新 Entity/Relation 和向量，再撤销旧来源/关系；
4. Neo4j 知识应用使用单事务；Qdrant 使用幂等 upsert/delete；
5. 对受影响 Block 的 mention 做精确集合校验，并校验 fact 的证据块集合、向量 payload 与退休状态；
6. 图与向量成功后，原子推进 fact_versions，保存 `fact_history_applied` checkpoint，再发布 PageVersion/Patch；
7. 任一步失败，Patch/PageVersion 均为 repair_required，重放使用已保存 KnowledgeDelta；
8. Freshness lifecycle 只在 Block 与 Knowledge 两层均成功后更新；
9. Worker 发布期间按 lease/3 续租；旧 Worker 丢失租约后只能停止并读取当前任务状态，不得覆盖新 Worker。

## 8. 向量策略

- 新 Entity 或当前 Entity 向量缺失：生成 Entity embedding；
- 已有 canonical Entity 不因单页短描述覆盖全局描述，也不重复 embedding；
- 新事实或事实语义变化：生成 Relation embedding；
- 仅 `source_block_ids` 迁移/增减：复用 Relation vector，只更新 payload；
- retired fact：删除当前 Qdrant relation point；
- 孤立 Entity 仅在没有 mention、没有当前关系时回收，避免误删跨页共享实体。

## 9. 运维 API 与指标

- `GET /api/indexing/facts?status=active|retired&source_url=...`；
- `GET /api/indexing/facts/{fact_key}/history`；
- `GET /api/indexing/knowledge-stats`。

指标：extraction blocks/calls、entities added、mentions replaced、facts added/updated/retired、entity/relation embeddings、relocated provenance reuse、knowledge repair rate。

## 10. 安全与质量边界

- 仅对已通过 Page Quality Gate 的 `index` 页面抽取；
- Fact 必须保留 page version、source URL、Block ID 和 fetched_at；
- 不把 LLM confidence 当作来源可信度；
- 当前版本不做敏感实体自动发布审批，受限内网站点上线前仍需 RBAC/approval；
- 对抽取结果执行端点、来源 Block、空名称和自环校验；
- 旧事实只有在来源 Block 真正受影响时才能撤销。

## 11. 验收标准

1. 仅修改一个 Block 时只进行一次页面级增量抽取；
2. unchanged/metadata_changed 不触发抽取；
3. relocated 迁移 mention/fact 来源且不调用 LLM；
4. 删除一个支持块但仍有其他支持时事实保持 active；
5. 最后一个支持消失时当前关系退休且历史可查；
6. 新事实实体与关系进入 Neo4j/Qdrant；
7. 已有 canonical Entity 不重复生成向量；
8. Patch 重试复用持久化 KnowledgeDelta；
9. 事实历史重启后可查询且相同版本重放幂等；
10. 全量测试、Ruff、TypeScript、真实 Observation 离线校准和 API 冒烟通过。

## 12. 非目标

- 本轮不实现跨文档 LLM 事实冲突裁决；
- 不实现完整自然语言时间解析和事件时间推断；
- 不自动删除仍被其他页面引用的 Entity；
- 不建设人工 ontology 管理 UI；
- 不把事实历史复制到 Qdrant；历史查询走 SQLite，在线检索只读当前态。

## 13. 后续方向

1. 双时态查询、事实冲突并存和来源优先级；
2. Human-in-the-loop 审批、敏感实体策略和租户隔离；
3. 抽取模型离线评测、schema-guided decoding 和置信度校准；
4. 独立 Knowledge Enrichment Outbox，Block 可先发布、Entity 层按 SLA 追平；
5. 变更通知、事实订阅和版本 Diff 前端；
6. 跨页面证据合并、关系强度校准与 orphan GC。
