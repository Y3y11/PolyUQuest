# PolyUQuest 浏览器安全 BFF 与 SSE 交付 PRD

> 迭代 18 · 2026-08-14 · 状态：实施中

## 1. 业务背景

PolyUQuest 从结构感知图增强 RAG 演进为面向企业内网/机构网站的在线检索 Agent：网页不是一次性离线语料，而是实时知识来源；Agent 在现有图与向量索引证据不足时抓取可信页面，异步增量入库，并由 Freshness Worker 持续更新事实。

迭代 17 已证明独立 FastAPI、Worker、Neo4j、Qdrant 与 Fixture 可以经真实 HTTP/SSE 完成冷查询、Worker 故障接管、热查询和知识更新。但生产用户并不直接调用 FastAPI，而是在浏览器访问 Next.js。浏览器如果直接持有后端服务 Key，会把长期凭据暴露在 bundle、DevTools、浏览器存储和前端日志中；如果只把 API 地址改为 `/api` 而没有 BFF，生产镜像则无法工作。

本轮建立一个最小但真实的服务端 BFF：浏览器只访问同源 Next.js Route Handler；BFF 从运行时 secret file 读取 reader 服务凭据，经过显式路由白名单后调用 FastAPI，并透明传递 SSE。它解决工作负载凭据隔离与生产网络闭环，但不冒充完整企业 IAM。

## 2. 修改前现状与主要问题

### 2.1 浏览器直接决定后端地址

- `frontend/lib/api.ts` 使用 `NEXT_PUBLIC_API_URL`；
- `NEXT_PUBLIC_*` 会进入客户端 bundle，不能承载服务端安全边界；
- 本地默认直连 `http://localhost:8000/api`，依赖 CORS；
- 浏览器无法安全附加生产 `X-API-Key`。

### 2.2 生产镜像声明 `/api`，但没有代理实现

- `frontend/Dockerfile` 在 build 阶段设置 `NEXT_PUBLIC_API_URL=/api`；
- App Router 下不存在 `app/api/**/route.ts`；
- 因此浏览器请求 `/api/agent/query/stream` 会落到 Next.js 404，而不是 FastAPI。

### 2.3 生产网络拓扑不连通

- production Compose 的 frontend 只加入 `frontend` network；
- FastAPI 加入 `backend` 与 `egress`，没有共享 frontend network；
- 即使新增 Route Handler，frontend 容器也无法解析或连接 `api:8000`。

### 2.4 缺少代理安全约束

- 通用 catch-all 代理如果直接拼接用户 path，会成为内部开放代理；
- 没有 Origin/CSRF 检查、请求体上限、上游 timeout 与错误清洗；
- 没有区分后端认证失败、用户请求错误和依赖不可用；
- 没有规定哪些响应头可返回浏览器。

### 2.5 SSE 生命周期没有跨 BFF 验证

- 迭代 17 验证 browser 之外的 HTTP client → FastAPI SSE；
- 未证明 Next.js 不缓冲、不改写事件边界；
- 浏览器停止回答时，取消信号可能只停止前端读取，而不终止 BFF 到 FastAPI 的上游请求；
- 缺少 BFF request ID 与 FastAPI request ID 的关联。

## 3. 迭代目标

1. 客户端所有 PolyUQuest API 请求固定使用同源 `/api`，不再读取 `NEXT_PUBLIC_API_URL`；
2. 新增 Node.js Route Handler BFF，只代理显式允许的 reader 路由与方法；
3. BFF 在服务端从 secret file 读取 raw reader key，并注入 `X-API-Key`；
4. 生产模式禁止从普通环境变量读取 raw key；本地 development 可显式使用临时 key 或匿名后端；
5. 对 POST 执行精确 Origin allowlist，拒绝无 Origin/跨站请求；
6. 限制实际请求体大小，设置整个上游响应生命周期 timeout；
7. SSE 保持事件顺序、`text/event-stream`、`no-store/no-transform`，不聚合完整响应；
8. 下游取消时取消 reader 并 abort 上游 fetch；
9. 清洗后端 401/403/5xx，不把服务凭据、内部地址或任意响应头暴露给浏览器；
10. 修复 production Compose 网络与 secret mount，并提供机器可验证合同和远程门禁。

## 4. 非目标

- 本轮不实现 OIDC、SAML、企业 SSO 登录页或用户目录；
- 不把 same-origin/Origin allowlist 描述为用户身份认证；
- 不在 BFF 暴露 operator/admin、刷新、修复、审计或 Worker 管理接口；
- 不实现多租户 namespace、用户级 RBAC 或行级数据隔离；
- 不改变 FastAPI 的 API key/hash、RBAC 与安全审计实现；
- 不改变 Agent 检索、抓取、增量入图和知识更新算法；
- 不进行视觉重设计；浏览器自动化只验证交付链路与流式状态。

