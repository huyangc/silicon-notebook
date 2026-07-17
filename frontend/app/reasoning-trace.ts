import type { ReasoningTraceStep } from "./ask-stream";
import { label } from "./vocabulary.ts";

export const TRACE_STEP_LABELS: Record<string, string> = {
  start: "启动",
  plan: "规划",
  retrieve: "检索",
  reflect: "反思",
  expand: "扩展",
  ppr: "漫游",
  expand_community: "对比",
  follow_chain: "推导",
  fallback: "原文",
  answer: "合成",
  skip: "跳过",
};

// next_action 取值来自 backend/app/services/prompts.py 的状态机决策(reflect 步骤
// next-step 提议),原样显示会把英文动作名泄漏给用户。
// 全部 7 个真实取值见 reasoning_retrieval.py:529-726 的 elif 分支。用「下一步意图」
// 措辞而非机制名(ppr/community/chain 这些是内部机制,不该摆给用户)。
const NEXT_ACTION: Record<string, string> = {
  answer: "开始作答",
  expand_graph: "顺着相关内容继续找",
  add_subquery: "换个角度再查一遍",
  search_elements: "回原文里找细节",
  ppr_retrieve: "顺着关联扩大范围",
  expand_community: "找相似内容对比",
  follow_chain: "顺着推导链继续",
};

export type ReasoningTraceSummary = {
  title: string;
  latestLabel: string;
  latestSummary: string;
  latestDetail: string;
  stepCountLabel: string;
  totalLabel: string;
};

// 把毫秒渲染成人话:<1s 用 ms、<1min 用 x.xs、更久用 xmxs。
// 非有限/负值一律归零,避免 NaN 泄漏到 UI。
export function formatDuration(ms: number): string {
  const v = Number.isFinite(ms) ? Math.max(0, Math.round(ms)) : 0;
  if (v < 1000) return `${v}ms`;
  if (v < 60000) return `${(v / 1000).toFixed(1)}s`;
  const totalSec = Math.round(v / 1000);
  return `${Math.floor(totalSec / 60)}m${totalSec % 60}s`;
}

// 轨迹总耗时 = 各步 duration_ms 之和(缺失按 0)。
function totalDurationMs(steps: ReasoningTraceStep[]): number {
  return steps.reduce(
    (sum, step) => sum + (typeof step.duration_ms === "number" ? step.duration_ms : 0),
    0,
  );
}

export function getTraceStepDetail(step: ReasoningTraceStep): string {
  const detail = step.detail ?? {};
  if (step.step_type === "follow_chain") {
    const parts: string[] = [];
    if (typeof detail.hops === "number") parts.push(`${detail.hops} 跳`);
    if (typeof detail.count === "number") parts.push(`${detail.count} 条`);
    if (typeof detail.chain_trust === "number") {
      const percentage = Math.round(Math.max(0, Math.min(1, detail.chain_trust)) * 100);
      parts.push(`可信度 ${percentage}%`);
    }
    return parts.join(" · ");
  }
  if (step.step_type === "plan" && Array.isArray(detail.sub_queries)) {
    return `${detail.sub_queries.length} 个子查询`;
  }
  if (typeof detail.count === "number") return `${detail.count} 个候选`;
  if (typeof detail.found === "number") return `新增 ${detail.found}`;
  if (typeof detail.next_action === "string") return label(NEXT_ACTION, detail.next_action, "");
  if (typeof detail.kg === "number" || typeof detail.elements === "number") {
    return `${Number(detail.kg ?? 0)} 个 KG / ${Number(detail.elements ?? 0)} 段原文`;
  }
  return "";
}

export function getReasoningTraceSummary(
  steps: ReasoningTraceStep[],
  live = false,
): ReasoningTraceSummary {
  const latest = steps[steps.length - 1];
  if (!latest) {
    return {
      title: live ? "Agent 推理中" : "Agent 推理轨迹",
      latestLabel: "",
      latestSummary: "等待后端事件…",
      latestDetail: "",
      stepCountLabel: "0 步",
      totalLabel: "",
    };
  }
  const totalMs = totalDurationMs(steps);
  return {
    title: live ? "Agent 推理中" : "Agent 推理轨迹",
    latestLabel: label(TRACE_STEP_LABELS, latest.step_type, "处理中"),
    latestSummary: latest.summary,
    latestDetail: getTraceStepDetail(latest),
    stepCountLabel: `${steps.length} 步`,
    totalLabel: totalMs > 0 ? formatDuration(totalMs) : "",
  };
}
