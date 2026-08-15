# PolyUQuest 可信身份传递与 Agent Run 所有权隔离 PRD

## 1. 迭代背景与产品定位

PolyUQuest 已具备面向 Web 的查询驱动检索、增量入图、持久化 Agent Run、独立 Worker、BFF 安全边界与分布式追踪。服务间 `X-API-Key` 能证明调用方是 BFF、运维脚本或内部服务，却不能证明当前浏览器背后的终端用户是谁。原有 Run 表也没有所有者字段，因此任何持有 reader 工作负载凭据且知道 `run_id` 的调用方都能读取事件、结果或发起取消。

本轮把“服务可以调用 API”和“用户可以操作某个 Run”拆成两个正交授权维度：工作负载继续使用 API Key；终端用户由上游企业 OIDC/SSO 网关认证，经过 BFF 验证和重签后传给 FastAPI。持久化 Run 按 `tenant_id + subject` 隔离。该能力是企业内网知识 Agent 的身份基线，不替代企业身份提供商，也不在本轮扩展为知识对象级多租户 ACL。

设计遵循 [RFC 7519](https://www.rfc-editor.org/rfc/rfc7519.html) 的 JWT claims 语义、[RFC 8725](https://www.rfc-editor.org/rfc/rfc8725.html) 的算法固定、显式类型、issuer/audience 校验与强密钥要求，并采用 [OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0-final.html) 中 `iss + sub` 作为稳定用户标识的原则。

## 2. 修改前现状与主要问题

### 2.1 工作负载身份不能代表终端用户

- 浏览器只访问 Next.js BFF，BFF 使用统一 reader key 调用 FastAPI；
- FastAPI `Principal` 只有 workload key ID 和角色；
- 多个真实用户在 API 看来是同一个 BFF，无法做资源所有权判断；
- 直接信任 `X-User-ID` 等浏览器 header 会形成可伪造边界。

### 2.2 持久化 Run 没有所有者

- `agent_runs` 只有 `run_id`、请求、状态、结果和执行元数据；
- `GET /runs/{id}`、SSE replay、cancel 只按 `run_id` 查询；
- 知道合法 ID 的 reader 可以操作任意用户 Run；
- 404 与 403 未形成防枚举语义。

### 2.3 幂等键是全局唯一

- 浏览器常将幂等键持久化或按本地动作生成；
- 不同用户复用同一 key 会错误地命中同一 Run 或产生冲突；
- 直接改成 `(owner, key)` 复合唯一约束需要重建 SQLite 表，升级风险较高。

### 2.4 身份重放和 token confusion 缺少约束

- 没有固定 `alg`、`typ`、issuer、audience 和最大 TTL；
- 同一类 token 若被不同服务接受，可能发生跨 JWT 场景替换；
- 长期 token 泄露会扩大攻击窗口。

### 2.5 不能把 Run 隔离等同于知识库 ACL

Neo4j 页面 URL、Block ID、Entity ID 及 Qdrant point ID 当前是全局标识。若本轮只给查询加 `tenant_id` 过滤，写入、更新和删除仍会发生跨租户主键碰撞，形成“看似隔离、实际共享”的错误安全保证。因此知识对象 ACL 必须作为独立迁移迭代处理。

## 3. 迭代目标与非目标

### 3.1 目标

1. 区分 workload principal 与 end-user identity；
2. 由企业网关签发短时 gateway assertion，BFF 验证后重签 API-only assertion；
3. 对两种 JWT 使用不同 `typ`、issuer、audience 和密钥；
4. 生产 BFF/API 缺失身份配置时 fail closed；
5. 为 Agent Run 增加 tenant 和 owner；
6. create/get/events/cancel 全部执行所有者作用域查询；
7. 越权与不存在统一返回 404；
8. 幂等键按租户和用户作用域化；
9. 对已有 SQLite 数据执行无损、幂等迁移；
10. 保持 Worker 内部 claim/lease/complete 流程不依赖用户 token；
11. 将身份门禁纳入 BFF 和生产拓扑 E2E；
12. 提供密钥轮换、故障诊断和回滚 Runbook。

### 3.2 非目标

- 不实现用户名密码、OAuth authorization code flow 或企业 IdP；
- 不把浏览器 session/cookie 直接交给 FastAPI；
- 不向前端暴露内部 assertion；
- 不实现 Neo4j/Qdrant/BM25 的知识对象 ACL；
- 不改变同步、非持久化问答接口的资源所有权语义；
- 不把 groups 用于知识权限判断；本轮只做有界解析和透传；
- 不实现 JWT 单次使用的分布式 `jti` 黑名单；短 TTL 限制重放窗口；
- 不在日志、trace 或响应中输出 token 和完整 claims。

## 4. 用户角色与业务场景

| 角色 | 场景 | 预期行为 |
|---|---|---|
| 已登录员工 Alice | 创建并恢复自己的长任务 | 相同幂等键重试命中 Alice 原 Run |
| 同租户员工 Bob | 使用与 Alice 相同的浏览器幂等键 | 创建 Bob 独立 Run，不冲突、不复用 Alice Run |
| 非所有者 | 猜测或获得他人 `run_id` | snapshot、SSE、cancel 均返回相同 404 |
| 企业身份网关 | 完成 OIDC 登录后转发请求 | 注入 60 秒内有效的 gateway assertion |
| Next.js BFF | 承接浏览器请求 | 校验网关 token，丢弃浏览器内部 token，重签 API token |
| FastAPI | 接收 BFF 请求 | 同时验证 reader API key 和 end-user assertion |
| Worker | 后台执行 Run | 依据数据库租约工作，不持有或解析用户 token |
| 运维人员 | 检查 stats/health | 使用 operator workload key，不枚举用户请求正文或身份 token |

## 5. 功能需求

### 5.1 网关到 BFF

- header：`X-PolyUQuest-Gateway-Identity`；
- JWT header 必须精确为 `alg=HS256`、`typ=polyuquest-gateway+jwt`；
- claims 必须精确包含 `v, iss, aud, sub, tenant_id, groups, iat, exp, jti`；
- `iss=polyuquest-gateway`、`aud=polyuquest-bff`；
- TTL 不超过 120 秒，默认上游建议 60 秒；
- subject/tenant/group 使用长度、字符集、数量和去重上限；
- 签名、有效期、issuer、audience 或结构任一失败均返回 401，且不调用上游 API。

### 5.2 BFF 到 API

- BFF 不转发 gateway token，也不转发浏览器提供的内部 token；
- BFF 使用独立密钥签发 `typ=polyuquest-internal+jwt`；
- `iss=polyuquest-bff`、`aud=polyuquest-api`；
- 内部 token TTL 为 60 秒或配置最大值中的较小者；
- 每次请求生成新 `jti`；
- 本轮仅 durable Run 路由强制身份，其他既有 BFF 路由行为保持兼容。

### 5.3 API 身份验证

- durable create/get/events/cancel 同时需要 reader workload key 和 end-user assertion；
- 签名使用 constant-time 比较；
- 固定接受 HS256，拒绝 `none`、算法替换和错误 `typ`；
- 生产 API 只能使用 `END_USER_IDENTITY_MODE=signed_jwt`；
- 开发/单元测试可使用固定 `legacy-tenant/legacy-user`，不得用于生产。

### 5.4 Run 所有权

- 新字段：`tenant_id`、`owner_subject`；
- create 将身份写入 Run；
- get/events/last-event/cancel 均在 SQL 中同时匹配 run、tenant、owner；
- 所有权不可由请求 body 覆盖；
- Worker 的内部方法保留无 token 访问，但不对 HTTP 暴露；
- owner mismatch 与未知 Run 都返回 `404 Agent Run not found`。

### 5.5 幂等语义

- 先验证浏览器原始幂等键格式；
- 存储键为 `ik1-hex(sha256(tenant_id \0 subject \0 raw_key))`；
- 数据库既有 UNIQUE 约束继续有效，无需重建表；
- 同一 owner + key + 相同请求返回原 Run；
- 同一 owner + key + 不同请求返回 409；
- 不同 owner 使用相同 raw key 相互独立；
- 存储和日志不保留原始浏览器幂等键。

## 6. 总体架构与信任边界

```text
Enterprise IdP / OIDC
        |
        v
Trusted Gateway
  sign gateway JWT (gateway secret, aud=BFF)
        |
        v
Next.js BFF
  verify gateway JWT
  reject browser-supplied internal identity
  sign internal JWT (different secret, aud=API)
  inject reader workload API key
        |
        v
FastAPI durable Run endpoints
  verify workload role
  verify internal end-user JWT
  query by run_id + tenant_id + owner_subject
        |
        +------> SQLite Agent Run Store
        |
        +------> Worker (lease-based, no identity secret)
```

密钥可见性：网关只需 gateway secret；BFF 需要 gateway 与 internal 两把密钥；API 只需 internal secret；Worker 两者都不需要。两把密钥相同会导致 BFF 启动失败。

## 7. 数据模型、迁移与接口合同

### 7.1 SQLite 迁移

启动时读取 `PRAGMA table_info(agent_runs)`：

- 缺少 `tenant_id` 时执行 `ALTER TABLE ... DEFAULT 'legacy-tenant'`；
- 缺少 `owner_subject` 时执行 `ALTER TABLE ... DEFAULT 'legacy-user'`；
- 创建 `idx_agent_runs_owner(tenant_id, owner_subject, created_at)`；
- 操作幂等，允许 API 和 Worker 先后启动；
- schema 检查、增列、建索引与 backfill 位于同一 `BEGIN IMMEDIATE`，并发首次启动串行化；
- 旧记录归属 legacy owner，不自动授权给任何新签名用户。

### 7.2 HTTP 合同

受保护路由：

- `POST /api/agent/runs`
- `GET /api/agent/runs/{run_id}`
- `GET /api/agent/runs/{run_id}/events`
- `POST /api/agent/runs/{run_id}/cancel`

身份错误为 401；workload key 错误沿用现有 401/403；资源越权为 404；幂等请求冲突为 409；容量和预算错误保持 429/422。

### 7.3 配置合同

API：`END_USER_IDENTITY_MODE`、`END_USER_IDENTITY_SECRET_FILE`、issuer、audience、最大 TTL、时钟偏差。

BFF：`BFF_IDENTITY_MODE`、gateway/internal secret file、两组 issuer/audience、最大 TTL、时钟偏差。生产密钥只通过只读 Compose secret 文件注入。

## 8. 安全、隐私与合规

1. workload auth 与 user auth 必须同时成立，任一不能替代另一；
2. 浏览器控制的 `X-API-Key`、内部 identity header 和 trace carrier 均不被信任；
3. gateway 与 internal JWT 使用互斥 `typ`、issuer、audience 和密钥，降低 token substitution；
4. HMAC 密钥至少 32 bytes，禁止人类口令；
5. 所有身份错误使用固定响应，不包含 claims、签名或解析细节；
6. 不在日志、span、事件 payload、Run snapshot 中返回身份 token；
7. 越权返回 404 防止 Run 枚举；
8. 用户 groups 当前不参与授权，避免产生未经实现的权限承诺；
9. `jti` 只用于唯一性和未来审计扩展，不宣称已实现单次防重放；
10. 密钥文件使用 root:10001 和 0440，Worker 不挂载；
11. 网关认证结果的来源和会话保护由企业 IdP/gateway 负责；
12. 下一轮知识 ACL 必须覆盖读、写、更新、删除、向量过滤和图 ID，而不只是检索过滤。

## 9. 稳定性、性能与可观测性

- JWT 校验和 HMAC 重签均为本地常数级操作，不增加外部网络调用；
- BFF 预先拒绝无效 token，避免消耗 API/Worker 容量；
- SQLite owner 索引支持按所有者定位；
- SSE 每轮重新执行 owner-scoped get，所有权不可变且不会在长连接中降级；
- Worker 保持原 claim/lease 批处理路径，无每次执行 token 校验；
- 身份解析失败不写 token，只记录固定错误类别和既有 request ID；
- 后续指标可统计 `identity_rejected` 和 `owner_not_found`，但不得使用 subject 作为高基数标签。

## 10. 测试与验收标准

1. Python/TypeScript 均能验证合法 token；
2. 伪造签名、`alg=none`、错误 `typ`、issuer、audience、过期、未来、超 TTL 被拒绝；
3. BFF 不转发 gateway header，不接受浏览器内部 header；
4. 缺失 gateway identity 在调用上游前返回 401；
5. BFF 重签 token 能被模拟 API 使用 internal secret 验证；
6. 同一 owner 幂等重试稳定，同 key 不同请求冲突；
7. 不同 owner 相同 raw key 生成不同 Run；
8. get/events/cancel 跨 owner 均返回统一 404；
9. owner 取消失败不改变原 Run；
10. 旧 SQLite schema 可无损升级并归入 legacy owner；
11. 生产配置禁用 identity 或缺少密钥时启动失败；
12. API/BFF/Worker 的 secret mount 满足最小权限；
13. 前端测试、TypeScript、Python、Ruff、deployment policy、Compose、真实 BFF 与生产拓扑门禁通过；
14. 本轮不得回归 Agent Run lease、admission、SSE replay 和 trace propagation。

### 10.1 本地验收证据

| 验收项 | 结果 |
|---|---|
| Python 全量回归 | 281 passed，65 subtests passed |
| 并发旧 schema migration | 连续 10 轮双实例初始化通过 |
| Frontend Vitest | 4 files，23 tests passed |
| TypeScript | `tsc --noEmit --incremental false` passed |
| 定向 Ruff | passed |
| Deployment / supply-chain policy | passed |
| YAML / Node E2E 脚本语法 | passed |
| Production / BFF / Topology Compose 展开 | passed（受控占位 secret） |

clean Linux image build、真实 BFF 身份重签和 API/Worker 同启由本次提交触发的 GitHub Browser BFF 与 Production Topology 门禁补充最终证据。

## 11. 配置、部署与运维

发布前由平台团队配置企业 OIDC gateway，使其只在成功认证后生成 gateway assertion。生成两把不同的高熵密钥并以文件方式部署；先升级 BFF/API 代码和数据库 schema，再启用 gateway header。Runbook 规定 canary、验签、权限、轮换和故障诊断步骤。

开发模式默认 `disabled`，使用 legacy identity 只为保持本地无 IdP 场景可运行。任何生产 Compose 都固定为 `signed_jwt`，不能通过浏览器参数降级。operator stats/health 继续由 workload RBAC 保护，不开放 owner 枚举接口。

## 12. 发布、回滚与风险控制

### 12.1 发布

1. 备份 Agent Run SQLite；
2. 部署兼容新旧 schema 的 API/Worker，确认列和索引创建；
3. 部署 internal secret 到 BFF/API，gateway secret 只到 gateway/BFF；
4. 配置 gateway assertion，使用 canary 用户验证 create/replay/cancel；
5. 验证另一用户对 canary run 得到 404；
6. 观察 401、404、BFF 502 与队列指标后全量切换。

### 12.2 回滚

- 数据库新增列和索引保留，不做破坏性降级；
- 应用可回滚到读额外列无影响的前一版本，但会暂时失去 owner 隔离，因此仅作为受控应急；
- 若 gateway 故障，生产环境不得切到 disabled，应恢复上一网关实例或密钥；
- 密钥泄露时轮换对应边界，不删除 Run 数据；
- 回滚不应把 legacy owner 暴露给新用户。

### 12.3 已知风险

- MVP 使用对称 HS256，需要严格控制共享密钥范围；
- 无分布式 `jti` cache，窃取 token 在短 TTL 内可重放；
- gateway 与 BFF 时钟偏差可能造成误拒绝；
- 当前 SQLite 单文件适合单节点/共享卷基线，不是多区域数据库；
- Run owner 隔离不意味着检索知识已租户隔离；
- groups 尚未具备生命周期、嵌套组和动态撤权语义。

## 13. 未来优化与改进方向

1. 下一迭代实现知识对象多租户迁移：Neo4j/Qdrant/BM25 使用 tenant-scoped composite identity；
2. 将 gateway HS256 升级为企业 IdP/JWKS 的 RS256/ES256 验签，BFF 不再共享网关私密签名材料；
3. internal assertion 使用非对称签名或 mTLS + SPIFFE workload identity；
4. 使用 Redis/数据库实现高风险操作的 `jti` 单次消费和撤销；
5. 建立密钥版本 `kid`、双 key 验证窗口和无中断轮换；
6. 增加 owner-scoped Run 列表、分页和保留策略；
7. 将 tenant/owner 的不可逆低基数审计标识写入安全审计，但不进入业务 trace；
8. 为 401/404 异常率、时钟偏差和密钥读取失败建立告警；
9. 将 SQLite Run Store 迁移到 PostgreSQL，支持行级安全、HA 和在线 schema migration；
10. 对 groups/roles 引入策略引擎，并建立 deny-by-default、策略版本和决策审计；
11. 为同步问答建立会话所有权与历史隔离；
12. 完成知识 ACL 后，再将发现页面、图补丁、索引任务与证据引用统一绑定 tenant provenance。