## 5. 威胁模型与安全边界

### 5.1 受保护资产

- FastAPI reader service key；
- 内部 API DNS/端口；
- Agent 查询、图查询与 SSE 运行记录；
- 上游错误、响应头和 request correlation metadata。

### 5.2 主要威胁

- raw key 被编译到 bundle 或传回浏览器；
- 攻击者利用 catch-all path 访问 operator/admin 接口；
- 跨站页面利用用户浏览器提交昂贵 Agent 查询；
- 超大 body/长时间 stream 占满 BFF 资源；
- 客户端断开后上游 Agent 仍运行；
- 后端认证错误或内部异常正文泄漏部署细节。

### 5.3 信任边界

```text
Browser (untrusted input, no service secret)
  -> same-origin Next.js BFF (route allowlist, Origin, size, timeout)
  -> FastAPI reader capability (API key + RBAC + audit)
  -> Agent / graph / storage / trusted web
```

生产入口仍必须由企业反向代理/SSO 保护。BFF 当前只证明“浏览器不持有工作负载 Key”和“reader capability 不可越权”，不证明最终用户是谁。

## 6. 方案与技术选型

### 6.1 Next.js App Router Route Handler

使用 `app/api/[...path]/route.ts` 作为同源 HTTP 边界，选择 Node.js runtime：

- 需要读取 Docker secret file；
- 使用标准 Fetch/ReadableStream 透明代理；
- 不引入第二套 Express server；
- 保持 standalone Next.js 镜像结构。

### 6.2 显式能力白名单

允许：

- `POST query`；
- `POST query/stream`；
- `POST agent/query/stream`；
- `GET graph/stats`；
- `POST graph/data`；
- `GET graph/neighbors/{node_id}`；
- `GET graph/path`；
- `GET graph/layered_slice`；
- `GET health`。

路径规则必须完整匹配，禁止 `..`、encoded slash、空 segment 与额外后缀。未列出的 route 返回 404；允许 path 使用错误 method 返回 405。

### 6.3 Secret file 优先

生产 Compose 通过只读 Docker secret 挂载 raw reader key，容器环境只保存文件路径。BFF 启动配置：

- `BACKEND_API_URL=http://api:8000/api`；
- `BFF_BACKEND_API_KEY_FILE=/run/secrets/bff_backend_api_key`；
- `BFF_ALLOWED_ORIGINS=https://...`；
- `BFF_MAX_REQUEST_BYTES=65536`；
- `BFF_UPSTREAM_TIMEOUT_SECONDS=120`。

`NODE_ENV=production` 下没有 secret file 或 key 为空时 fail closed。development 可使用 `BFF_BACKEND_API_KEY`，也可连接关闭 API auth 的本地后端。

普通 Compose 的 file secret 是 bind mount，宿主机文件必须允许非 root frontend
（UID/GID 10001）读取。Linux 基线为 `root:10001`、`0440`；不能使用仅 root/部署用户
可读的 `0600`，也不能放宽为全局可读 `0444`。配置加载失败只向浏览器返回稳定 503，
服务端记录不含 raw key 的结构化原因。

### 6.4 SSE 透明流

BFF 不解析 Agent 事件，不维护第二套 SSE schema。它只：

- 验证上游 `Content-Type`；
- 逐 chunk enqueue 到下游 ReadableStream；
- 设置 `Cache-Control: no-store, no-transform` 与 `X-Accel-Buffering: no`；
- 返回 BFF 与 upstream request ID；
- 在下游 cancel、请求 abort、timeout 或 read error 时取消 reader 和上游 controller。

### 6.5 错误映射

- BFF 配置缺失：503 `bff_not_configured`；
- Origin 不允许：403 `origin_forbidden`；
- 路由/方法不允许：404/405；
- 请求体过大：413 `request_too_large`；
- FastAPI 400/409/422/429：保留状态，返回有界、清洗后的 detail；
- FastAPI 401/403：502 `backend_authentication_failed`；
- timeout：504 `backend_timeout`；
- 连接/读取失败：502 `backend_unavailable`。

## 7. 接口与配置合同

浏览器请求路径保持与 FastAPI `/api` 子路径一致，因此现有 `lib/api.ts` 只需把 base 固定为 `/api`。

BFF 只转发：

- method；
- allowlisted path 与原始 query string；
- `Content-Type`、`Accept`；
- 有界 body；
- BFF 生成的 `X-Request-ID`；
- server-only `X-API-Key`。

BFF 不转发浏览器提供的 Authorization、Cookie、X-API-Key、Host、Forwarded 或任意 hop-by-hop header。

返回浏览器的 header allowlist：

