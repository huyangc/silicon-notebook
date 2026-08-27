// 「问题理解」阶段的轨迹步骤(纯函数;单测于 ask-intent-trace.test.mjs)。
//
// 这一段跑在 `/ask/intent`,发生在持久 ask job 存在之前 —— 后端此刻还没有 job
// 可以挂轨迹,所以由前端合成这几步,再与随后从 `/ask/stream` 流下来的后端步骤
// 拼成同一条轨迹。合成步只活在本次实时展示里:重开会话回放的是后端持久化的
// 轨迹,那里由 intent 步(带前端回传的 understanding_ms)代表这一阶段。
import type { QueryIntentContract } from "./ask-intent-model";
import type { ReasoningTraceStep } from "./ask-stream";

// 与后端 pre_trace 的首步同类型:折叠行的标签、展开列表的分组都按它取。
const INTENT_STEP_TYPE = "intent";

/** 墙钟差,非有限/倒流一律归零(系统时钟被回拨也不能出现负耗时)。 */
export function elapsedMs(startedAt: number, endedAt: number): number {
  const delta = endedAt - startedAt;
  return Number.isFinite(delta) && delta > 0 ? Math.round(delta) : 0;
}

/** 预检在途:此刻真的还没有读取任何资料,措辞要顶住用户「怎么还没开始」的疑问。 */
export function intentUnderstandingStep(durationMs?: number): ReasoningTraceStep {
  return {
    step_type: INTENT_STEP_TYPE,
    summary: "正在理解问题，尚未读取资料或开始检索",
    detail: {},
    ...(typeof durationMs === "number" && durationMs > 0
      ? { duration_ms: Math.round(durationMs) }
      : {}),
  };
}

/** 预检返回且无阻断性歧义:问题已定,下一步就进检索。 */
export function intentUnderstoodStep(
  contract: QueryIntentContract,
  durationMs: number,
): ReasoningTraceStep {
  const topics = contract.mandatory_topics.length;
  return {
    step_type: INTENT_STEP_TYPE,
    summary: topics > 0 ? `已理解问题，梳理出 ${topics} 个必答要点` : "已理解问题",
    detail: { resolved_question: contract.resolved_question },
    duration_ms: durationMs,
  };
}

/** 预检返回但有阻断性歧义:轨迹停在这里,等用户在下方的确认卡片里补充。 */
export function intentClarifyStep(
  contract: QueryIntentContract,
  durationMs: number,
): ReasoningTraceStep {
  return {
    step_type: INTENT_STEP_TYPE,
    summary: `发现 ${contract.ambiguities.length} 处会改变检索方向的歧义，等待你补充`,
    detail: { resolved_question: contract.resolved_question },
    duration_ms: durationMs,
  };
}

/** 用户在确认卡片里定稿。刻意不带 duration_ms —— 这段等待是人在填表,
 *  不是系统在跑,计进轨迹总耗时会把「这次问答花了多久」读成假的。 */
export function intentConfirmedStep(
  resolvedQuestion: string,
  answerCount: number,
): ReasoningTraceStep {
  return {
    step_type: INTENT_STEP_TYPE,
    summary: answerCount > 0
      ? `已确认最终问题，并补充了 ${answerCount} 条说明`
      : "已确认最终问题",
    detail: { resolved_question: resolvedQuestion },
  };
}

/** 用理解结果替换在途的那一步(轨迹只保留终态,不留「正在理解」的残影)。 */
export function replaceLastIntentStep(
  steps: ReasoningTraceStep[],
  resolved: ReasoningTraceStep,
): ReasoningTraceStep[] {
  return [...steps.slice(0, -1), resolved];
}

/** 交给 executeAsk 之前把这段前缀的耗时摘掉。
 *
 * 同一段理解时间在实时面板里会出现两次:这里的合成步一次,后端 `intent` 步
 * (取自回传的 `understanding_ms`)又一次。面板的总耗时是逐步求和,两处都留就
 * 把这一段算了两遍。后端那一步是持久化、可回放的那一份,所以由它保留耗时;
 * 合成步在理解阶段独自显示时仍带耗时,只在拼进整条轨迹前摘掉。 */
export function handOffIntentTrace(
  steps: ReasoningTraceStep[],
): ReasoningTraceStep[] {
  return steps.map(({ duration_ms: _dropped, ...step }) => step);
}
