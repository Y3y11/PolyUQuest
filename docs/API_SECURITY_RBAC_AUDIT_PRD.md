# API 安全边界、RBAC 与审计 PRD

## 1. 迭代目标

为面向企业内网、机构官网和受控知识门户部署的 Agent-RAG 服务建立最小可用的 API 安全边界：

1. 生产环境禁止匿名启动；
2. 使用不保存明文的 API Key 完成服务到服务认证；
3. 以 `reader / operator / admin` 角色约束查询、运维读取和有副作用操作；
4. 对受保护请求形成不含原始密钥、问题、回答和网页正文的持久化安全审计；
5. 认证/审计故障不泄露凭据，审计存储故障不拖垮核心服务；
6. 本地开发默认保持兼容，生产浏览器接入明确通过 BFF/SSO/API Gateway，而不是把静态 Key 放进前端 Bundle。

本轮定位是单租户部署安全 MVP，不声称已实现企业完整 IAM、多租户数据隔离或终端用户 SSO。

## 2. 业务背景与风险

PolyUQuest 原算法框架主要验证结构感知检索质量；演进后的系统已经能够在线抓取、增量入图、刷新知识、重试任务和执行跨存储修复。这些能力进入真实机构环境后，API 不再只是“展示 Demo”：

- Query/Graph 可能暴露内部知识和关系；
- Telemetry、Fact History 和 Page Version 会暴露运行与知识演进元数据；
- Retry、Refresh、Pause/Resume、Reconciliation Execute 会改变后台状态；
- `confirm=true` 只能防误点击，不能证明调用者有权限；
- CORS 只约束浏览器跨域行为，不是认证和授权机制；
- 把静态 API Key 放进 Next.js 客户端等同于公开凭据。

因此需要先建立服务边界和职责分级，再继续做生产部署、告警与多租户治理。

## 3. 修改前现状与主要问题

### 3.1 所有 API 匿名可访问

除持久化环境开关和 reconciliation 的确认参数外，路由没有 Principal、Credential 或 Role 概念。任何能访问端口的调用者都可以读取图/Telemetry，触发刷新、重试和修复。

### 3.2 配置无法阻止生产误用

没有显式 `APP_ENVIRONMENT` 与 `API_AUTH_MODE`。开发默认配置若直接复制到生产，服务仍可匿名启动，不会 fail fast。

### 3.3 明文 Key 容易进入配置与日志

若只增加 `API_KEYS=plaintext`，密钥会出现在 `.env`、错误消息、Debug 输出和进程环境排查记录中。认证存储必须只接收 Key 的单向摘要，并用恒定时间比较。

### 3.4 只有确认，没有职责分离

读查询、查看队列、暂停刷新与执行修复风险不同。单一“已登录”布尔值无法实施最小权限。

### 3.5 缺少安全审计

当前 Telemetry 解释 Agent/Worker 性能，但不记录“哪个服务身份以什么角色访问了哪个路由、结果是允许还是拒绝”。同时不能为了安全审计把原始 Key、Query 或正文写入日志。

## 4. 身份与凭据模型

### 4.1 API Key 配置

环境变量：

```text
API_AUTH_MODE=disabled | api_key
API_AUTH_KEYS=key_id:role:sha256_hex[,key_id:role:sha256_hex...]
```

约束：

- `key_id` 只允许小写字母、数字、点、下划线和连字符；
- `role` 只允许 `reader / operator / admin`；
- 摘要必须为 64 位小写 SHA-256；
- Key ID 与摘要均不得重复；
- `api_key` 模式至少包含一个 admin，避免无法运维；
- `.env.example` 只能出现占位摘要，不能出现可用 Key；
- 原始 Key 只在部署方生成时出现一次，不写入仓库。

请求使用 `X-API-Key`。服务对收到的 Key 计算 SHA-256，与配置摘要使用 `hmac.compare_digest` 比较；错误响应不区分“不存在”和“不匹配”，防止枚举身份。

### 4.2 Principal

认证成功得到：

- `principal_id`：配置中的 Key ID；
- `role`：reader/operator/admin；
- `auth_mode`：api_key 或 development_bypass。

Principal 只在 Request State 和结构化审计中传递，不进入 Agent Prompt、回答或知识图。

## 5. RBAC 模型

角色具有包含关系：`admin >= operator >= reader`。

### 5.1 Public

- `/api/health/live`；
- `/api/health/ready`；
- `/api/health/dependencies`。

健康探针不携带业务数据，保持公开以兼容容器编排。

### 5.2 Reader

- 旧 `/api/query` 与 streaming；
- `/api/agent/query` 与 streaming；
- Graph 查询与可视化；
- `/api/security/whoami`。

Agent 是否允许在线持久化仍由服务端 `AGENT_ALLOW_PERSISTENCE`、可信域约束和页面质量门控决定。本轮不让前端 Key 直接绕过这些策略。

### 5.3 Operator

- Index Job/Quality/PageVersion/Fact/Reconciliation 状态读取；
- Freshness Target/Stats 读取；
- Telemetry 读取；
- 发起只读 Reconciliation Scan。

### 5.4 Admin

- Retry Index Job；
- Refresh Now、Pause、Resume；
- Execute Reconciliation；
- 查看 Security Audit。

