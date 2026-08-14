# PolyUQuest 浏览器 BFF 与 SSE 运行手册

本手册对应迭代 18。BFF 是 Next.js App Router Route Handler，职责是把同源浏览器请求限制到 reader 路由、从服务端 secret file 注入 FastAPI key，并透明转发 SSE。它不替代企业 SSO、WAF、用户会话或多租户授权。

## 1. 本地开发

后端使用开发模式且关闭 API auth 时：

```powershell
Copy-Item frontend/.env.local.example frontend/.env.local
cd frontend
npm ci
npm run dev
```

浏览器只请求 `http://localhost:3000/api/...`。Next.js 服务端再访问
`BACKEND_API_URL=http://127.0.0.1:8000/api`。不要再设置
`NEXT_PUBLIC_API_URL`，也不要在浏览器 localStorage、cookie 或源码中保存服务 Key。

若本地 FastAPI 启用 `API_AUTH_MODE=api_key`：

1. 生成 reader key；
2. 把 hash-only reader record 加入 FastAPI `API_AUTH_KEYS`；
3. 只在本地 `frontend/.env.local` 写入临时 raw key 的 `BFF_BACKEND_API_KEY`；
4. 生产环境禁止使用该普通环境变量，必须改用 secret file。

## 2. 生产凭据准备

分别生成 reader 与 admin 能力：

```powershell
python -m agent_rag.security.cli --key-id frontend-reader --role reader
python -m agent_rag.security.cli --key-id operations-admin --role admin
```

- 两条 hash-only record 以逗号连接写入 `API_AUTH_KEYS`；
- frontend reader 的 raw key 单独写入受限文件，例如
  `D:\protected\polyuquest\frontend-reader.key`；
- 文件只包含一行 raw key，不提交 Git；Linux Compose 宿主机使用
  `root:10001` 与 `0440`，只允许 root 和 frontend 容器组读取；
- 文件末尾可以有一个换行，但 `API_AUTH_KEYS` digest 必须针对去除该换行后的 raw key
  计算，不能直接对整个文件字节执行 `sha256sum`；
- admin raw key 不交给 BFF，仅交给受控运维工具或 secret manager。

`.env.production` 至少配置：

```dotenv
BFF_BACKEND_API_KEY_FILE=D:\protected\polyuquest\frontend-reader.key
BFF_ALLOWED_ORIGINS=https://knowledge.example.com
BFF_MAX_REQUEST_BYTES=65536
BFF_UPSTREAM_TIMEOUT_SECONDS=120
```

Compose 把文件挂载为 `/run/secrets/bff_backend_api_key`。容器环境只能看到文件路径，
`docker inspect` 不应出现 raw key。

Linux 宿主机示例：

```bash
sudo chown root:10001 /etc/polyuquest/secrets/frontend-reader.key
sudo chmod 0440 /etc/polyuquest/secrets/frontend-reader.key
```

普通 Compose 的 file secret 是 bind mount，会保留宿主机权限；如果仍使用 `0600`
且文件属于部署用户/root，UID/GID 10001 的 frontend 会读不到文件并返回
`503 bff_not_configured`。不要用 `0444` 绕过问题。Windows Docker Desktop 应在部署前
进入生产 frontend 容器验证该路径可读，并确保宿主机 ACL 仅授予部署账号/Docker 服务。

## 3. 反向代理与网络

- TLS/SSO 网关把所有用户流量转发到 frontend `127.0.0.1:3000`；
- `/api` 也必须转发到 frontend，不能绕过 BFF 直达 FastAPI；
- FastAPI `127.0.0.1:8000` 只供本机运维或可信服务使用；
- Compose 内 frontend 通过 internal backend network 访问 `api:8000`；
- Neo4j/Qdrant 仍只在 backend network，不发布宿主机端口。

生产仍必须由 SSO/ingress 识别最终用户。当前 BFF 注入的是共享 reader 工作负载身份，
FastAPI 审计能记录 `frontend-reader`，但不能区分具体员工。

## 4. 部署前检查

```powershell
.venv\Scripts\python.exe scripts/validate_deployment.py
docker compose --env-file deploy/.env.production -f compose.production.yml config --quiet
cd frontend
npm test
npm exec tsc -- --noEmit --incremental false
npm run build
```

必须确认：

- `frontend/lib/api.ts` 固定为 `/api`；
- frontend Docker image/client chunks 不包含 `NEXT_PUBLIC_API_URL`、内部 API URL 或本次
  E2E raw key 的公开 canary 前缀；driver 不读取 secret file；
- Compose frontend 同时位于 frontend/backend network；
- frontend 只挂载 `bff_backend_api_key` secret；
- FastAPI 的 reader hash 与 secret file raw key 匹配。

## 5. 冒烟验证

部署后从浏览器执行一个 Agent 查询，检查：

1. 请求 URL 为同源 `/api/agent/query/stream`；
2. 浏览器 request headers 中没有 `X-API-Key`；
3. response 为 `text/event-stream`；
4. 依次出现 `run_started/action/.../done`；
5. response headers 包含 `Cache-Control: no-store, no-transform`、
   `X-Accel-Buffering: no`、`X-BFF-Request-ID` 与 `X-Request-ID`；
6. FastAPI security audit principal 为配置的 reader key id；
7. 浏览器点击 Stop 后，上游请求及时结束。

负向检查：

- 非 allowlist Origin 的 POST 返回 403；
- `/api/workers/status`、refresh、repair 和 audit 等接口经 BFF 返回 404；
- 超过 64 KiB 的 body 返回 413；
- reader/hash 不匹配时浏览器只看到 `backend_authentication_failed`，看不到内部正文。

## 6. 故障排查

### `503 bff_not_configured`

检查 `BACKEND_API_URL`、`BFF_ALLOWED_ORIGINS`、secret mount 路径、文件内容和容器
UID/GID 10001 的读取权限。生产环境不会回退读取 `BFF_BACKEND_API_KEY`；服务端日志会
记录不包含 raw key 的 `bff_configuration_invalid` 原因。

### `502 backend_authentication_failed`

reader raw key 与 `API_AUTH_KEYS` digest 不匹配，或 key 被撤销。重新生成/轮换成对凭据，不要把 admin key 临时塞给 BFF。

### `502 backend_unavailable`

检查 frontend 是否加入 backend network、`api` DNS、FastAPI readiness 与容器日志。BFF 会清洗上游 5xx 正文，应使用 request IDs 在服务端关联日志。

### `403 origin_forbidden`

浏览器实际 Origin 必须与 `BFF_ALLOWED_ORIGINS` 完全一致，包括 scheme 与 port。不要用 `*`。

### SSE 一次性返回或被截断

确认外部反向代理关闭 buffering/compression，尊重 `X-Accel-Buffering: no` 与
`Cache-Control: no-transform`，并把 idle timeout 设置得高于 BFF/Agent budget。

## 7. Key 轮换

1. 生成新的 reader raw key/hash；
2. FastAPI `API_AUTH_KEYS` 暂时同时保留新旧 reader digest；
3. 原子替换 secret file 并重建 frontend；
4. 冒烟确认新 principal；
5. 删除旧 digest 并重启 FastAPI；
6. 检查 security audit 中没有旧 key id 新请求。

共享 reader key 是 MVP 工作负载凭据。接入 OIDC 后，应替换为短期、带用户/tenant claims 的 token，而不是无限扩展共享 key。
