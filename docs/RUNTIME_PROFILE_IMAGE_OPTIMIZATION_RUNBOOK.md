# PolyUQuest 运行 Profile 与镜像能力 Runbook

本文用于选择、构建和排查后端 `remote` / `local-ml` 运行 profile。需求、基线和设计取舍见
[`RUNTIME_PROFILE_IMAGE_OPTIMIZATION_PRD.md`](RUNTIME_PROFILE_IMAGE_OPTIMIZATION_PRD.md)。

## 1. Profile 选择

### remote（生产默认）

适用于 embedding/reranker 由 SiliconFlow 或其他 OpenAI-compatible 服务提供的部署：

- 不安装 Sentence Transformers、Torch、FlagEmbedding；
- 保留 Web Agent、Neo4j、Qdrant、BM25、抓取、增量入图、Worker 和审计能力；
- 镜像必须小于供应链策略中的字节预算；
- `EMBEDDING_PROVIDER=local` 会在启动前失败。

### local-ml（显式选择）

适用于必须在应用节点运行本地 SentenceTransformer embedding 的部署：

- 安装 Sentence Transformers 和 Torch；
- 不安装未被 serving 代码使用的 FlagEmbedding；
- 不内置模型权重，首次运行仍需要受管模型缓存或允许的下载路径；
- 需要独立评估 CPU/GPU、内存、启动时间与模型供应链。

## 2. 本地开发

远程 embedding：

```powershell
uv sync --locked
$env:APP_RUNTIME_PROFILE = "auto"
$env:EMBEDDING_PROVIDER = "siliconflow"
```

本地 embedding：

```powershell
uv sync --locked --extra local-ml
$env:APP_RUNTIME_PROFILE = "auto"
$env:EMBEDDING_PROVIDER = "local"
```

如果 `.env` 已设置 `EMBEDDING_PROVIDER=local`，不要执行默认 `uv sync` 后直接启动服务；先安装
`local-ml` extra。`auto` 只用于无镜像 marker 的开发环境，生产必须显式选择 profile。

验证当前包能力：

```powershell
.venv\Scripts\python.exe -c "import importlib.util; print({name: bool(importlib.util.find_spec(name)) for name in ('sentence_transformers','torch','FlagEmbedding')})"
```

local-ml 期望前两项为 `True`、FlagEmbedding 为 `False`；remote 三项均为 `False`。

## 3. 构建镜像

remote：

```powershell
docker build `
  --build-arg APP_RUNTIME_PROFILE=remote `
  --tag polyuquest-backend:remote .
```

local-ml：

```powershell
docker build `
  --build-arg APP_RUNTIME_PROFILE=local-ml `
  --tag polyuquest-backend:local-ml .
```

非法 profile 会在依赖安装前退出。最终镜像同时包含：

- `ENV APP_RUNTIME_PROFILE=<profile>`；
- OCI label `org.polyuquest.runtime-profile`；
- root-owned `/app/.runtime-profile` immutable marker。

环境变量可以被部署覆盖，但 marker 不随环境变化；两者不一致时服务和 contract 均失败。

## 4. 本地运行 Contract

以下示例省略业务数据库和模型凭证，因为 contract 不连接外部依赖：

```powershell
docker run --rm `
  --read-only `
  --user 10001:10001 `
  --tmpfs /tmp:rw,noexec,nosuid,size=64m `
  --tmpfs /app/data/runtime:rw,noexec,nosuid,size=64m,uid=10001,gid=10001 `
  --tmpfs /app/data/cache:rw,noexec,nosuid,size=64m,uid=10001,gid=10001 `
  --tmpfs /home/app/.cache:rw,noexec,nosuid,size=64m,uid=10001,gid=10001 `
  polyuquest-backend:remote agent-rag-container-check
```

输出 `schema_version=2`、`ok=true`，并显示 declared/marker/effective profile 与模块能力。

负向验证：

```powershell
docker run --rm `
  -e EMBEDDING_PROVIDER=local `
  polyuquest-backend:remote agent-rag-container-check
```

命令必须非零退出，并说明 local embedding 需要 local-ml 镜像。

## 5. 生产 Compose

默认配置：

```dotenv
BACKEND_RUNTIME_PROFILE=remote
EMBEDDING_PROVIDER=siliconflow
```

切换 local-ml 时必须同时：

1. 将 `BACKEND_RUNTIME_PROFILE=local-ml`；
2. 将 `EMBEDDING_PROVIDER=local`；
3. 构建或选择已通过 local-ml 手动门禁的镜像；
4. 准备只读来源、可写 cache 与模型权重；
5. 校准 API/Worker 内存、CPU/GPU 和 readiness 时间；
6. 执行 contract、依赖 health 与代表性查询后再切流。

不要只修改 `APP_RUNTIME_PROFILE` 环境变量。Compose 通过 `BACKEND_RUNTIME_PROFILE` 同时控制 build arg 和
API/Worker 声明，镜像 marker 负责发现预构建镜像与环境不一致。

## 6. CI 门禁

每次容器相关 push/PR 自动验证 remote profile：

- 明确 build arg；
- non-root/read-only contract；
- remote 拒绝 local embedding；
- image inspect size budget；
- SBOM、HIGH/CRITICAL 报告和 fixable CRITICAL gate。

需要部署或升级 local-ml 时，在 GitHub Actions 手动运行 `Container Supply Chain Gate`，设置
`include_local_ml=true`。它会用独立 runner/cache 构建 local-ml、执行 contract、生成 SBOM/CVE 并上传
`local-ml-supply-chain-<commit>` artifact。

## 7. 常见错误

### `EMBEDDING_PROVIDER=local requires ... local-ml`

- 宿主机：执行 `uv sync --extra local-ml`；
- 容器：换用 `APP_RUNTIME_PROFILE=local-ml` 构建的镜像；
- 不要在 remote 镜像里临时 `pip install`，那会破坏 SBOM、digest 和可复现性。

### `APP_RUNTIME_PROFILE does not match immutable image profile`

部署环境与镜像能力不一致。检查 `BACKEND_RUNTIME_PROFILE`、镜像 tag/digest 和
`docker image inspect` label，重新选择正确镜像；不要覆盖 marker。

### remote image contains forbidden local ML modules

某个 core 依赖重新引入了 Torch/Sentence Transformers。使用 `uv tree` 和 SBOM 找到传递链，将可选能力
移入 extra 或评审新的业务必要性；不得简单放宽 contract。

### local-ml 缺少模型权重

运行库与模型制品是两类供应链。本轮镜像不内置 BGE-M3 权重。为模型缓存配置受控来源、固定 revision/
digest、许可证审计和可写 cache volume，再运行 warmup。

## 8. 回滚

- remote 发布故障且确认依赖缺失：切换到最近一次已通过门禁的 local-ml 镜像；
- local-ml 资源或模型故障：切回 remote 镜像并恢复远程 embedding 凭证；
- 两类回滚均按镜像 digest 和 profile marker 验证，不恢复成能力不透明的旧单镜像；
- profile 切换不改变向量维度、模型名称或 Qdrant collection；若这些值变化，需要独立重建/迁移方案。
