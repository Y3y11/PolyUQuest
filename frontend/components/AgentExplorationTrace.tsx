"use client";

import { useState } from "react";
import {
  BrainCircuit,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDashed,
  Database,
  FileSearch,
  GitBranch,
  Globe2,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import type { AgentActivity, AgentAction } from "@/lib/api";

const ACTION_LABELS: Record<string, string> = {
  "query.profile": "解析查询约束",
  "polyuquest.search": "检索现有知识库",
  "polyuquest.expand": "扩展图搜索前沿",
  "frontier.select": "选择下一探索页面",
  "web.fetch_trusted_page": "访问可信机构页面",
  "polyuquest.publish_patch": "发布增量知识补丁",
  "polyuquest.queue_index_patch": "提交异步知识更新",
  "answer.compose": "基于证据生成回答",
};

const STOP_LABELS: Record<string, string> = {
  evidence_sufficient: "证据已足够，停止探索",
  frontier_exhausted: "候选页面已耗尽",
  time_budget_exhausted: "达到时间预算",
  iteration_budget_exhausted: "达到探索轮次预算",
  page_budget_exhausted: "达到页面访问预算",
  exploration_disabled: "网页探索已关闭",
  initial_search_failed: "初始检索失败",
};

const REASON_LABELS: Record<string, string> = {
  "No evidence block was retrieved.": "现有知识库没有检索到证据块",
  "Retrieved blocks do not pass relevance checks.": "检索结果未通过相关性检查",
  "Retrieved blocks match navigation context but not the requested details.": "页面导航匹配问题，但正文尚未覆盖所需细节",
  "Retrieved blocks do not preserve the requested target entity.": "证据没有覆盖问题限定的目标实体",
  "Retrieved blocks conflict with an explicit query qualifier.": "证据与问题中的限定条件不一致",
  "Relevant evidence confidence is below the answer threshold.": "相关证据置信度尚未达到回答门槛",
  "At least one evidence block passes relevance checks.": "已有证据通过相关性检查",
  "Required answer claims remain unsupported.": "仍缺少回答所必需的证据声明",
  "The question is freshness-sensitive but indexed evidence is stale.": "问题需要最新信息，但索引证据可能已过期",
  "The selected frontier page could not be fetched.": "当前候选页面抓取失败，将尝试其他路径",
};

function actionIcon(action: string) {
  if (action === "query.profile") return BrainCircuit;
  if (action === "polyuquest.search") return Database;
  if (action === "polyuquest.expand") return GitBranch;
  if (action === "frontier.select") return BrainCircuit;
  if (action === "web.fetch_trusted_page") return Globe2;
  if (action === "polyuquest.publish_patch") return ShieldCheck;
  if (action === "polyuquest.queue_index_patch") return ShieldCheck;
  return FileSearch;
}

function actionSummary(action: AgentAction): string {
  const details = action.details || {};
  if (action.action === "query.profile") {
    if (action.status === "started") return "抽取目标实体、任务意图与回答所需证据";
    if (action.status === "succeeded") {
      const constraints = Array.isArray(details.constraints) ? details.constraints : [];
      const labels = constraints
        .map((item) => typeof item === "object" && item && "label" in item ? String(item.label) : "")
        .filter(Boolean);
      return labels.length > 0
        ? `已识别约束：${labels.join("、")}`
        : "未发现需要额外限定的目标实体";
    }
  }
  if (action.action === "polyuquest.search") {
    if (action.status === "started") return "正在判断检索模式并查询块、页面和图关系";
    if (action.status === "failed" && details.fallback_to_trusted_web === true) {
      return "现有知识库暂不可用，降级为从受信任机构入口继续探索";
    }
    if (action.status === "succeeded") {
      const source = details.route_source === "heuristic"
        ? "规则路由"
        : details.route_source === "cache"
          ? "缓存路由"
          : details.route_source === "llm"
            ? "LLM 路由"
            : "默认路由";
      const mode = typeof details.route_mode === "string" ? ` ${details.route_mode}` : "";
      const stages = typeof details.stage_durations === "object" && details.stage_durations
        ? details.stage_durations as Record<string, unknown>
        : {};
      const slowest = Object.entries(stages)
        .filter(([, value]) => typeof value === "number")
        .sort((a, b) => Number(b[1]) - Number(a[1]))[0];
      const bottleneck = slowest && Number(slowest[1]) >= 1000
        ? `；最慢阶段 ${slowest[0]} ${(Number(slowest[1]) / 1000).toFixed(2)}s`
        : "";
      return `${source}${mode}；找到 ${details.evidence ?? 0} 个证据块，获得 ${details.frontier ?? 0} 个候选入口${bottleneck}`;
    }
  }
  if (action.action === "polyuquest.expand") {
    if (action.status === "started") {
      const claims = Array.isArray(details.missing_claims)
        ? details.missing_claims.map(String).filter(Boolean)
        : [];
      return claims.length > 0
        ? `第 ${details.iteration ?? "?"} 轮：优先寻找“${claims.join("、")}”的证据`
        : `第 ${details.iteration ?? "?"} 轮：沿页面链接与实体关系寻找候选页`;
    }
    if (action.status === "succeeded") return `发现 ${details.candidates ?? 0} 个可信候选页面`;
  }
  if (action.action === "web.fetch_trusted_page") {
    const url = typeof details.url === "string" ? details.url : "";
    if (action.status === "started") {
      const edge = typeof details.edge_type === "string" ? details.edge_type : "";
      const score = typeof details.candidate_score === "number"
        ? `，候选分数 ${details.candidate_score.toFixed(2)}`
        : "";
      return url
        ? `选择 ${edge || "可信入口"} 候选页 ${url}${score}`
        : "准备抓取候选页面";
    }
    if (action.status === "succeeded") {
      return `${url || "页面抓取完成"} · 新增 ${details.relevant_blocks ?? 0} 个相关证据块`;
    }
  }
  if (action.action === "frontier.select") {
    const source = details.source === "llm" ? "V4-Flash 仲裁" : "规则选择";
    const reason = typeof details.reason === "string" ? details.reason : "";
    return `${source}${reason ? ` · ${reason}` : ""}`;
  }
  if (action.action === "polyuquest.publish_patch") {
    if (action.status === "started") return "正在增量写入页面、证据块和页面链接";
    if (action.status === "succeeded") {
      const labels: Record<string, string> = {
        create: "新建知识快照",
        update: "更新知识快照",
        unchanged: "内容未变化，幂等跳过",
        repair: "修复图与向量索引",
      };
      const operation = typeof details.operation === "string" ? details.operation : "";
      const counts = operation === "unchanged"
        ? ""
        : ` · 写入 ${details.blocks_written ?? 0} 块/${details.links_written ?? 0} 链接`;
      const deleted = Number(details.blocks_deleted || 0) + Number(details.links_deleted || 0);
      return `${labels[operation] || "增量知识已发布"}${counts}${deleted > 0 ? ` · 清理 ${deleted} 个旧项` : ""}`;
    }
  }
  if (action.action === "polyuquest.queue_index_patch") {
    if (action.status === "started") return "正在提交异步知识更新任务";
    if (action.status === "succeeded") {
      return details.deduplicated === true
        ? `相同页面快照已在队列中 · ${details.job_id ?? ""}`
        : `知识更新已排队 · ${details.job_id ?? ""} · 本次回答无需等待入图`;
    }
  }
  if (action.action === "answer.compose") {
    if (action.status === "started") return `使用 ${details.evidence ?? 0} 个证据块组织回答`;
    if (action.status === "succeeded") return "回答已完成，并保留来源引用";
  }
  if (action.status === "failed") {
    return "该步骤失败，Agent 将按预算尝试其他候选路径";
  }
  return action.status === "started" ? "执行中" : "已完成";
}

function assessmentSummary(activity: Extract<AgentActivity, { kind: "assessment" }>) {
  const { decision, confidence, reasons, missing_claims } = activity.assessment;
  const lead: Record<string, string> = {
    expand: "证据不足，继续探索",
    refresh: "已有证据可能过期，查找新页面",
    answer: "证据达到回答条件",
    abstain: "证据仍不足，准备明确拒答",
  };
  const rawReason = reasons?.[0] || "";
  const reason = REASON_LABELS[rawReason] || rawReason;
  const confidenceText = confidence > 0 ? ` · 置信度 ${Math.round(confidence * 100)}%` : "";
  const missing = missing_claims?.length > 0
    ? ` · 缺失：${missing_claims.join("、")}`
    : "";
  return `${lead[decision] || decision}${confidenceText}${reason ? ` · ${reason}` : ""}${missing}`;
}

export default function AgentExplorationTrace({
  activities,
  isActive,
}: {
  activities: AgentActivity[];
  isActive: boolean;
}) {
  const [open, setOpen] = useState(true);
  const visible = activities.filter(
    (item) => item.kind !== "action" || item.action.status !== "skipped"
  );

  if (visible.length === 0 && !isActive) return null;

  return (
    <section className="mb-3 overflow-hidden rounded-xl border border-border bg-surface-alt/70">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between gap-3 px-3.5 py-3 text-left"
        aria-expanded={open}
      >
        <span className="flex min-w-0 items-center gap-2">
          <BrainCircuit size={15} className="shrink-0 text-primary" />
          <span className="text-xs font-medium text-text-main">
            {isActive ? "Agent 正在自主探索" : "Agent 探索记录"}
          </span>
          <span className="rounded bg-surface-sunk px-1.5 py-0.5 font-mono text-[9px] text-text-muted">
            {visible.length} steps
          </span>
        </span>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>

      {open && (
        <div className="border-t border-border px-3.5 py-3">
          <p className="mb-3 text-[11px] leading-relaxed text-text-muted">
            以下是可审计的决策摘要与工具结果，不包含模型隐藏思维链。
          </p>
          <ol className="space-y-3">
            {visible.map((item, index) => {
              if (item.kind === "assessment") {
                return (
                  <li key={`assessment-${index}`} className="flex gap-2.5">
                    <BrainCircuit size={14} className="mt-0.5 shrink-0 text-amber-500" />
                    <div>
                      <div className="text-[11px] font-mono uppercase tracking-[0.1em] text-text-muted">证据判断</div>
                      <div className="mt-0.5 text-xs leading-relaxed text-text-main">{assessmentSummary(item)}</div>
                    </div>
                  </li>
                );
              }
              if (item.kind === "summary") {
                const label = STOP_LABELS[item.summary.stop_reason] || item.summary.stop_reason;
                return (
                  <li key={`summary-${index}`} className="flex gap-2.5">
                    <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-success" />
                    <div>
                      <div className="text-[11px] font-mono uppercase tracking-[0.1em] text-text-muted">探索结束</div>
                      <div className="mt-0.5 text-xs leading-relaxed text-text-main">
                        {label} · 共 {item.summary.iterations} 轮，访问 {item.summary.pages_fetched} 页，临时证据 {item.summary.temporary_evidence_blocks} 块
                      </div>
                    </div>
                  </li>
                );
              }

              const Icon = actionIcon(item.action.action);
              const StatusIcon = item.action.status === "failed"
                ? XCircle
                : item.action.status === "started"
                  ? CircleDashed
                  : CheckCircle2;
              return (
                <li key={`action-${item.action.sequence}-${index}`} className="flex gap-2.5">
                  <Icon size={14} className="mt-0.5 shrink-0 text-primary" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[11px] font-mono uppercase tracking-[0.1em] text-text-muted">
                        {ACTION_LABELS[item.action.action] || item.action.action}
                      </span>
                      <StatusIcon
                        size={11}
                        className={item.action.status === "failed" ? "text-red-500" : item.action.status === "started" ? "animate-spin text-amber-500" : "text-success"}
                      />
                    </div>
                    <div className="mt-0.5 break-words text-xs leading-relaxed text-text-main">
                      {actionSummary(item.action)}
                      {item.action.duration_ms > 0 && (
                        <span className="ml-1 font-mono text-[10px] text-text-muted">
                          {(item.action.duration_ms / 1000).toFixed(2)}s
                        </span>
                      )}
                    </div>
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </section>
  );
}
