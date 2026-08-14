# PolyUQuest 生产部署基线 PRD

> 迭代 13 · 2026-08-14 · 状态：已实现；真实 Docker 全栈/恢复演练待 daemon 环境复验

## 1. 背景与目标

PolyUQuest 已完成查询驱动的 Agent 检索、在线网页探索、增量入图、异步索引、知识新鲜度、可观测性、评测发布门禁与 API 安全边界。当前系统已经具备业务能力，但启动方式仍以开发机为中心：仅 Neo4j 与 Qdrant 有 Compose 定义，API、Worker 和前端缺少可复现镜像，后台任务与 API 进程耦合，也没有成体系的备份恢复流程。

本迭代不改变检索算法，而是建立第一个可交付的生产部署基线，使一套经过验收的版本能够被重复部署、独立扩缩、受控停机、备份恢复和快速回滚。

## 2. 业务场景与成功标准

目标场景是企业内网、机构官网或部门知识门户。它们通常由可信域名提供内容，但页面持续变化，系统需要在在线回答、后台增量更新与长期知识积累之间保持稳定。

成功标准：

1. 一条生产 Compose 命令可以声明 API、Worker、前端、Neo4j、Qdrant 及其持久化卷。
2. API 与 Worker 使用同一应用镜像、不同进程入口，互不抢占职责。
3. 数据库不直接暴露到公网；应用进程以非 root、只读根文件系统运行。
4. 缺少认证、数据库密码或模型密钥时，生产配置在启动阶段明确失败。
5. 收到终止信号后，Worker 先停止领取新任务，并在限定时间内完成当前任务。
6. 运维人员可执行带校验和的冷备份、受确认保护的恢复与恢复后验证。
7. 无 Docker daemon 时也能静态验证部署清单；有 daemon 时可按 Runbook 完成真实演练。

## 3. 修改前现状与主要问题

### 3.1 部署拓扑不完整

原 `docker-compose.yml` 只声明 Neo4j 和 Qdrant，API 与 Next.js 仍依赖开发机上的 Python/Node 环境。部署结果受到本机依赖、命令和目录状态影响，无法形成可审计的制品。

### 3.2 基础设施默认值不适合生产

- Neo4j 密码硬编码在 Compose 中。
- Qdrant 使用可漂移的 `latest` 标签。
- Neo4j Browser、Bolt、Qdrant REST/gRPC 均直接发布到宿主机所有网卡。
- Compose 顶层仍包含已废弃的 `version` 字段。
- 缺少非 root、只读文件系统、能力裁剪和进程回收等容器约束。

### 3.3 API 与后台任务生命周期耦合

API 进程同时执行索引 Worker、新鲜度 Worker、补丁恢复和新鲜度目标引导。多副本 API 会重复启动后台任务，API 扩容与任务吞吐无法独立控制。

当前 Worker 关闭时直接取消 asyncio task；索引过程使用 `asyncio.to_thread`，取消协程并不能强制终止已经运行的线程，可能在进程退出期间仍写 Neo4j/Qdrant。

### 3.4 生产错误配置不能充分提前暴露

已有配置会阻止生产环境关闭 API 认证，但仍允许默认 Neo4j 密码、开发热重载、缺失当前模型供应商密钥等配置进入运行期。错误通常要等第一次查询或数据库连接时才出现。

### 3.5 数据恢复缺少可执行路径

Neo4j、Qdrant、运行时 SQLite/outbox 与 BM25 缓存分散在不同位置，尚未定义一致性边界、备份清单、恢复确认、校验和、恢复后健康检查与演练频率。

## 4. 目标架构

```text
                   External TLS / SSO / API Gateway
                              |
                    loopback published ports
                              |
              +---------------+---------------+
              |                               |
        Next.js frontend                  FastAPI API
                                              |
                         private backend network
                +-------------+---------------+-------------+
                |             |                             |
          Worker process    Neo4j                         Qdrant
        index + freshness   graph                           ANN
                |
       shared runtime/outbox volume
```

职责边界：