- `Content-Type`；
- `Cache-Control`；
- `X-Request-ID`；
- `X-BFF-Request-ID`；
- SSE 专用 `X-Accel-Buffering`。

## 8. 生产拓扑与部署

production Compose 调整：

- frontend 同时加入 `frontend` 与 internal `backend` network；
- API 仍不暴露到 frontend/public network；
- frontend 只通过 `api:8000` 访问 FastAPI；
- raw key 通过 `BFF_BACKEND_API_KEY_FILE` 挂载，不进入 `NEXT_PUBLIC_*`；
- backend 的 `API_AUTH_KEYS` 继续只保存 reader/admin SHA-256 records；
- reader key 与其 hash 必须由同一 secret provisioning 流程生成并轮换。

## 9. 测试与验收标准

| 边界 | 验收标准 |
|---|---|
| 客户端 bundle | 不包含 `NEXT_PUBLIC_API_URL`、raw service key 或内部 API URL |
| 路由白名单 | reader routes 成功；operator/admin/未知路径 404；错误 method 405 |
| Origin | production POST 缺失或不在 allowlist 返回 403，且不调用 upstream |
| Body | Content-Length 与实际读取均执行上限，超限返回 413 |
| Key 注入 | upstream 收到配置 key；browser response/request fixture 不出现 key |
| SSE | chunk/事件顺序不变；响应为 event-stream；禁止缓存和代理缓冲 |
| 取消 | client cancel 后 upstream AbortSignal 为 aborted |
| timeout | 超时返回/终止为 504 或 stream abort，资源释放 |
| 错误清洗 | backend 401/403/5xx 不透传内部正文与敏感 header |
| Compose | frontend 可访问 internal API，key 通过 secret file，配置可渲染 |
| 构建 | `npm test`、TypeScript、Next standalone build 全部通过 |
| 远程门禁 | Linux runner 启动真实 frontend + controlled upstream，机器报告通过 |

## 10. 文件级修改计划

新建：

- `frontend/lib/server/bffConfig.ts`；
- `frontend/lib/server/bffProxy.ts`；
- `frontend/app/api/[...path]/route.ts`；
- `frontend/__tests__/bffProxy.test.ts`；
- `docs/BROWSER_BFF_SSE_PRD.md`；
- `docs/BROWSER_BFF_SSE_RUNBOOK.md`；
- BFF 合同/运行时验证脚本与 GitHub workflow。

修改：

- `frontend/lib/api.ts`：固定 same-origin `/api`；
- `frontend/Dockerfile`：移除 public API 地址 build arg；
- `compose.production.yml`：backend network、server-only config 与 secret mount；
- `.env.example`、README：BFF 开发/生产配置；
- deployment/supply-chain validators：保护 BFF secret 与网络合同；
- 本地迭代文档：记录现状、缺陷、修复、自测和远程证据。

## 11. 风险与应对

- **BFF 被误认为完整登录系统**：文档与 UI/运维明确标注仍需企业 SSO/ingress；
- **SSE 被平台缓冲**：设置 no-transform/no-buffering，并用多 chunk 时间证据验证；
- **长 Agent 占用 Node connection**：有界 timeout、取消传播与并发/限流后续接入；
- **secret/hash 不匹配**：readiness smoke test 必须实际通过 FastAPI reader route；
- **file secret 权限不匹配**：Linux provisioning 固定 `root:10001/0440`，E2E 使用与生产
  相同的非 root UID/GID 验证真实读取；
- **catch-all 扩大攻击面**：完整正则 allowlist，拒绝未知 path/method，不转发用户鉴权头；
- **Next build 读取运行时 secret**：配置延迟到请求时加载，不在 builder stage 要求 secret；
- **本地开发复杂**：development 默认 backend URL + 无 key兼容匿名本地 API。

## 12. 回滚方案

BFF 改动不改变 FastAPI 接口。若 Route Handler 出现兼容问题，可在受控开发环境临时通过反向代理把 `/api` 指向 FastAPI；生产不回退为 `NEXT_PUBLIC_API_URL + raw key`。Compose 回滚只移除 frontend 的 backend network 与 secret mount，不影响 API/Worker/存储卷。

## 13. 后续方向

1. 接入企业 OIDC/SAML，会话使用 HttpOnly/Secure/SameSite cookie；
2. 把 Principal/tenant claims 转换为短期 JWT，而不是共享 reader key；
3. Redis/Postgres 分布式 rate limit、并发 quota 与成本预算；
4. SSE Last-Event-ID/持久化 run 支持断线恢复；
5. 浏览器 Playwright 视觉与无障碍门禁；
6. 多 frontend replica 下的 session、trace 与 cancel 一致性；
7. 对 BFF/API/Worker 建立统一 OpenTelemetry trace；
8. 将本轮纳入完整工程故事、简历描述和面试材料。
