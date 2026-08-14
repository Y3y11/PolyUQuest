# PolyUQuest 生产运行能力分层与镜像瘦身 PRD

> 迭代 15 · 2026-08-14 · 状态：已完成并通过 GitHub Linux 双 Profile 制品验收

## 1. 背景

迭代 14 首次用 GitHub Linux runner 真实构建、运行并扫描生产镜像。门禁证明当前镜像可用，也给出了此前静态检查无法获得的成本基线：

- 后端最终镜像大小 `5,910,698,673 bytes`，约 5.91 GB；
- 后端 CycloneDX SBOM 包含 256 个组件；
- 首次冷门禁约 26 分钟，修复后绿色 `build-contract-scan` 仍耗时 17 分 57 秒；
- 前端镜像仅约 191 MB、44 个组件，主要成本集中在后端；
- `uv tree` 显示默认依赖安装了 Torch 2.11、Transformers、Sentence Transformers，以及 FlagEmbedding 引入的 Accelerate、Datasets、IR Datasets、PEFT 等训练/评测依赖。

代码审计同时发现：当前 serving repo 只有 `EMBEDDING_PROVIDER=local` 路径会延迟导入 `sentence_transformers`；`FlagEmbedding` 没有任何运行时代码引用。生产 Compose 和生产环境示例默认使用 SiliconFlow 远程 embedding，却仍安装完整本地 ML 栈。

因此，当前默认制品把“可选的本地推理能力”与“企业 Web Agent 在线服务核心能力”绑定，造成镜像、构建、分发、扫描和攻击面的非必要膨胀。

## 2. 业务目标

企业内网/机构网站问答系统的默认生产形态通常通过受控 API 调用 embedding/reranker，服务节点负责检索编排、图/向量访问、网页探索、增量建图和审计。并非每个 API/Worker 实例都需要加载本地 BGE-M3。

本轮目标是将交付制品按运行能力分为：

1. **remote profile（默认）**：保留完整 Agent/RAG/增量知识能力，embedding/reranker 走远程服务，不安装 Torch/Sentence Transformers；
2. **local-ml profile（显式选择）**：增加本地 Sentence Transformers embedding 能力，用于离线、无外部 embedding 服务或 GPU/CPU 自托管场景；
3. 两个 profile 使用同一业务代码和锁文件，但拥有可审计、不可伪装的镜像能力标记；
4. 配置与镜像能力不匹配时在进程启动前 fail-fast，不允许等到首个用户请求才 `ModuleNotFoundError`；
5. 默认 remote 镜像建立大小预算与“本地 ML 包不得出现”的 CI 合同；
6. local-ml 镜像必须能够单独构建、执行同一只读 contract，并接受 SBOM/CVE 门禁。

## 3. 修改前现状与主要问题

### 3.1 可选能力被声明为核心依赖

`sentence-transformers` 与 `FlagEmbedding` 位于 `[project.dependencies]`。任何 `uv sync --no-dev` 都会安装完整 ML 栈，即使运行时 `EMBEDDING_PROVIDER=siliconflow`。

### 3.2 未使用依赖扩大供应链

`FlagEmbedding` 在当前 serving 代码中没有 import，却引入训练、数据集、IR benchmark 和 PEFT 等大量传递依赖。这些组件不会改善在线 Agent 的远程 embedding 路径，却增加下载、CVE、License 和更新成本。

### 3.3 镜像标签不能证明能力

仅提供 `polyuquest-backend:<version>`，无法判断镜像是否包含本地 ML。操作者可以把环境变量改成 `EMBEDDING_PROVIDER=local`，直到第一次 warmup 才发现包不存在，API readiness 只会保持失败，Worker 还可能在其他路径继续运行。

### 3.4 Docker 环境变量可被覆盖

如果只用 `ENV APP_RUNTIME_PROFILE=remote` 声明能力，部署时可以覆盖该值并把 remote 镜像伪装成 local-ml。运行能力必须由镜像内 immutable marker 与声明值共同验证。

