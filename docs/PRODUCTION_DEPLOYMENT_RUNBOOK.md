# PolyUQuest 生产部署与恢复 Runbook

本 Runbook 对应 `compose.production.yml`。它是单机 Compose 生产基线，默认由同机的 TLS/SSO/API Gateway 把全部浏览器流量转发到 Next.js；Next.js BFF 从 Docker secret 注入 reader key，再通过 internal backend network 调用 FastAPI。Neo4j 与 Qdrant 不发布宿主机端口，API 与前端也只监听 `127.0.0.1`。

API/Worker 通过独立 egress 网络访问模型供应商和可信网页；生产防火墙应限制
允许的域名、协议和 DNS，并记录异常出站。Neo4j/Qdrant 只加入 internal backend。

## 1. 前置条件

- Docker Engine 与 Docker Compose plugin 可用；
- 建议至少 12 GB 可用内存、4 vCPU，并为模型缓存和图/向量数据预留磁盘；
- 已有受管的 TLS/SSO 网关，能够将所有应用流量（包括 `/api`）转发到前端；
- `/api` 不能绕过 Next.js BFF 直达 FastAPI；BFF 负责注入 reader key，不得把长期 key 编译到浏览器 JavaScript；
- 真实 `.env.production` 存储在密钥管理系统或受限目录，不提交 Git。

## 2. 准备配置

```powershell
Copy-Item deploy/.env.production.example deploy/.env.production
python -m agent_rag.security.cli --key-id frontend-reader --role reader
python -m agent_rag.security.cli --key-id operations-admin --role admin
```

把两条 hash-only record 写入 `API_AUTH_KEYS`。reader raw key 单独写入受限文件，并把路径配置为 `BFF_BACKEND_API_KEY_FILE`；admin raw key 只交给运维端。必须替换示例中的 Neo4j 密码、DeepSeek/SiliconFlow key、CORS/BFF origin、secret file 路径和镜像标签。完整步骤见 `docs/BROWSER_BFF_SSE_RUNBOOK.md`。

生产应用会二次校验以下条件并 fail-fast：认证不能关闭、Neo4j 不能使用开发默认密码、reload 不能开启、当前远程 LLM/embedding provider 必须有凭证、waiting 不能超过 active、容量告警比例与部署预算必须合法。API 与 Worker 必须使用相同的 Admission 配置。

首次上线建议保留示例的 `100 active / 80 waiting` 保护上限，再根据真实吞吐逐步收紧。它不是容量承诺；单 Worker 的稳定容量应通过排队等待、P95/P99、模型费用和网页访问额度共同校准。

## 3. 部署前验收

```powershell
.venv\Scripts\python.exe scripts/validate_deployment.py
docker compose --env-file deploy/.env.production -f compose.production.yml config --quiet
docker compose --env-file deploy/.env.production -f compose.production.yml build api frontend
```

第一条不需要 Docker daemon；第二条验证变量展开；第三条才是真实镜像构建。任何一步失败都不得启动或更新生产服务。

## 4. 首次启动与验证

```powershell
docker compose --env-file deploy/.env.production -f compose.production.yml up -d neo4j qdrant
docker compose --env-file deploy/.env.production -f compose.production.yml up -d worker api frontend
.venv\Scripts\python.exe scripts/deployment_ops.py --env-file deploy/.env.production verify
docker compose --env-file deploy/.env.production -f compose.production.yml ps
docker compose --env-file deploy/.env.production -f compose.production.yml logs --tail 200 worker api
```

验收要求：API liveness、readiness 和前端均返回 2xx；日志出现 `worker_process_ready` 且 capabilities 包含 `agent-run`；Neo4j/Qdrant 无宿主机端口；浏览器查询只访问同源 `/api` 且不携带 `X-API-Key`；BFF 先创建 durable Run，再通过带事件 ID 的 SSE 逐步返回并关联 BFF/API request IDs；刷新页面后应恢复同一 run_id，而不是重新执行。以受控小上限执行并发冒烟，确认超限返回 429/Retry-After、完成后恢复接收、同 key 重提仍返回原 run。最后用一条需要在线探索的问题验证 outbox 最终完成和图统计增长。详见 `docs/DURABLE_AGENT_RUN_RUNBOOK.md`。

### 4.1 Prometheus 抓取与容量基准

`GET /api/metrics` 需要 operator/admin key，不应经过面向浏览器的 reader BFF。Prometheus 应位于
受控内网，通过 Secret 注入 `X-API-Key`，建议 15 秒抓取一次；响应只包含固定标签的队列、准入、
Worker 和 telemetry 聚合，不包含问题、网页、Run ID 或 Worker instance ID。出现 scrape error 时先
检查 `polyuquest_metrics_snapshot_refresh_total{outcome="error"}` 与 snapshot age，不要通过提高抓取
频率掩盖 SQLite 或磁盘故障。