- API：只负责请求、鉴权、查询、管理接口与 readiness，不在生产拓扑中运行后台 Worker。
- Worker：执行补丁恢复、新鲜度引导、索引 outbox 和定期刷新；当前仅部署一个副本，后续再做多副本租约压测。
- 前端：提供静态/SSR UI，通过外部网关的同源 `/api` 访问 API，不把长期 API key 编译进浏览器包。
- Neo4j/Qdrant：只加入私有后端网络，不发布宿主机端口。
- 外部网关：负责 TLS、SSO/身份转换、限流，并向 API 注入短期或受管凭证；不在本仓库基线内实现。

## 5. 制品与镜像设计

### 5.1 后端镜像

- 多阶段构建，使用 `uv.lock` 和 `uv sync --locked` 保证依赖可复现。
- API 与 Worker 复用同一镜像，通过不同 command 启动。
- 运行阶段使用非 root UID 10001。
- 只复制运行所需源码、配置、已发布数据与虚拟环境；排除 `.env`、Git、缓存、测试产物和本地运行数据。
- 内置 API liveness 健康检查，但 readiness 由编排与运维验证单独判断。

### 5.2 前端镜像

- 使用 Next.js `standalone` 输出进行多阶段构建。
- 使用系统字体栈，避免 `next/font/google` 在受限网络中产生不可复现的构建期下载。
- 构建期将 API 地址设置为同源 `/api`。
- 运行阶段使用非 root 用户，并只复制 standalone server、静态资源和 public 资源。

### 5.3 版本策略

- 运行时、Neo4j 与 Qdrant 使用显式版本标签，禁止 `latest`。Neo4j 首次基线固定
  在既有 5.x store 兼容线 `5.26.28`，不在部署迭代中隐式跨大版本升级。
- 依赖升级通过单独 PR 完成，先跑自动化测试、评测门禁和备份恢复演练，再更新生产标签。
- 镜像 digest 固定与 SBOM/签名属于下一阶段供应链加固范围。

## 6. 配置、密钥与启动前校验

生产环境必须显式提供：

- `NEO4J_PASSWORD`，且不得等于仓库开发默认值；
- `API_AUTH_MODE=api_key` 与至少一个 admin 哈希凭证；
- 当前 LLM provider 所需 API key；
- 当前远程 embedding provider 所需 API key；
- 明确的 CORS origin、运行数据路径与外部访问端口。

生产 Compose 使用 `${NAME:?message}` 声明必填变量。应用配置同时进行二次 fail-fast 校验，防止绕过 Compose 直接启动时漏检。仓库只提交 `.env.production.example`，不提交真实密钥。

`APP_PROCESS_ROLE=api|worker` 绑定进程入口：只有 API 容器接收 `API_AUTH_KEYS`；
Worker 不提供 HTTP 服务，也不获得该 Secret。API/Worker 在生产启动时分别校验角色，
避免通过把 API 标记为 worker 绕过认证。

## 7. 网络与容器安全基线

- Neo4j 与 Qdrant 仅存在于 `backend` 私有网络，不声明 `ports`。
- API/Worker 同时连接 `backend` 与可出站的 `egress` 网络，以支持模型 API 和
  在线网页探索；数据库不加入 `egress`。下一阶段由代理/防火墙收紧出站白名单。
- API/前端默认仅绑定宿主机 `127.0.0.1`，由同机受管网关转发。
- API、Worker、前端启用 `read_only`、`tmpfs /tmp`、`cap_drop: ALL`、`no-new-privileges` 与 `init`。
- 运行数据、模型缓存和 BM25 缓存使用最小化命名卷挂载；API 不挂载不需要的源码目录。
- 设置重启策略、停止宽限期、PID 与资源上限，避免单个进程失控影响整机。
- 健康接口保持匿名，但不返回凭证、请求正文或内部堆栈。

## 8. Worker 拆分与优雅停机

新增 `agent-rag-worker` 入口，单独运行索引和新鲜度两个循环，并复用原有 outbox、租约、重试和幂等写入语义。

终止流程：

1. 捕获 SIGTERM/SIGINT，设置进程级停止事件。
2. Worker 停止领取新任务。
3. 在 `WORKER_SHUTDOWN_GRACE_SECONDS` 内等待当前任务自然结束。
4. 超时后取消外层 task，并交由 Compose 的更长 `stop_grace_period` 兜底。
5. 因线程无法被 Python 安全强杀，所有写操作仍依赖既有幂等 patch/outbox 与启动恢复保证最终一致。

