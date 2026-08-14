# 业务评测治理与自动发布门禁 PRD

## 1. 迭代目标

在端到端 Telemetry 与确定性评测报告已经可用的基础上，建立可审计、可复现、能够实际阻止有害版本发布的 Evaluation Governance 与 Release Gate。

本轮不以“再增加一个平均分”为目标，而是回答四个发布决策问题：

1. 当前 baseline 与 candidate 是否使用同一个冻结数据集和同一评分合同；
2. 数据集是否包含足够的语义 Gold，还是只有流程 Smoke Case；
3. 总体指标没有下降时，是否仍有关键任务或业务切片发生回退；
4. 质量收益是否以不可接受的延迟、页面访问量或 LLM 成本为代价。

最终输出机器可执行的 `pass / fail / insufficient_evidence` 结论、逐项 Check、JSON/Markdown 报告和稳定退出码，并接入 GitHub Actions。

## 2. 业务背景与适用边界

企业内网、大学/研究机构官网、政府门户和产品文档站的问答质量具有明显的长尾分布。总体平均分可能稳定，但“最新政策”“申请截止日期”“权限受限页面”“无证据应拒答”等关键场景一旦回退，业务风险远高于普通问答。

因此发布门禁需要同时覆盖：

- Semantic Gold：答案必须包含的事实、可信来源和禁止声明；
- Behavioral Contract：是否需要在线探索、是否允许持久化、是否应该拒答；
- Operational Budget：延迟、抓取页数、失败次数与实际计费 Token；
- Critical Slice：关键任务、关键标签和 blocker case 不得被总体均值掩盖。

本轮不把仓库中的示例数据冒充正式业务 Gold。正式上线前仍需要部署方基于冻结网页快照完成标注与审批。

## 3. 修改前现状与主要问题

### 3.1 Compare 只有 Delta，没有发布结论

现有 `compare_reports()` 能校验 dataset/evaluator fingerprint 并计算总体差值，但没有阈值、方向、缺失指标策略或退出码。CI 即使看到质量下降也会继续成功。

### 3.2 数据集缺少 Manifest 与审批状态

JSONL 只有 Case 内容，没有 dataset ID、版本、Owner、状态、快照时间、文件 Hash、最低样本数、Gold 覆盖率和审批记录。无法证明两次实验所说的“同一数据集”在业务语义上可用。

### 3.3 Smoke Case 可以伪装为质量证据

只有探索、持久化和延迟预期的 Case 可以验证程序流程，却不能证明答案正确。若此类数据进入 quality gate，会产生虚假的高分和发布信心。

### 3.4 平均指标掩盖业务切片回退

EvaluationReport 没有保留 tags、task_type、oracle_type 和 criticality，无法按 freshness、multi-hop、abstention 等切片比较，也无法实施 blocker case 零回退。

### 3.5 成本与质量没有联合门禁

现有报告虽包含平均延迟和 Token，但没有 candidate/baseline 比率、零分母规则、最大增幅和“指标缺失是否失败”的明确策略。

### 3.6 没有 CI 执行入口和证据产物

仓库没有 `.github/workflows`。评测命令依赖人工执行，PR 无法稳定产出 Gate Decision，也没有机器 JSON 与人读 Markdown Artifact。

## 4. 数据集治理模型

### 4.1 EvaluationDatasetManifest

Manifest 使用 YAML，包含：

- `dataset_id`、`version`、`schema_version`；
- `description`、`owner`、`domain_scope`；
- `status: draft | reviewed | approved | retired`；
- `case_file`、`case_file_sha256`；
- `created_at`、可选 `source_snapshot_at`；
- `reviewer`、`reviewed_at`；
- `minimum_cases`、`minimum_semantic_gold_ratio`；
- `required_tags`。

Manifest 路径为信任根，`case_file` 必须解析在 Manifest 所在目录内，禁止 `..` 路径逃逸。

### 4.2 Case Oracle 类型

