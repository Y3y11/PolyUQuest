# PolyUQuest 持久化 Agent Run 运行手册

本手册对应迭代 19。目标是让查询任务独立于浏览器/SSE 连接存活，并能按事件游标恢复。当前实现面向单机 Compose：API 与独立 Worker 共享 `runtime_data` 中的 SQLite WAL。多主机或多租户部署必须先迁移到 PostgreSQL/Redis 并增加 run ownership。

## 1. 运行合同

- 浏览器只调用同源 Next.js BFF；
- `POST /api/agent/runs` 只持久化请求并返回 202；
- 独立 Worker claim、heartbeat、执行、重试和写终态；
- GET events 是观察通道，断开不会取消任务；
- `POST .../cancel` 才表示业务取消；
- `done event + result_json + completed` 在同一事务提交；
- 当前为 at-least-once，知识发布依赖 GraphPatch/Outbox 幂等。

## 2. 必需配置

开发环境可由 FastAPI lifespan 同进程启动 Run Worker：

```dotenv
AGENT_RUN_STORE_PATH=data/runtime/agent_runs.sqlite3
AGENT_RUN_WORKER_ENABLED=true
```

生产必须分离职责：

```text
api:    AGENT_RUN_WORKER_ENABLED=false
worker: AGENT_RUN_WORKER_ENABLED=true
both:   AGENT_RUN_STORE_PATH=/app/data/runtime/agent_runs.sqlite3
```

`scripts/validate_deployment.py` 会阻断 API 错误执行 Worker、Worker 未启用或共享路径不一致。Run Store 保存 query/history/answer/evidence，runtime volume 必须受 UID 10001 权限和磁盘加密保护，默认终态保留 7 天。

## 3. API 冒烟

以下示例直接访问 FastAPI，生产中浏览器不得复制这种 service key 用法：

```powershell
$headers = @{
  "X-API-Key" = $env:POLYUQUEST_READER_KEY
  "Idempotency-Key" = "ops-$([guid]::NewGuid().ToString('N'))"
  "Content-Type" = "application/json"
}
$body = @{
  query = "What is the current application process?"
  explore_web = $true
  persist_discoveries = $true
} | ConvertTo-Json
$run = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/agent/runs -Headers $headers -Body $body
$run
```

查询状态与取消：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000$($run.status_url)" -Headers @{"X-API-Key"=$env:POLYUQUEST_READER_KEY}
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000$($run.cancel_url)" -Headers @{"X-API-Key"=$env:POLYUQUEST_READER_KEY}
```

SSE 客户端应保存每个 `id:`，断线后发送 `Last-Event-ID`。不要把网络 abort 等同 cancel。

## 4. 运维检查

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/agent/runs/stats -Headers @{"X-API-Key"=$env:POLYUQUEST_READER_KEY}
docker compose --env-file deploy/.env.production -f compose.production.yml logs --tail 200 worker api
```

重点观察：

- `queued/retry` 是否持续增长；
- `oldest_waiting_seconds` 是否超过正常查询等待；
- Worker 日志是否有 `agent_run_worker_started/completed/lease_lost`；
- 同一 run 的 attempts 是否反复增加；
- runtime volume 是否空间不足或只读；
- API 与 Worker 是否确实挂载同一 volume/path。

## 5. 故障处理

### Run 长期 queued

检查 Worker 容器、`AGENT_RUN_WORKER_ENABLED=true`、共享数据库路径和文件权限。不要重新提交不同 idempotency key；原 Run 在 Worker 恢复后会被消费。

### SSE 断开或页面刷新

同页客户端自动用 `Last-Event-ID` 重连；刷新后从 sessionStorage 取回 run_id 并重放。Run snapshot 为 completed 时结果仍可读取。若 run 已因 retention 清理，API 返回 404，应建立新的业务请求。

### Worker 执行中崩溃

等待 lease 到期，新 Worker 会以新 attempt 完整重跑。旧 attempt 事件保留；旧 owner 的写入会被 CAS 拒绝。不要手工把 running 改 queued。

### 取消后仍有外部请求

取消是 cooperative best-effort；已进入同步线程或供应商的请求无法强杀。等待当前调用返回，任何迟到知识写入仍由 Patch/Outbox 幂等保护。

### SQLite busy 或磁盘满

先停止接收新任务并扩容/恢复文件系统；不要删除 WAL/SHM 文件或在线复制单个 sqlite 文件。持续写竞争意味着已超过单机 SQLite 边界，应迁移 PostgreSQL。

## 6. 备份、恢复与回滚

现有冷备份包含整个 `runtime_data`，因此包含 Agent Run Store。恢复必须在 API/Worker 均停止后进行。应用回滚时可以让前端临时恢复旧 `/agent/query/stream`，停止 Agent Run Worker并保留数据库用于审计，不要直接删除 active Run。

## 7. 验收清单

- 同一 idempotency key + 同一请求返回同一 run；不同请求返回 409；
- SSE 包含单调 id，断线补发无重复执行；
- 关闭浏览器后 snapshot 仍从 running 进入 completed；
- queued/running cancel 均进入 cancelled；
- 杀死 Worker 后 lease 到期能由新实例接管；
- API 生产进程不运行 Agent Run loop；
- BFF 不转发浏览器 X-API-Key，只转发校验后的 Idempotency-Key/Last-Event-ID；
- Python、Vitest、TypeScript、deployment validator 全绿。

## 8. 后续升级触发条件

出现多主机部署、并发写锁等待、单租户共享 reader 不再成立、需要历史任务中心或步骤级恢复时，迁移 PostgreSQL durable queue、OIDC tenant ownership、事件总线与 checkpoint。不要通过无限增大 SQLite timeout 掩盖架构边界。