### 3.5 CI 没有体积预算

迭代 14 记录了 `image inspect`，但未对大小设置上限。未来新增一个包可能再次把 remote 镜像推回数 GB，而 workflow 仍保持绿色。

## 4. 运行 Profile 设计

### 4.1 Profile 定义

| Profile | 默认 | Embedding | 必须存在 | 必须不存在 |
|---|---:|---|---|---|
| `remote` | 是 | SiliconFlow / OpenAI-compatible API | Agent/RAG core | `sentence_transformers`、`torch`、`FlagEmbedding` |
| `local-ml` | 否 | 本地 SentenceTransformer | `sentence_transformers`、`torch` | `FlagEmbedding` |

远程 Qwen3 reranker 已通过 HTTP API 调用，不依赖本地 Transformers，因此属于 core 能力。

### 4.2 Immutable marker

Docker build 接收 `APP_RUNTIME_PROFILE=remote|local-ml`：

- 根据 profile 决定是否安装 `local-ml` optional extra；
- 把值写入 `/app/.runtime-profile`；
- 同时设置同名环境变量供日志和配置使用。

运行时解析规则：

1. 镜像 marker 存在时，它是实际能力的事实源；
2. 环境声明必须与 marker 一致，否则 fail-fast；
3. 宿主机开发没有 marker 时使用环境声明，默认 `auto`；
4. `remote + EMBEDDING_PROVIDER=local` 直接拒绝；
5. `local-ml` 缺少 Sentence Transformers 直接拒绝；
6. remote 镜像意外包含本地 ML 包时 contract 失败，防止体积回归。

### 4.3 启动与 Readiness 语义

能力不匹配属于部署配置错误，不是临时依赖故障：

- 在 `Settings` 构造/模块加载阶段抛出明确错误；
- API/Worker 均不能进入主循环；
- 不用 readiness 永久 503 掩盖错误；
- contract 输出 profile、marker、embedding provider 与包能力检查，便于 CI 审计。

## 5. 依赖拆分

`[project.dependencies]` 保留 FastAPI、Neo4j、Qdrant、OpenAI-compatible client、抓取、HTML 处理、BM25 和通用 Agent 依赖。

新增：

```toml
[project.optional-dependencies]
local-ml = ["sentence-transformers>=3.3"]
```

处理原则：

- 从 core 删除 `sentence-transformers`；
- 删除当前未使用的 `FlagEmbedding`，不迁入 local-ml；
- 本地开发需要本地 embedding 时执行 `uv sync --extra local-ml`；
- 锁文件仍记录 optional extra 的精确传递版本，local-ml 构建继续使用 `--locked`；
- 不为了减小镜像删除网页探索、图存储、向量存储或异步索引业务能力。

## 6. Docker 与 Compose 设计

### 6.1 单 Dockerfile、双能力构建

同一 Dockerfile 用 build arg 选择依赖，不复制两套业务镜像定义。非法 profile 在 build 阶段退出 64。

默认构建：

```bash
docker build --build-arg APP_RUNTIME_PROFILE=remote -t polyuquest-backend:remote .
```

本地 ML：

```bash
docker build --build-arg APP_RUNTIME_PROFILE=local-ml -t polyuquest-backend:local-ml .
```

### 6.2 生产 Compose

- `BACKEND_RUNTIME_PROFILE` 默认 `remote`；
- build arg 与 API/Worker 环境声明来自同一变量；
- API/Worker 继续复用同一镜像，避免 profile 混用；
- 使用预构建镜像时，marker 会阻止错误环境声明伪装能力；
- local-ml profile 的 CPU/GPU、模型缓存和 warmup 容量需要单独评估，不沿用 remote 资源结论。

## 7. CI 与制品合同

### 7.1 自动 remote 门禁

所有相关 push/PR：

1. 显式以 remote profile 构建；
2. 运行只读 non-root contract；
3. 证明 Sentence Transformers、Torch、FlagEmbedding 均不在镜像；
4. 使用 `docker image inspect` 获取精确字节数；
5. 超过配置中的 remote size budget 立即失败；
6. 生成 SBOM、漏洞报告并执行 CRITICAL gate。

