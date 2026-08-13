# Agent 探索增量知识入库 PRD

版本：v1.0
日期：2026-08-13
状态：MVP 已实现并完成本地验收

## 1. 背景与问题

Query-driven Agent 已能在现有知识库证据不足时，从受信任网站自主选择、抓取并结构化网页。但当前抓取结果默认只保存在进程内 Observation 中，用于本次回答；请求结束后，Graph 页面仍为空，后续相同或相关问题也无法复用这些新证据。

本迭代将 Agent 的在线探索升级为受控的增量知识获取路径：每个通过安全与内容检查的实际抓取页面，都应以幂等 Patch 写入当前 Neo4j/Qdrant，而不是依赖或导入历史离线知识卷。

## 2. 目标

1. Agent 每次成功探索的新页面默认增量写入当前知识库。
2. 同一 URL、相同内容重复抓取时不重复写入或重复计算 embedding。
3. 同一 URL 内容变化时用新快照替换旧页面内容，避免旧证据继续被检索。
4. Neo4j 与 Qdrant 的每次写入都有 Patch、运行、来源、哈希与状态审计。
5. 写入失败不阻断本次临时证据回答，并明确标记 `repair_required`。
6. Graph 页面可在查询完成后观察到新增 Page、Block、LINKS_TO 和 CONTAINS。

## 3. 非目标

1. 本迭代不导入旧 Docker 卷或历史离线语料。
2. MVP 不在在线请求中执行完整实体抽取、消歧和跨页实体关系构建；Entities/Topics 可保持为 0。
3. 不允许 Agent 扩大受信任域名白名单或持久化登录页、错误页、空页面。
4. 不在本轮实现跨进程分布式事务；MVP 以单 Worker 和可修复 Patch 为边界。

## 4. 用户故事

- 作为使用者，我第一次询问一个知识库没有覆盖的问题时，Agent 会探索可信网页并回答；我随后打开 Graph 页面，应看到本次探索形成的新页面和证据块。
- 作为使用者，我再次询问相关问题时，Agent 应优先复用刚写入的知识，而不是重新抓取相同页面。
- 作为运维人员，我需要区分一次页面写入是 `create`、`update`、`unchanged` 还是 `repair`，并能追踪对应 run、URL、旧新内容哈希和写入/删除数量。

## 5. 增量更新语义

### 5.1 Create

目标 URL 在 Neo4j 中不存在，或只存在由页面链接创建的空 Stub：

1. 写入 WebPage 元数据及网页向量；
2. 写入结构感知 DOM Blocks、CONTAINS/PARENT_BLOCK 及块向量；
3. 写入本页观测到的 LINKS_TO；
4. 执行 Neo4j/Qdrant 写后读取校验；
5. Patch 标记 `operation=create, status=published`。

### 5.2 Unchanged

URL 已存在且 `content_hash` 相同，同时页面、全部 Blocks 与向量均可读取：

1. 不重新计算 embedding；
2. 不重复 Upsert；
3. Patch 标记 `operation=unchanged, status=published`；
4. 更新本次审计记录，但知识节点数量不增长。

### 5.3 Update

URL 已存在且 `content_hash` 变化：

1. 先写入新页面、Blocks、向量和链接；
2. 再移除该页面不再包含的旧 CONTAINS；
3. 仅删除已无任何 WebPage 引用的旧 Block 及其 Qdrant 向量；
4. 删除本页面本次快照中已不存在的旧出边；
5. 校验当前页面的 Block 集与向量完整性；
6. Patch 标记 `operation=update` 并记录删除数量。

写新后删旧保证中途失败时最多保留陈旧数据，不会先破坏仍可用的旧快照。

### 5.4 Repair

URL 与哈希相同，但 Neo4j/Qdrant 存在缺失或偏斜时，不按 Unchanged 跳过，而是重放 Upsert，Patch 标记 `operation=repair`。若仍无法通过写后校验，则状态为 `repair_required`。

## 6. 数据契约

`GraphPatch` 至少包含：

- `patch_id / observation_id / run_id`；
- `source_url`；
- `operation: create | update | unchanged | repair`；
- `previous_content_hash / content_hash`；
- `status: staged | publishing | published | repair_required | failed`；
- `webpages_written / blocks_written / links_written`；
- `blocks_deleted / links_deleted`；
- `read_after_write_ok / error`；
- `created_at / updated_at`。

WebPage、Block 与 LINKS_TO 保留 `source_type=agent_fetch`、`agent_run_id`、`patch_id`、`content_hash` 和 `fetched_at`。

## 7. 写入状态机

```text
Fetched Observation
        |
        v
      staged
        |
        v
  inspect current snapshot
        |
   +----+---------+-----------+
   |              |           |
 create         update     unchanged
   |              |           |
   +-------> publishing <------+
                  |
          read-after-write
             /          \
       published    repair_required
```

页面抓取、结构化或安全检查失败时不创建可发布 Patch。本次回答仍可使用已验证的其他临时证据。

## 8. 安全与质量门槛

1. URL 必须通过既有 scheme、端口、域名白名单、DNS 与重定向检查。
2. 登录页、错误页、空 Block 页面不进入知识库。
3. 写入只接受服务端 Observation，客户端不能直接提交 HTML、Block 或目标 URL。
4. `persist_discoveries=false` 仍允许显式只读运行；部署级开关 `AGENT_ALLOW_PERSISTENCE=false` 可禁止所有在线写入。
5. 默认只持久化 WebPage–DOM Block–Link 层；实体层需独立质量门槛。

