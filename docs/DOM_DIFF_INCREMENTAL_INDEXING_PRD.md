# DOM Block Diff 与局部增量索引 PRD

## 1. 迭代目标

在 Freshness Worker 已能发现页面变化并提交新快照后，将发布过程从“整页重新 Embedding”改为“只处理新增或真正变化的结构块”，并持久化可审计的页面版本与 Block Diff。

本轮解决的是更新成本和恢复能力，不改变 PolyUQuest 的 WebPage–DOM Block–Entity 图建模方法。

## 2. 背景与现状

机构网站的更新通常只影响截止日期、人员、政策段落或新增章节。现有 `PublishPatchTool` 即使只变化一个 DOM Block，也会重新生成页面向量和全部 Block 向量，并全量写入 Neo4j/Qdrant。长页面和高频公告页会因此放大模型成本、网络写入和发布延迟；系统也缺少版本级 Diff，无法解释本次更新内容、Embedding 节省量及失败位置。

## 3. Block 身份与匹配

当前 Block ID 是 `hash(URL + DOM path)`：同一路径文本修改时 ID 稳定，但插入同级节点可能导致后续 `nth-of-type` 路径位移。MVP 使用两阶段匹配：

1. Stable-ID match：先匹配相同 `block_id`；
2. Relocation match：剩余块按规范化的 `content + heading_context` 指纹一一配对。

HTML path、depth、parent 和 child index 属于结构元数据，只变化这些字段时更新节点与 payload，但不重新生成语义向量。

## 4. Diff 分类与动作

| 分类 | 条件 | Embedding | 写入动作 |
|---|---|---:|---|
| `unchanged` | ID、语义和结构均相同 | 否 | 不重写块 |
| `metadata_changed` | ID、语义相同，结构不同 | 否 | 更新 Neo4j/Qdrant 元数据 |
| `modified` | ID 相同，语义不同 | 是 | upsert 新节点内容与向量 |
| `relocated` | ID 不同，语义相同 | 否 | 复用旧向量写入新 ID |
| `added` | 新 ID 无匹配 | 是 | 新建节点与向量 |
| `deleted` | 旧 ID 无匹配 | 否 | 移除关系；无引用时删除节点和向量 |

页面向量只在标题或 `meta_description` 变化、向量缺失时生成。

## 5. 发布流程

```text
Publish Patch
  -> load current page, blocks and vectors
  -> build deterministic BlockDiffPlan
  -> persist PageVersion(status=planned)
  -> embed changed page and modified/added blocks only
  -> reuse old vectors for relocated blocks
  -> partial upsert Neo4j/Qdrant
  -> reconcile removed blocks and links
  -> read-after-write verification
  -> PageVersion(status=published|repair_required)
  -> update freshness lifecycle only after success
```

Neo4j 保存当前可查询快照；SQLite Version Ledger 保存版本、Diff 和成本统计。完整 HTML 已由 Observation Ledger 压缩保存，本表不重复存储。

## 6. 页面版本模型

`page_versions` 包含：

- `version_id`、唯一 `patch_id`、`observation_id`、`run_id`、URL；
- previous/current content hash；
- `planned/publishing/published/repair_required` 状态和错误；
- old/new/unchanged/metadata_changed/modified/relocated/added/deleted 计数；
- page/block embedding 数、复用向量数、实际写入与删除数；
- `diff_json`（各类 ID、relocation 映射和页面语义是否变化）；
- created/updated/published 时间。

相同 patch 重放更新同一版本记录，不重复创建版本。

## 7. 一致性与恢复

1. Diff 确定性生成，写入前持久化；
2. 新节点和向量先 upsert，删除最后执行；
3. Neo4j/Qdrant 写操作幂等；
4. relocated 先复用旧向量写新 ID，再回收旧 ID；
5. 异常同时把 Patch 和 Version 标记为 `repair_required`；
6. 重试检查新向量是否已经存在，避免重复 Embedding；
7. read-after-write 验证页面、精确 Block ID 集合和全部当前向量；
8. Version 审计失败不得报告发布成功；
9. Freshness lifecycle 只在 Patch 与 Version 均发布后切换至新 hash。

## 8. 运维 API 与指标

- `GET /api/indexing/versions`：按 URL/status 查询；
- `GET /api/indexing/versions/{version_id}`：查看 Diff 与成本；
- `GET /api/indexing/version-stats`：版本数、失败数、变化块比例、Block/Page Embedding 节省率、relocated 复用数。

核心指标：

- `changed_block_ratio = (modified + added) / new_blocks`；
- `block_embedding_savings = 1 - block_embeddings / new_blocks`；
- `page_embedding_savings`；
- `relocated_reuse_count`；
- publish latency 和 repair-required rate。

## 9. 验收标准

1. 单块修改只生成一个 Block embedding；
2. 新增块只为新增块生成 embedding；
3. 删除块不触发其他块 embedding；
4. DOM 路径位移但内容不变时复用旧向量；
5. 仅结构元数据变化不重做 embedding；
6. 页面标题/描述未变时复用页面向量；
7. missing-vector repair 能补齐向量；
8. 删除/relocated 后 Neo4j/Qdrant 精确等于新快照；
9. Version Ledger 重启可查，相同 patch 保持幂等；
10. API、全量测试、Ruff、TypeScript 和真实页面离线 Diff 校准通过。

## 10. 非目标与后续方向

本轮不做用户一键回滚、Entity/Relation 增量抽取、语义模型跨内容模糊匹配和跨 URL Block 所有权迁移。后续可增加版本回滚与 Diff 可视化、受影响实体子图更新、变更通知，以及基于历史变化率的批量调度和成本预算。