首个预算设为 `1,500,000,000 bytes`。它比 5.91 GB 基线降低约 75%，同时给非 ML core 依赖保留空间。真实构建若证明合理核心制品仍超过预算，应基于 layer/SBOM 证据调整，而不是静默删除门禁。

### 7.2 手动 local-ml 门禁

`workflow_dispatch(include_local_ml=true)` 额外执行：

- local-ml 构建；
- profile/package contract；
- image inspect 与 CycloneDX；
- HIGH/CRITICAL 报告和同一 fixable CRITICAL gate；
- 独立 artifact。

local-ml 不是每次 PR 默认构建，避免让可选的大型能力继续占据主交付路径；但在首次实现、依赖升级和计划部署 local-ml 前必须手动运行并保留证据。

## 8. 配置与可观测性

`configs/supply_chain.json` 升级 schema，增加：

- remote profile 最大镜像字节数；
- remote 禁止模块；
- local-ml 必须/禁止模块；
- 手动 profile artifact 保留策略沿用全局值。

Container contract JSON schema 升级，增加：

- immutable marker profile；
- declared profile；
- effective profile；
- embedding provider；
- local ML package availability；
- profile/dependency/config compatibility。

不记录 API key、模型输入或用户问题。

## 9. 验收标准

### 9.1 功能与配置

1. 远程 embedding 的现有测试和业务路径无回归；
2. host `auto` 模式兼容现有开发环境；
3. remote + local embedding 在启动期失败，错误包含 profile 和修复指引；
4. marker 与环境声明不一致时失败；
5. local-ml 缺包时失败；
6. FlagEmbedding 不再是任何安装 profile 的直接依赖。

### 9.2 自动 remote 制品

1. Linux BuildKit 成功；
2. contract `ok=true`；
3. image size `<= 1,500,000,000 bytes`；
4. `sentence_transformers`、`torch`、`FlagEmbedding` 均不可导入；
5. 前端及既有供应链门禁继续成功；
6. fixable CRITICAL 为 0；
7. artifact 记录优化前后大小与 SBOM 组件数。

### 9.3 local-ml 制品

1. 手动 Linux build 成功；
2. contract 证明 marker/profile 一致；
3. Sentence Transformers 与 Torch 可导入，FlagEmbedding 不存在；
4. SBOM/CVE artifact 上传；
5. fixable CRITICAL 为 0。

### 9.4 实际验收结果

代码提交 `12ed071` 后，自动 remote 门禁与手动 local-ml 门禁均在 GitHub Linux runner 完成真实 BuildKit 构建、只读 non-root contract、SBOM、CVE 和 artifact 验收：