`confirm=true` 与 admin 授权同时存在：前者证明调用者明确确认，后者证明调用者有权执行。

## 6. 启动期安全校验

新增：

- `APP_ENVIRONMENT=development | test | production`；
- `API_AUTH_MODE=disabled | api_key`；
- `API_AUTH_KEYS`；
- `SECURITY_AUDIT_PATH`；
- `SECURITY_AUDIT_RETENTION_DAYS`。

规则：

1. production + disabled 直接启动失败；
2. api_key + 空/非法配置直接启动失败；
3. api_key 模式无 admin 直接启动失败；
4. retention 必须为正数；
5. development/test 可 disabled，产生 `development-bypass` admin Principal，保证本地兼容；
6. 错误消息只能定位配置结构，不能回显摘要之外的请求凭据。

## 7. 安全审计模型

SQLite `security_audit_events` 保存：

- event_id、request_id、created_at；
- principal_id、role、auth_mode；
- method、FastAPI route template；
- required_role、status_code；
- outcome：allowed / unauthorized / forbidden / error。

明确不保存：

- `X-API-Key` 及其请求摘要；
- Query String（可能含 URL 或敏感筛选）；
- Request/Response Body；
- 用户问题、回答、Prompt、证据正文；
- 原始 Client IP。

中间件只审计声明了 `required_role` 的受保护请求；健康检查不写。SQLite 写失败 fail-open，输出不含敏感字段的结构化 warning 并累计 dropped write 计数。

## 8. API 与错误合同

- 无 Key/错误 Key：`401`，`WWW-Authenticate: ApiKey`；
- 已认证但角色不足：`403`；
- 业务资源不存在/冲突：继续使用现有 `404/409`；
- 每个受保护响应返回服务生成的 `X-Request-ID`；
- `/api/security/whoami` 返回当前 Principal；
- `/api/security/audit` 仅 admin，可按 outcome/principal 分页；
- `/api/security/audit/stats` 仅 admin，返回 outcome 计数和 dropped writes。

认证失败不返回 Key ID 猜测、合法角色列表或配置细节。

## 9. 与前端和网关的边界

静态服务 Key 禁止写入 `NEXT_PUBLIC_*`、浏览器 localStorage 或前端 Bundle。生产推荐：

```text
Browser -> Enterprise SSO / Session -> BFF or API Gateway
        -> server-side X-API-Key -> FastAPI
```

BFF/网关负责终端用户认证、会话、CSRF、用户级授权和 Key 轮换；本服务负责工作负载身份和 API 能力分级。开发环境可继续关闭认证，或由本地反向代理注入测试 Key。

## 10. 可观测性与运维

- 认证拒绝使用结构化日志，禁止输出 Credential；
- 安全审计与 Agent Telemetry 分库/分表，避免性能指标查询暴露安全事件；
- 初始化时清理超过 retention 的事件；
- 统计 allowed/unauthorized/forbidden/error 与 dropped writes；
- Key 轮换采用“先添加新摘要、部署、切流、再移除旧摘要”；
- Role/Key 配置变化需要重启，确保单进程内授权视图一致。

## 11. 验收标准

1. production + disabled 无法构造 Settings；
2. 非法、重复、无 admin 的 Key 配置无法启动；
3. 原始 Key 从不写入 SQLite、日志或错误响应；
4. 正确 reader 可访问 Query/Graph，不能访问 Operator/Admin API；
5. operator 可读取运维状态和发起 Scan，不能 Retry/Pause/Execute；
6. admin 可执行有副作用操作，Reconciliation 仍要求 confirm；
7. 健康检查无认证可访问；
8. missing/wrong key 返回 401，角色不足返回 403；
9. audit 使用 route template，不保存 query string/body/client IP；
10. audit store 故障不影响业务响应并可见 dropped write；
11. retention purge 有测试；
12. 现有开发模式 API/全量测试保持兼容；
13. Python 全量测试、定向 Ruff 与前端 TypeScript 通过。

## 12. 非目标

- 不实现 OAuth2/OIDC/SAML/LDAP；
- 不把静态 Key 当作终端用户身份；
- 不实现多租户 Neo4j/Qdrant/SQLite 行级隔离；
- 不实现细粒度到单个 Domain/Entity 的 ABAC；
- 不实现分布式 Rate Limit/WAF/DDoS 防护；
- 不自动轮换或托管 Key；
- 不保存原始 IP、UA、Query 或 Body；
- 不宣称 API Key 可以替代 TLS，生产必须由 HTTPS 网关终止连接。

## 13. 后续方向

1. OIDC/JWT 验签、JWKS 缓存与企业 SSO；
2. BFF 会话、CSRF 和用户级 Permission；
3. tenant/domain/resource scope 与 Neo4j/Qdrant/SQLite 端到端隔离；
4. Vault/KMS/Secret Manager 托管、自动轮换与撤销；
5. Redis/Envoy 分布式限流、并发配额与异常流量检测；
6. Security Audit 导出 SIEM/OpenTelemetry，增加不可变对象存储保留；
7. 管理操作双人审批、break-glass 和临时权限；
8. SBOM、镜像签名、依赖漏洞扫描和部署 NetworkPolicy。
