# PolyUQuest 容器供应链门禁 Runbook

本文说明如何运行、审阅和维护 `Container Supply Chain Gate`。详细需求与取舍见
[`CONTAINER_SUPPLY_CHAIN_GATE_PRD.md`](CONTAINER_SUPPLY_CHAIN_GATE_PRD.md)。

## 1. 门禁何时运行

以下改动在 `main`、`codex/**` 分支 push 或 Pull Request 时触发：

- 后端、前端、配置、锁文件或 Dockerfile；
- 生产 Compose 与部署策略；
- 容器门禁 workflow、策略校验器或相关测试。

也可从 GitHub Actions 手动执行 `workflow_dispatch`。工作流不读取生产 Secret、不调用模型、
不访问 Neo4j/Qdrant，也不推送镜像。

## 2. 本地预检

本地预检不需要 Docker daemon：

```powershell
.venv\Scripts\python.exe scripts/validate_supply_chain.py validate
.venv\Scripts\python.exe -m pytest tests/test_container_supply_chain.py -q
.venv\Scripts\python.exe -m ruff check `
  src/agent_rag/deployment `
  scripts/validate_supply_chain.py `
  tests/test_container_supply_chain.py
```

有可用 Docker daemon 时，可额外执行真实构建；最终仍以 GitHub Linux runner 的结果为共享证据：

```powershell
docker build --tag polyuquest-backend:local .
docker build --tag polyuquest-frontend:local frontend
```

## 3. 运行契约

后端镜像必须在以下约束中运行 `agent-rag-container-check`：

- 用户固定为 `10001:10001`；
- 根文件系统只读；
- 仅 `/app/data/runtime`、`/app/data/cache`、`/home/app/.cache` 和 `/tmp`
  使用临时可写挂载；
- API、Worker 和 contract console script 均存在；
- API/Worker Python 模块可导入；
- `/app` 本身不可写。

输出文件 `backend/container-contract.json` 的 `ok` 必须为 `true`。前端在相同用户和只读约束下
启动 standalone server，并在 60 秒内返回首页 HTTP 2xx。

## 4. 审阅 CI 证据

每次运行上传 `container-supply-chain-<commit SHA>` artifact，默认保留 14 天：

```text
artifacts/
├── buildx-version.txt
├── trivy-version.txt
├── gate-status.txt
├── backend/
│   ├── build-digest.txt
│   ├── build-metadata.json
│   ├── image-inspect.json
│   ├── container-contract.json
│   ├── sbom.cdx.json
│   └── vulnerabilities.json
└── frontend/
    ├── build-digest.txt
    ├── build-metadata.json
    ├── image-inspect.json
    ├── index.html
    ├── container.log
    ├── sbom.cdx.json
    └── vulnerabilities.json
```

审阅顺序：

1. 确认两个 build digest 非空且对应目标 commit；
2. 确认后端 contract `ok=true`，前端日志无权限或只读文件系统错误；
3. 查看 vulnerability JSON 中 HIGH/CRITICAL 的 package、installed/fixed version 与来源层；
4. 对照 CycloneDX SBOM 判断漏洞是否确实存在于最终镜像；
5. 只有存在“有修复版本的 CRITICAL”时门禁自动失败，HIGH 暂时需要人工 triage。

## 5. 更新 Action 或 Trivy

所有版本更新必须在同一变更中完成：

1. 从官方 release/commit 页面确认版本和完整 40 位 Action commit SHA；
2. 从官方 Trivy release 下载页确认版本，并独立计算 Linux AMD64 archive SHA-256；
3. 更新 `configs/supply_chain.json`；
4. 更新 workflow 中对应 Action pin；
5. 运行本地 validator 与测试；
6. 检查 GitHub runner 的真实构建、contract、SBOM 和扫描结果。

禁止为了恢复绿色状态改成 major tag、`:latest`、`continue-on-error` 或移除 checksum。扫描器下载或漏洞库
短时不可用时，先重试；持续故障应记录 incident，并在有时限和责任人的情况下评审临时处置。

## 6. 漏洞处置

- **Fixable CRITICAL**：更新基础镜像或依赖，重新构建；未通过不得发布。
- **Unfixed CRITICAL**：当前保留证据并人工评估暴露面；后续通过 VEX/到期豁免治理。
- **HIGH**：按 OS/application、是否可达、是否有修复版本建立 backlog；形成稳定基线后再启用增量预算。
- **误报**：保留原报告、package path、分析依据、owner 和到期时间，不直接从报告中删除。

## 7. 已知边界

本门禁证明源码可形成满足基础安全契约的 Linux AMD64 镜像，但不是发布签名：

- 不推送 registry，因此 artifact 中的 build digest 不能替代 registry digest；
- 不生成 GitHub artifact attestation 或签名 provenance；
- 不执行真实数据库、模型与网页探索端到端测试；
- 不验证备份恢复；
- 不覆盖 ARM64。

下一阶段应增加受保护 tag 的 release workflow：推送 GHCR digest、生成 provenance/SBOM attestation，并让部署
环境按 digest 拉取和验证。