API 开发模式仍可选择进程内 Worker；生产 Compose 明确关闭该功能，避免 API 副本重复消费。

## 9. 持久化、备份与恢复

一致性备份采用第一阶段更可靠的冷备份：先停止写入服务与数据库，再打包命名卷。备份集合至少包含：

- Neo4j 数据；
- Qdrant storage；
- Agent ledger、index outbox、新鲜度状态与安全审计 SQLite；
- BM25/cache 等可重建数据（记录但不作为恢复成功的唯一条件）；
- 应用版本、Compose 文件摘要、时间戳与 SHA-256 清单。

恢复必须指定备份 ID 并输入显式确认短语，流程先校验清单，再替换目标卷，最后启动依赖、Worker、API 与前端并运行健康/数据抽样验证。恢复操作不得在服务仍运行时执行。

RPO/RTO 初始目标：每日冷备份，RPO 24 小时；单机基线 RTO 60 分钟。若业务不能接受停机，后续升级为 Neo4j 官方 dump/backup、Qdrant snapshot 与双写冻结协议。

## 10. 健康检查、可观测性与运维

- `/api/health/live`：进程存活，不依赖外部服务。
- `/api/health/ready`：嵌入模型和启动阶段完成，可接收查询。
- `/api/health/dependencies`：受保护地检查 Neo4j/Qdrant 等依赖。
- Compose 用 liveness 控制重启；上线与恢复验收同时检查 readiness/dependencies。
- Runbook 记录部署、升级、回滚、日志定位、备份、恢复与故障升级步骤。
- 本迭代保留结构化标准输出日志；集中日志、指标告警与分布式 trace 后端列入后续迭代。

## 11. 验收与测试方案

自动化验收：

1. Python 单元测试覆盖生产 fail-fast、Worker 正常停机与超时取消。
2. 部署静态验收器检查镜像标签、数据库端口、只读/非 root 约束、必填变量与卷。
3. `docker compose ... config` 能在示例环境下成功展开且不含真实密钥。
4. Ruff、后端全量 pytest、前端测试、TypeScript 与 Next.js production build 通过。
5. Docker daemon 可用时构建两个镜像，启动全栈并执行 live/ready/dependencies、写入、重启、备份与恢复冒烟。

当前开发机若 Docker daemon 未启动，第 5 项记录为“环境未执行”，不得把静态验证描述为实际容器通过。

前端不得以“没有测试文件”的 Vitest 退出结果冒充通过；至少保留布局/构建契约的
确定性测试，并在后续 UI 迭代继续扩展关键交互覆盖。

## 12. 发布、回滚与非目标

发布：先生成备份，拉取/构建新制品，运行配置与评测门禁，再滚动启动数据库、Worker、API、前端。单机 Compose 基线会有短暂停机。

回滚：保留上一版显式镜像标签与最近一次备份；应用失败优先回退镜像，发生 schema/数据不兼容时才执行恢复。

本迭代非目标：

- Kubernetes/多可用区/自动扩缩容；
- 零停机数据库迁移；
- 自建 TLS、SSO 或完整 API Gateway；
- 在线热备与跨地域容灾；
- 镜像签名、SBOM、漏洞门禁；
- 多 Worker 并发吞吐保证。

## 13. 后续优化方向

1. **真实交付门禁**：在 CI 使用 BuildKit 构建、扫描并签名镜像，按 digest 部署。
2. **托管入口层**：接入企业 SSO、短期令牌、WAF、租户级限流与审计主体映射。
3. **在线备份**：Neo4j dump/backup 与 Qdrant snapshot 编排，缩短停机窗口并自动做恢复演练。
4. **Worker 横向扩展**：租约 fencing token、心跳续租、并发上限、毒任务隔离和 DLQ 重放。
5. **Schema migration**：为 Neo4j、Qdrant payload 与 SQLite 引入版本化迁移及向后兼容窗口。
6. **可观测性平台**：OpenTelemetry trace、Prometheus 指标、SLO 告警与每查询成本/新鲜度看板。
7. **多环境发布**：dev/staging/prod 配置分层、金丝雀流量、评测门禁自动晋级与一键回滚。
8. **高可用架构**：根据业务 RTO/RPO 选择托管 Neo4j/Qdrant、跨区副本与对象存储备份。
