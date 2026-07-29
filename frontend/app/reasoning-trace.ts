import type { ReasoningTraceStep } from "./ask-stream";
import { label } from "./vocabulary.ts";

export const TRACE_STEP_LABELS: Record<string, string> = {
  start: "启动",
  intent: "理解",
  memory: "记忆",
  plan: "规划",
  retrieve: "检索",
  enumerate: "枚举",
  reflect: "反思",
  expand: "扩展",
  ppr: "漫游",
  exact_lookup: "精查",
  expand_community: "对比",
  follow_chain: "推导",
  fallback: "原文",
  answer: "合成",
  // answer = 检索器决定作答并报告采用了哪些证据;synthesis = 答案真的写出来了。
  // 分两步是因为中间那次生成调用往往是整轮里最长的一段,合并会让它彻底隐形。
  synthesis: "作答",
  skip: "跳过",
};

// next_action 取值来自 backend/app/services/prompts.py 的状态机决策(reflect 步骤
// next-step 提议),原样显示会把英文动作名泄漏给用户。
// 全部 8 个真实取值见 reasoning_retrieval.py `run()` 循环里的 elif 分支。用「下一步
// 意图」措辞而非机制名(ppr/community/chain 这些是内部机制,不该摆给用户)。
const NEXT_ACTION: Record<string, string> = {
  answer: "开始作答",
  expand_graph: "顺着相关内容继续找",
  add_subquery: "换个角度再查一遍",
  search_elements: "回原文里找细节",
  ppr_retrieve: "顺着关联扩大范围",
  expand_community: "找相似内容对比",
  follow_chain: "顺着推导链继续",
  exact_lookup: "按名称精确查找",
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
  if (step.step_type === "intent" && typeof detail.resolved_question === "string") {
    return detail.resolved_question;
  }
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
  // memory/synthesis 必须先于下面按 detail 形状的通用分支:两者的 count/anchors
  // 数的都不是「候选」,落到通用分支会给出一个读起来对、其实错位的数。
  if (step.step_type === "memory") {
    return typeof detail.count === "number" ? `${detail.count} 条记忆` : "";
  }
  if (step.step_type === "synthesis") {
    // 用 anchors(模型真正绑上的 [k])而不是 citations —— 后者是「每条检索到的
    // 证据一张卡」,零绑定的回答上会读成「10 处引用」。citations/evidence_level
    // 仍留在 detail 里供排查,但不上屏:那是内部口径。included_kg/
    // included_chunks/included_elements 同理:PR-1 止血加的诊断字段,记录真正
    // 进入合成 prompt 的计数(区别于更早 answer 步的候选池计数),同样只供排查
    // 不上屏,不在此处渲染。
    return typeof detail.anchors === "number" ? `${detail.anchors} 处引用` : "";
  }
  if (step.step_type === "exact_lookup") {
    // terms 是服务端本轮真正探测过的名称(已按上限截过),不是问题里出现的全部。
    // 名称和新增段数一起显示,用户才看得出「查的是哪个名字、捞回了多少」——落到
    // 下面的通用 found 分支只说得出后半句。
    const terms = Array.isArray(detail.terms)
      ? detail.terms.filter((term): term is string => typeof term === "string" && !!term)
      : [];
    const parts: string[] = [];
    if (terms.length) parts.push(terms.join("、"));
    if (typeof detail.found === "number") parts.push(`新增 ${detail.found} 段`);
    return parts.join(" · ");
  }
  if (step.step_type === "enumerate" && typeof detail.scanned_rows === "number") {
    return `${detail.scanned_rows}/${Number(detail.known_total_rows ?? 0)} 行`;
  }
  if (typeof detail.count === "number") return `${detail.count} 个候选`;
  if (typeof detail.found === "number") return `新增 ${detail.found}`;
  if (typeof detail.next_action === "string") return label(NEXT_ACTION, detail.next_action, "");
  if (typeof detail.kg === "number" || typeof detail.elements === "number") {
    // 「知识对象」而非「概念」:detail.kg 数的是图谱里的各类对象(Concept / Claim /
    // Formula / Procedure,外加 knowhow 表带来的自定义类型),叫「概念」等于把这堆
    // 类型统统降格成其中一种,用户看到的数与图谱里实际的东西对不上。
    return `${Number(detail.kg ?? 0)} 个知识对象 / ${Number(detail.elements ?? 0)} 段原文`;
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