发布前可运行不产生外部 API 费用的控制面基准：

```powershell
agent-rag-capacity-benchmark `
  --requests 32 --concurrency 16 `
  --max-active 12 --max-waiting 8 `
  --max-p95-ms 750 `
  --output data/runtime/capacity-benchmark.json
```

只有报告 `status=passed`、所有 assertions 为 true 才能作为控制面门禁通过。该数值不能替代真实
LLM、抓取、Neo4j、Qdrant 与完整 Worker 拓扑的容量测试。

## 5. 常规升级

1. 运行后端、前端测试与离线评测发布门禁。
2. 构建新的显式镜像版本，禁止覆盖旧 tag。
3. 执行冷备份。
4. 更新 `.env.production` 的镜像标签并运行 `compose config`。
5. `docker compose ... up -d` 应用新版本。
6. 运行 verify、查询冒烟和增量入图检查。
7. 保留上一版镜像与备份，直到观察窗口结束。

## 6. 冷备份

备份脚本会依次停止前端、API、Worker 和数据库，打包 Neo4j、Qdrant、runtime SQLite/outbox 与 cache 卷，生成 manifest 和 SHA-256 清单，然后重新启动服务。由此获得一致性清晰但存在停机窗口的基线备份。

```powershell
.venv\Scripts\python.exe scripts/deployment_ops.py `
  --env-file deploy/.env.production `
  --backup-root D:\protected\polyuquest-backups `
  backup
```

要求：备份目录应位于加密、受访问控制且有异地复制的存储；每日执行，至少每月做一次恢复演练。脚本失败后会尝试恢复服务，但不删除不完整备份目录，运维人员应保留它用于排查并使用新的 backup ID 重试。

## 7. 恢复

恢复会覆盖四个目标卷，必须在维护窗口内执行。脚本先检查备份文件是否齐全；容器内再次验证 SHA-256。恢复失败后不会自动启动服务，避免对外提供部分替换的数据。

```powershell
.venv\Scripts\python.exe scripts/deployment_ops.py `
  --env-file deploy/.env.production `
  --backup-root D:\protected\polyuquest-backups `
  restore 20260814T120000Z `
  --confirm I_UNDERSTAND_DATA_WILL_BE_REPLACED
```

恢复后必须运行：

```powershell
.venv\Scripts\python.exe scripts/deployment_ops.py --env-file deploy/.env.production verify
docker compose --env-file deploy/.env.production -f compose.production.yml logs --tail 300 worker api neo4j qdrant
```

随后抽样检查图节点/边数量、Qdrant collection、outbox 非终态任务、最近一次可追溯查询和安全审计记录。

## 8. 回滚与故障处理

- 仅应用故障：把镜像标签改回上一版并 `up -d api worker frontend`，不恢复数据。
- schema 或写入不兼容：停止服务，确认当前数据是否需要额外留档，再恢复升级前备份。
- Worker 卡住：先查看 Agent Run、Index Job 当前租约与 outbox；`docker compose stop worker` 会等待 75 秒。超时取消后，Agent Run 由 lease 重新执行，知识发布由幂等 patch/outbox 和下一次启动恢复。
- 大量 429：先区分正常削峰与 Worker 故障；查看 admission counters、capacity utilization、queue age 和 Worker heartbeat。禁止让客户端无 jitter 高频重试。
- `/api/metrics` 返回 503：确认 runtime SQLite 可读、磁盘空间和权限正常；若已有旧快照，系统会短时返回旧数据并通过 snapshot age/error counter 告警，业务查询不应受影响。
- API live 正常但 ready 失败：检查 embedding warm-up、Neo4j/Qdrant 连通性和模型凭证，禁止仅靠重启掩盖持续错误。
- 磁盘不足：先停止写入，扩容或迁移卷；不要手工删除 Neo4j/Qdrant 文件。

## 9. 已知限制与下一阶段

- 这是单机冷备份方案，不提供零停机与跨区容灾。
- BFF 的共享 reader key 只是工作负载边界；企业浏览器访问仍需要外部 SSO/ingress 识别最终用户。
- Compose 资源限制需要结合实际 corpus、模型与并发压测校准。
- 当前 Admission 是全局容量，不是 tenant/user quota；共享 BFF reader 无法提供可信租户归属。
- 下一阶段应引入镜像 digest/SBOM/签名、CI BuildKit、集中日志指标、Neo4j/Qdrant 官方在线 snapshot/backup 和自动恢复演练。