每个 Case 增加：

- `oracle_type: semantic_gold | behavioral_contract | smoke`；
- `criticality: blocker | critical | standard`；
- 可选 `annotation_owner`、`reviewed_at`。

约束：

- semantic_gold 至少包含 required facts/source/forbidden claims 之一；
- behavioral_contract 至少包含 status、exploration、persistence 或 budget 之一；
- blocker 必须是 semantic_gold，且必须有 expected_status；
- smoke 不参与 quality score 与业务质量门禁；
- approved 数据集必须满足最低样本数、Gold 比例、required tags 和审批字段。

### 4.3 验证结果

`DatasetValidationResult` 输出：

- valid、errors、warnings；
- dataset/case fingerprint 与实际 SHA-256；
- case 数、semantic/behavioral/smoke 数；
- Gold ratio、blocker/critical 数；
- tag/task_type 分布。

Draft 数据不足产生 warning；approved 数据不足产生 error。

## 5. 评分报告与业务切片

### 5.1 CaseScore 元数据

CaseScore 保留 task_type、tags、oracle_type、criticality，使报告本身可以独立重放切片门禁，不再依赖外部 JSONL。

### 5.2 SliceSummary

至少生成：

- `tag:<tag>`；
- `task:<task_type>`；
- `oracle:<oracle_type>`；
- `criticality:<criticality>`。

每个切片记录 case_count、missing_responses、semantic_gold_cases、quality_score、operational_score、overall 和平均延迟/Token。

### 5.3 指标适用性

- smoke：只进入运行/CLI 健康检查，不进入业务质量分；
- behavioral_contract：`expected_status`、exploration、persistence 和预算约束进入 operational score；
- semantic_gold：进入 quality score，可同时进入 operational score；
- 无适用值继续输出 N/A，禁止自动填 0；
- overall 只有存在 semantic gold 时生成。

## 6. Release Gate Policy

Policy 使用 YAML 并版本化，包含：

- evidence：允许的数据集状态、最小总样本、最小 semantic gold 数/比例；
- candidate floors：质量、行为、缺失响应、禁止声明等绝对下限；
- regression limits：candidate-baseline 最大允许下降；
- cost limits：延迟、Token、页面数的最大绝对值或增长比例；
- critical rules：blocker/critical case 最大回退数；
- slice rules：切片最小样本数、质量/行为最大下降；
- `missing_metric_policy: fail | insufficient_evidence | ignore`。

阈值方向必须显式建模：quality 越高越好；forbidden/missing/latency/token 越低越好，不能通过同一个符号隐式猜测。

## 7. Gate 判定规则

### 7.1 结论

- `pass`：证据充分且所有 error check 通过；
- `fail`：至少一项已能确定的硬发布约束失败；即使同时存在证据不足，已知回退也不能被降级成 `insufficient_evidence`；
- `insufficient_evidence`：数据集/指标/样本不足，不能证明安全发布；

默认采取 fail-closed：CLI 中 `fail=1`、`insufficient_evidence=2`、配置/输入错误=3。

### 7.2 绝对值与回退

同时检查 candidate floor 与 baseline delta，避免“双方都很差但没有回退”或“candidate 尚可但相对大幅下降”被放行。

### 7.3 成本比率

- baseline > 0：使用 candidate / baseline；
- baseline = candidate = 0：比率视为 1；
- baseline = 0 且 candidate > 0：视为无限增长并失败；
- 缺失 cost metric 按 missing policy 处理。

### 7.4 Case 与切片回退

- blocker：quality/overall 从通过变为失败或显著下降即计回退；
- critical：允许 Policy 配置有限回退数；
- slice：只对双方都达到 minimum slice cases 的切片判定；
- 新增/缺失 Case 不静默跳过，报告为 evidence issue。

## 8. CLI 与 CI 工作流

新增命令：

