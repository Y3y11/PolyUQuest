# PolyUQuest 容器制品与供应链门禁 PRD

> 迭代 14 · 2026-08-14 · 状态：实施中

## 1. 背景

迭代 13 已补齐后端/前端 Dockerfile、生产 Compose、独立 Worker、配置 fail-fast、冷备份与恢复 Runbook，但本地 Docker daemon 未启动，因此只能证明 Dockerfile 静态策略与 Next.js 本地构建，尚不能证明 Linux 镜像真的能够构建、以非 root/只读方式运行或通过漏洞基线。

同时，仓库当前唯一 GitHub Actions 只执行离线评测门禁；应用代码、锁文件或 Dockerfile 改动不会触发容器制品验证。生产部署仍可能在发布当天才发现依赖无法安装、entrypoint 缺失、只读文件系统不兼容或基础镜像存在已修复的严重漏洞。

## 2. 业务目标

企业内网/机构网站问答系统会持续在线探索并把知识增量写入图和向量库。其发布风险不仅是“答案指标下降”，也包括制品无法启动、以 root 运行、供应链组件被替换或已知漏洞进入受信网络。

本轮目标是在 PR 阶段建立可复现的容器交付证据：

1. Linux BuildKit 真实构建 API/Worker 与前端镜像；
2. 后端镜像在非 root、只读根文件系统下执行内部 contract check；
3. 前端镜像在同等约束下启动并返回 HTTP 2xx；
4. 每个镜像生成 CycloneDX SBOM、漏洞 JSON、image inspect 和 build metadata；
5. 对“存在修复版本的 CRITICAL 漏洞”阻断，对 HIGH/未修复项先留证据；
6. 所有远程 GitHub Action 使用完整 commit SHA，不信任可移动 tag；
7. 扫描器二进制使用固定版本和仓库内固定 SHA-256 校验。

## 3. 修改前现状与问题

### 3.1 静态检查不能证明镜像可构建

`validate_deployment.py` 能发现 `latest`、端口暴露和缺少非 root 配置，但不会执行 `uv sync`、`npm ci`、Next standalone 复制或镜像 healthcheck。Dockerfile 语法正确不等于制品可运行。

### 3.2 本地环境不是稳定发布证据

- Windows 本地构建与 Linux container filesystem 行为不同；
- 本地 daemon 是否启动、缓存是否存在无法成为团队门禁；
- 人工说“我本地运行过”无法关联到 PR commit，也无法长期下载审计。

### 3.3 缺少容器内部契约

镜像 inspect 只能看到声明的 `USER`，不能证明：

- `/app/src`、`configs`、发布数据是否存在；
- `agent-rag-serve` / `agent-rag-worker` 是否真的安装；
- runtime/cache/model cache 在只读根文件系统下是否仍可写；
- API/Worker 模块是否可导入；
- `/app` 是否保持不可写。

### 3.4 缺少组件清单与漏洞证据

`uv.lock`/`package-lock.json` 是构建输入，不是最终镜像 SBOM；基础系统包、编译结果和传递依赖仍不可见。没有固定的 severity、ignore-unfixed 和 exit-code 策略时，扫描很容易沦为“生成报告但永远不阻断”，或因为全部 HIGH 一次性阻断而长期全红。

### 3.5 CI 自身也属于供应链

现有 workflow 中 `actions/checkout@v7`、`actions/upload-artifact@v7` 使用可移动 major tag。2026 年 Trivy 生态曾发生 action/tag 与发布制品供应链事件，说明“工具本身是安全扫描器”不能替代版本、SHA 和下载校验治理。

## 4. 门禁架构

```text
Pull Request / workflow_dispatch
              |
              v
      Policy validation job
      - workflow/action SHA
      - scanner checksum
      - Docker contract policy
              |
              v
      BuildKit container job
      +--------------------------+
      | backend image            |
      | frontend image           |
      +-------------+------------+
                    |
          +---------+---------+
          |                   |
   runtime contract      Trivy evidence
   - uid/read-only        - CycloneDX
   - paths/commands       - vuln JSON
   - writable volumes     - CRITICAL gate
   - HTTP smoke           - inspect/metadata
          |                   |
          +---------+---------+
                    v
            immutable run artifact
```

PR workflow 不推送镜像、不接触生产 Secret、不调用模型 API，也不声称生成可部署 registry attestation。它证明“此 commit 可形成满足基础契约的镜像”。镜像发布、GHCR digest 与签名 attestation 留给独立的受保护 tag/release 流程。

## 5. 后端 Container Contract

新增 `agent-rag-container-check`，在镜像内输出稳定 JSON：

- schema version 与总状态；
- 当前 UID 是否为 10001 且非 root；
- `/app/src/agent_rag`、`/app/configs`、`/app/data` 是否存在；
- API、Worker、Contract 三个 console script 是否在 PATH；
- `agent_rag.api.main`、`agent_rag.workers.main` 是否可导入；
- `/app/data/runtime`、`/app/data/cache`、`/home/app/.cache` 是否可写；
- `/app` 是否不可写。

CI 必须以 `--user 10001:10001 --read-only` 运行，并把三类合法写路径挂为 tmpfs。Contract 不连接 Neo4j/Qdrant、不下载模型、不读取生产 Key，因此结果确定且不会产生外部副作用。