- [自动 remote 门禁 run 31793909535](https://github.com/Y3y11/PolyUQuest/actions/runs/31793909535) 全部成功；
- [手动双 Profile 门禁 run 31794187307](https://github.com/Y3y11/PolyUQuest/actions/runs/31794187307) 全部成功；
- 两个 Profile 均为实际镜像证据，不以宿主机包状态或 Dockerfile 静态文本代替。

| 指标 | 迭代 14 单一镜像 | 迭代 15 `remote` | 改进 |
|---|---:|---:|---:|
| 后端镜像大小 | 5,910,698,673 bytes | 516,736,548 bytes | 减少 91.26% |
| CycloneDX 组件数 | 256 | 177 | 减少 30.86% |
| 绿色 `build-contract-scan` | 17m57s | 1m52s | 减少 89.60% |
| HIGH 漏洞 | 9 | 6 | 减少 3 项 |
| CRITICAL / fixable CRITICAL | 0 / 0 | 0 / 0 | 持续通过 |

`remote` artifact 的 14 项 contract 全部通过：marker、声明与 effective profile 均为 `remote`，provider 为 `siliconflow`，Sentence Transformers、Torch 和 FlagEmbedding 均不可用。负向 contract 使用 `EMBEDDING_PROVIDER=local` 启动同一镜像时按预期失败，证明配置不能伪装镜像能力。实际大小仅占 1.5 GB 预算的 34.45%。

| `local-ml` 验收项 | 实际结果 |
|---|---|
| Linux BuildKit / contract | passed；14 项检查 |
| Profile / provider | `local-ml` / `local` |
| Sentence Transformers / Torch | present / present |
| FlagEmbedding | absent |
| 镜像大小 | 5,640,402,724 bytes |
| CycloneDX 组件数 | 230 |
| HIGH / CRITICAL / fixable CRITICAL | 6 / 0 / 0 |
| Job 总耗时 | 15m36s；其中构建 12m30s、扫描 2m44s |

`local-ml` 仍是大型制品，因此保持手动触发是合理的：它证明本地能力没有被镜像瘦身误删，但不会让可选能力占据每次提交的主交付路径。

## 10. 文件级修改计划

新建：

- `docs/RUNTIME_PROFILE_IMAGE_OPTIMIZATION_PRD.md`；
- `docs/RUNTIME_PROFILE_IMAGE_OPTIMIZATION_RUNBOOK.md`；
- `src/agent_rag/deployment/runtime_profile.py`；
- `tests/test_runtime_profile.py`。

修改：

- `pyproject.toml` / `uv.lock`：core 与 local-ml extra；
- `config.py`：profile 声明与启动 fail-fast；
- `Dockerfile`：profile build arg、optional extra 和 immutable marker；
- `container_contract.py`：profile/package/config 合同；
- `compose.production.yml` / production env template：显式 remote 默认；
- `supply_chain.json` / validator / workflow：size budget、remote 自动门禁、local-ml 手动门禁；
- deployment policy/tests：API/Worker profile 一致性；
- README 与本地迭代文档。

## 11. 风险与回滚

### 风险

- 隐式依赖：某个模块可能偶然依赖 Torch；全量 import/test 与真实镜像 contract 用于发现；
- profile 欺骗：使用 marker + 环境一致性检查，不信任单一 env；
- local-ml 模型权重不随镜像发布：本轮只验证运行库，不下载数 GB 模型；部署仍需受管模型缓存和离线供应链；
- 大小预算过紧：以最终 inspect 和 layer 证据评审，不使用任意压缩或删除业务模块；
- optional extra 漂移：`uv.lock --check` 与手动 local-ml build 共同验证。

### 回滚

如 remote profile 暴露未发现的业务依赖，可暂时选择已验证的 local-ml profile 镜像，而不是把 ML 包重新塞回 core。回滚必须保留 profile marker 和 contract，不能恢复成能力不透明的单一镜像。

## 12. 非目标

- 本轮不改变 BGE-M3 算法、向量维度或已建 Qdrant collection；
- 不把本地 reranker引入镜像，当前 Qwen3 reranker 仍走远程 API；
- 不内置模型权重；
- 不实施 GPU/CUDA 镜像；
- 不更换 embedding provider；
- 不以 Alpine/musl 重写 Python 镜像换取表面体积；
- 不在本轮完成 registry push、签名和部署晋级。

## 13. 后续方向

1. **业务端到端门禁**：Neo4j/Qdrant + fake model 执行 Query → 探索 → 增量入图；
2. **发布制品**：受保护 tag 推送 GHCR digest，生成 provenance/SBOM attestation；
3. **HIGH 增量预算**：基于 remote/local-ml 各自 SBOM 建立独立 baseline；
4. **GPU profile**：仅在真实业务需要时设计 CUDA/runtime/driver compatibility matrix；
5. **模型制品治理**：模型 digest、来源、许可证、离线缓存、恶意 pickle/safetensors 策略；
6. **CI 成本优化**：主分支写 cache、PR 只读、按 profile/layer 统计 cache hit 与构建费用；
7. **容量模型**：remote 与 local-ml 分别压测启动时间、P95 查询、内存与并发。