## 9. 一致性与失败处理

Neo4j 与 Qdrant 不支持同一原子事务。MVP 采用：

- Upsert 可重放；
- Patch 状态机；
- 写新后删旧；
- 写后读取校验；
- `repair_required` 保留故障上下文；
- 同进程按 URL 加锁，避免并发更新同一页面。

后续工程化版本应将 Patch/Observation 从进程内存迁移到 Postgres/任务队列，并提供 repair worker。

## 10. API 与界面

- Agent API 默认 `persist_discoveries=true`。
- 前端 Ask 默认发送持久化请求。
- 探索轨迹显示 create/update/unchanged/repair、写入和删除计数、校验状态。
- Graph 空库时显示明确空状态；完成首次探索并成功发布后刷新即可看到新图。

## 11. 验收标准

1. 空库执行一次真实探索后 `webpages > 0`、`blocks > 0`、`links_to/contains > 0`。
2. 相同 URL/相同哈希第二次发布不调用 embedder，节点与向量数量不增长。
3. 内容变更发布后旧的页面专属 Block 不再存在于 Neo4j/Qdrant。
4. 共享 Block 仍被其他页面引用时不得删除。
5. Qdrant 写入失败时 Patch 为 `repair_required`，Agent 仍能基于临时证据完成回答。
6. `persist_discoveries=false` 与部署级禁写开关仍然有效。
7. 每个成功或失败 Patch 均可追溯 run、URL、哈希和写入结果。

## 12. 评测指标

- Patch Publish Success Rate；
- Create / Update / Unchanged 比例；
- Duplicate Embedding Avoidance Rate；
- Read-after-write Pass Rate；
- Repair Required Rate / Repair Recovery Rate；
- Exploration-to-Reuse Hit Rate；
- Incremental Indexing P50/P95 延迟；
- Stale Block Leakage Rate；
- Graph/Vector Count Drift。

## 13. MVP 实现结果（2026-08-13）

### 13.1 已实现

- Agent API 与 Ask 前端默认使用 `persist_discoveries=true`；仍保留请求级只读模式和部署级总开关。
- 每个成功抓取且通过安全/内容检查的 Observation 自动执行 Stage → Publish。
- Publish 在 URL 级进程锁内检查现有页面、内容哈希、Block 集合及 Qdrant 向量完整性，分类为 `create / update / unchanged / repair`。
- `unchanged` 不调用 embedder，也不执行 WebPage、Block 或 Link Upsert。
- `update` 采用写新后删旧：移除快照中已不存在的 CONTAINS/LINKS_TO，只删除不再被任何页面引用的孤立 Block 及其 Qdrant 向量。
- 每次写入后同时读取 Neo4j 页面/Block 与 Qdrant 页面/Block 向量进行一致性校验；失败 Patch 标记为 `repair_required`。
- 探索轨迹显示增量操作类型、旧/新哈希、写入/删除计数与读后校验结果。
- Graph API 的默认数据契约从仅 `Entity-RELATES_TO-Entity` 扩展为 WebPage、Block、Entity、Topic 跨层关系；搜索支持页面标题、URL、块标题路径、正文、实体及主题。
- Graph 页面默认进入 Free Explore，确保尚未执行实体增强的新知识也能立即展示；旧的三层实体 Slice 保留为可选视图。

### 13.2 当前持久化边界

本轮只持久化可直接从网页确定的结构事实：

```text
WebPage -[:CONTAINS]-> DOM Block
WebPage -[:LINKS_TO]-> WebPage
```

链接目标在尚未被抓取时以 WebPage Stub 入图；之后真实访问该 URL 时，同一 URL 节点被补全为带 `content_hash`、标题和抓取时间的完整页面。Entity、Topic 与跨页语义关系需要独立抽取/消歧质量门槛，不在在线 MVP 中强制执行。

### 13.3 本地验收结果

- 空的新数据卷完成一次真实 Agent 探索后：`86 WebPages / 24 Blocks / 158 LINKS_TO / 24 CONTAINS`。其中 2 个是实际抓取页面，其余为从页面观察到、等待后续访问的链接 Stub。
- `/api/graph/data` 返回 `80 nodes / 134 edges`，包含 `56 WebPages / 24 Blocks`、`24 CONTAINS / 110 LINKS_TO`。
- 搜索 `computing` 返回 30 个邻域节点，中心为 `Home | Department of Computing`。
- 对 `https://www.polyu.edu.hk/comp/` 重复真实抓取：旧/新 SHA-256 相同，结果为 `operation=unchanged`，三类写入计数均为 0，读后校验为 true。
- Ruff 通过；Python `36 passed`；TypeScript `tsc --noEmit --incremental false` 通过。
- 本地浏览器端到端验证中，Graph 默认页展示 `Pages 86 / Snippets 24 / links to 158 / contains 24`，不再出现有数据但画布为空。

### 13.4 后续工程化项

- 将 Observation/Patch 从进程内存迁移到持久化任务表，并实现跨 Worker URL 锁与 repair worker。
- 在统计接口区分完整 WebPage 与仅链接 Stub，避免 `webpages` 数量被误解为已抓取页数。
- 增加条件请求（ETag/Last-Modified）、Patch 历史查询、人工审计/回滚和后台实体增强任务。
