import type { ReasoningTraceStep } from "./ask-stream";

export const TRACE_STEP_LABELS: Record<string, string> = {
  start: "启动",
  plan: "规划",
  retrieve: "检索",
  reflect: "反思",
  expand: "扩展",
  fallback: "原文",
  answer: "合成",
  skip: "跳过",
};

export type ReasoningTraceSummary = {
  title: string;
  latestLabel: string;
  latestSummary: string;
  latestDetail: string;
  stepCountLabel: string;
};

export function getTraceStepDetail(step: ReasoningTraceStep): string {
  const detail = step.detail ?? {};
  if (step.step_type === "plan" && Array.isArray(detail.sub_queries)) {
    return `${detail.sub_queries.length} 个子查询`;
  }
  if (typeof detail.count === "number") return `${detail.count} 个候选`;
  if (typeof detail.found === "number") return `新增 ${detail.found}`;
  if (typeof detail.next_action === "string") return detail.next_action;
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
    };
  }
  return {
    title: live ? "Agent 推理中" : "Agent 推理轨迹",
    latestLabel: TRACE_STEP_LABELS[latest.step_type] ?? latest.step_type,
    latestSummary: latest.summary,
    latestDetail: getTraceStepDetail(latest),
    stepCountLabel: `${steps.length} 步`,
  };
}