## 6. 前端 Runtime Smoke

CI 用 production standalone 镜像启动临时容器：

- 强制 UID/GID 10001；
- `--read-only` 且只有 `/tmp` 为 tmpfs；
- 只绑定 loopback 随机/固定 CI 端口；
- 有界重试首页 HTTP 2xx；
- 无论成功失败都输出 logs 并删除容器。

这验证的不只是 `next build`，还包括 standalone 文件复制、Node entrypoint、只读运行与 HTTP serving。

## 7. 构建策略

- 使用 Buildx 的 path context，确保 checkout 后的当前 PR 内容参与构建；
- 后端/前端分别使用 GHA cache scope，避免互相污染；
- `load: true` 将构建结果载入 job daemon 供 contract 与 scanner 使用；
- image tag 只包含 `ci-${github.sha}`，禁止 latest；
- 不把大型 image tar 上传 artifact，避免存储成本；
- 上传 build metadata、inspect、contract、SBOM 和 scan report，保留 14 天。

## 8. 漏洞与 SBOM 策略

扫描器固定 Trivy `0.72.0`；Linux AMD64 release archive SHA-256 固定为 `bbb64b9695866ce4a7a8f5c9592002c5961cab378577fa3f8a040df362b9b2ea`。CI 下载后先 `sha256sum -c`，再执行。

每个镜像执行：

1. CycloneDX JSON SBOM；
2. HIGH + CRITICAL 漏洞 JSON，供人工和后续趋势分析；
3. 阻断扫描：`CRITICAL + ignore-unfixed + exit-code 1`。

首个基线不直接阻断全部 HIGH，因为机器学习/Python 镜像常包含大量传递项，未经 triage 的全量阻断会导致永久绕过。后续以报告为证据逐步设定 HIGH budget、VEX 和到期豁免。

## 9. CI 自身安全

- workflow 权限仅 `contents: read`；
- 不使用 `pull_request_target`，避免 fork PR 获得高权限上下文；
- checkout 设置 `persist-credentials: false`；
- checkout、Buildx、build-push、upload-artifact 全部固定 40 位 commit SHA；
- 不使用曾被 tag 劫持风险放大的 scanner action，改为官方 immutable release asset + 本地固定 checksum；
- 不接收/打印 production Secret；
- shell 变量引用加引号，artifact 文件名不来自不可信 PR 文本。

## 10. 配置与可演进策略

新增 `configs/supply_chain.json` 作为机器可读合同，集中保存：

- schema version；
- Trivy 版本与 archive SHA；
- blocking severity、ignore-unfixed；
- expected UID/GID；
- artifact retention days；
- 允许的远程 Action 及固定 commit。

`validate_supply_chain.py` 同时校验配置、workflow 和 Dockerfile，防止 workflow 手工修改后与文档策略漂移。

## 11. 验收标准

### 本地可执行

1. supply-chain policy validator 通过；
2. container contract 单元测试覆盖成功与多类失败；
3. workflow action 全部 SHA pin；
4. 全量 pytest、Ruff、前端测试与 TypeScript 通过；
5. `git diff --check` 无错误。

### GitHub Linux runner

1. 两个镜像 BuildKit build 成功；
2. 后端 contract JSON `ok=true`；
3. 前端 HTTP smoke 成功；
4. 两套 SBOM、scan JSON、inspect 和 metadata 均上传；
5. 有已修复 CRITICAL 时 workflow 失败；
6. 不需要仓库 Secret。

本地 daemon 仍不可用时，不把 policy test 代替远端 build；push 后必须检查对应 Actions run，只有远端 job 成功才证明本轮完整验收。

## 12. 非目标与回滚

非目标：

- PR 中推送 GHCR/生产 registry；
- 生成签名 provenance/SBOM attestation；
- 多架构镜像；
- 在线模型/网页/Neo4j/Qdrant 端到端查询；
- 自动修复 CVE；
- 用漏洞数量替代风险 triage。

回滚时可删除新 workflow 与 contract entrypoint，不改变检索、存储或生产数据。若扫描器数据库暂时不可用，应重试 job；不得永久添加 `continue-on-error` 绕过安全失败。

## 13. 后续方向

1. **Release workflow**：受保护 tag 构建并推送 GHCR，使用 digest 而非 tag 部署。
2. **Artifact attestation**：OIDC 生成 build provenance 与 SBOM attestation，部署侧验证。
3. **VEX/豁免治理**：owner、理由、到期时间、修复版本和审批审计。
4. **HIGH budget**：按 OS/application、fixable/unfixed 分层设置增量预算。
5. **多架构**：amd64/arm64 matrix 与平台一致性 contract。
6. **端到端 smoke**：启动 Neo4j/Qdrant，使用 deterministic fake model 执行查询和增量入图。
7. **恢复门禁**：临时 volume 执行 backup → mutate → restore → checksum/data assertions。
8. **依赖更新自动化**：Renovate/Dependabot 提交 pin/digest 更新并强制经过本门禁。

## 参考

- Docker Build Push Action: https://github.com/docker/build-push-action
- GitHub artifact attestations: https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations
- Trivy 0.72.0 release: https://github.com/aquasecurity/trivy/releases/tag/v0.72.0
- Trivy 2026 supply-chain advisory: https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23
