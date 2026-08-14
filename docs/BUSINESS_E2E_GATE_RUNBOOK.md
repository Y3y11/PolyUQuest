# PolyUQuest 业务 E2E 门禁运行手册

## 1. 门禁验证什么

该门禁使用真实 Neo4j、Qdrant、SQLite Ledger 和生产 Agent/Worker 实现，验证：

```text
冷查询 → 在线抓取 → 临时证据回答 → Outbox
       → 部分写失败 → Worker 重启恢复 → 图/向量发布
       → 热查询零抓取 → 页面更新 → 局部向量化
       → Fact 退休/生效 → Patch 幂等重放
```

它不调用外部 LLM、Embedding、Reranker，不测模型语义质量；模型质量由冻结评测门禁负责，容器安全由供应链门禁负责。

## 2. 本地前置条件

- Python 3.11+ 与 `uv`；
- Docker daemon 正常运行；
- 端口 `17687`、`16333` 未被占用；
- 不需要任何 API Key；
- 不要把 E2E 指向生产 Neo4j/Qdrant。

## 3. 启动隔离依赖

PowerShell：

```powershell
docker compose -f compose.e2e.yml up -d --wait
$env:APP_ENVIRONMENT = "test"
$env:APP_RUNTIME_PROFILE = "auto"
$env:NEO4J_URI = "bolt://127.0.0.1:17687"
$env:NEO4J_USER = "neo4j"
$env:NEO4J_PASSWORD = "business_e2e_password"
$env:QDRANT_HOST = "127.0.0.1"
$env:QDRANT_PORT = "16333"
$env:EMBEDDING_PROVIDER = "siliconflow"
$env:EMBEDDING_DIM = "64"
$env:RERANKER_MODE = "first_stage_only"
$env:AGENT_ASYNC_INDEXING = "true"
```

Compose 使用 tmpfs，不挂载项目已有数据库，也不执行清库。

## 4. 执行

```powershell
uv sync --locked --group dev
uv run pytest tests/test_business_e2e_contract.py -q
uv run agent-rag-business-e2e `
  --output data/runtime/business-e2e/report.json `
  --runtime-dir data/runtime/business-e2e
```

成功时进程退出码为 0；任何 required check 失败时退出码为 1，但仍写出 JSON 报告。

## 5. 停止与清理

```powershell
docker compose -f compose.e2e.yml down
```

E2E 数据只存在于 Compose tmpfs 和被 `.gitignore` 忽略的 `data/runtime/business-e2e/`。Compose down 后数据库内容不可恢复，这是隔离测试数据，不是业务备份。

## 6. 报告阅读

重点字段：

- `status`：只有所有 contract check 通过才是 `passed`；
- `stages`：依赖、冷查询、故障注入、恢复、热查询、刷新、增量发布、幂等、更新后查询；
- `checks`：每项 expected/actual/ok；
- `audit_ids`：Agent Run、Index Job、Patch、PageVersion 的关联 ID；
- `summary.origin_requests`：正常应为 2（初次抓取 + 一次变化刷新）；
- `summary.llm_usage/reranker_usage`：调用数应为 0；
- `error`：已脱敏、单行且最长 500 字符。

## 7. 常见失败

### 7.1 `Neo4j/Qdrant did not become ready`

检查：

```powershell
docker compose -f compose.e2e.yml ps
docker compose -f compose.e2e.yml logs neo4j-e2e qdrant-e2e
```

确认环境端口是 `17687/16333`，没有误连生产 Compose 的内部服务。

### 7.2 Qdrant vector dimension mismatch

E2E collection 固定使用当前进程的 `EMBEDDING_DIM`。如果复用了非 tmpfs Qdrant，旧 collection 可能维度不同。停止错误容器并使用 `compose.e2e.yml` 的隔离实例，不要删除生产 collection。

### 7.3 冷查询进入 `evidence_only`

查看 `cold_start_query.metrics.quality` 和相应 check。测试夹具必须满足通用 Page Quality Policy；不要在 E2E 中绕过质量门禁。若生产策略有意收紧，应同步调整夹具为更真实的高质量页面，而不是降低阈值。

### 7.4 第二次查询仍然抓网页

检查 `restart_recovery` 的 Block/Vector 数、`hot_query_reuse` 的 Evidence 和检索 trace。通常意味着发布不完整、Embedding 维度不一致或证据判断规则发生了合同变化。

### 7.5 外部模型调用不为 0

确认 `RERANKER_MODE=first_stage_only`，Query 使用 forced block mode，且 Profile/Answer/Extraction 都使用 E2E 确定性边界。不要给 CI 添加真实 API Key 来掩盖问题。

## 8. GitHub Actions

Workflow `Business E2E Gate` 在相关 Agent/Store/Worker/配置变更时自动运行。它使用 GitHub service containers 的默认端口 `7687/6333`，与本地隔离 Compose 端口不同。

失败时也会上传 `business-e2e-<commit>` artifact。排查顺序：

1. 下载 `report.json`；
2. 找到第一个 failed stage/check；
3. 用 `audit_ids` 关联 Worker/Patch/Version；
4. 本地用隔离 Compose 复现；
5. 修复生产路径或明确更新 PRD 合同，不能只改测试返回值。

## 9. 三类门禁的职责

| 门禁 | 回答的问题 |
|---|---|
| Evaluation Gate | 模型/检索质量是否回归 |
| Business E2E Gate | 在线知识业务闭环是否真实成立 |
| Container Supply Chain Gate | 交付镜像是否可运行、可审计且安全 |

发布候选最终需要三类证据绑定到同一个 commit。