```text
python -m agent_rag.evaluation.cli validate --manifest ... --output ...
python -m agent_rag.evaluation.cli gate --baseline ... --candidate ... --policy ... --output ...
```

GitHub Actions：

1. 只读 checkout；
2. 使用锁定版本的 Python 与最小评测依赖，避免 CI 依赖漂移；
3. 运行 dataset validation 和 evaluation/gate 单测；
4. 对 deterministic fixture 执行 score + gate；
5. 上传/保留 JSON 与 Markdown Gate 结果；
6. Gate 非 pass 时 Job 失败。

真实业务 baseline/candidate 可以在 workflow_dispatch 或后续 Artifact Pipeline 中替换 fixture，不在 CI 内调用外部 LLM 产生不可重复结果。

## 9. 安全与可信边界

1. Manifest case path 禁止逃逸仓库/数据集目录；
2. Dataset Validator 检测疑似 API Key/Bearer Token；
3. CI 不读取生产密钥、不调用外部 LLM、不访问真实内网页面；
4. baseline 与 candidate 报告均保留 dataset/evaluator/system config/code fingerprints；
5. Policy、去除 wall-clock 字段后的 baseline/candidate report 与 Gate Decision 都保存 SHA-256；
6. 修改 Gold、Policy 或 baseline 需要 Code Review；
7. 示例/fixture 明确标注非生产业务结论。

## 10. 可观测性与审计

GateDecision 包含：

- gate_id、status、created_at；
- baseline/candidate variant、report fingerprints 与 summary；
- policy ID/version/hash；
- dataset ID/version/status；
- checks：category、metric、scope、outcome、actual、expected、message；
- failed/insufficient/warning 数；
- candidate/baseline 摘要、summary deltas 和逐 slice 回退 Check。

Markdown 报告优先展示失败项、关键回退和证据不足，完整 JSON 用于 CI/后续 Dashboard。

## 11. 验收标准

1. Manifest 能检测 Hash 不一致、路径逃逸、重复 ID 和疑似 secret；
2. approved 数据集缺少审批、样本、Gold 比例或 required tag 时验证失败；
3. semantic/behavioral/smoke 的评分适用性正确；
4. EvaluationReport 生成 tag/task/oracle/criticality 切片；
5. baseline/candidate snapshot 或 evaluator 不兼容时 Gate 拒绝；
6. 质量 floor、最大回退、成本比率和缺失指标策略均有测试；
7. blocker case 回退能使 Gate fail；
8. 样本或 Gold 不足返回 insufficient_evidence，而非 pass；
9. Gate CLI 退出码 0/1/2/3 稳定；
10. JSON/Markdown Decision 可复现且包含 policy、dataset、baseline report、candidate report 与 decision fingerprint；仅 `created_at` 变化不得改变 decision ID；
11. GitHub Actions 不需要 Neo4j、Qdrant、LLM Key 或网络业务依赖即可执行 deterministic gate；
12. 全量 Python、Ruff、TypeScript 与本地 CI 命令通过。

## 12. 非目标

- 本轮不宣称示例 fixture 是正式业务 Gold；
- 不在 PR CI 中在线调用 LLM 或抓取真实机构站点；
- 不实现人工标注 Web UI；
- 不用 LLM-as-judge 替代确定性 Gold；
- 不自动更新 baseline；
- 不自动部署或回滚生产服务；
- 不把单一 overall 作为唯一发布依据。

## 13. 后续方向

1. 建立部署方真实 50～100 题 Gold Set 与双人审批；
2. 保存冻结网页证据快照与 annotation provenance；
3. CI Artifact/Registry 中管理不可变 baseline；
4. 接入 shadow/canary 流量与线上自动回滚；
5. 引入统计置信区间、bootstrap 和显著性判定；
6. 建立质量—延迟—成本 Pareto 调参任务；
7. 前端管理端展示 slice drift、case diff 和审批记录；
8. 可选校准 LLM Judge，但必须和 deterministic gate 分开。
