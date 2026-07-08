// 重开会话「实时接回」进行中推理的纯逻辑 —— 单测于 ask-reconnect.test.mjs。
// 首连(用户在场)走 WS2a 的 push 流;这里只服务「重连」:轮询 GET …/ask/jobs/{id}
// 增量追加已持久化轨迹,直到终态。
import type { ReasoningTraceStep } from "./ask-stream";

export type AskJobDetail = {
  job_id: string;
  status: string;          // running | done | cancelled | failed | interrupted
  mode: string;
  question: string;
  trace: ReasoningTraceStep[];
  answer_id: string;
  error: string;
};

/** 轮询是否应停止(非 running 即终态)。 */
export function jobPollDone(status: string): boolean {
  return status !== "running";
}

/** 持久化轨迹里「已见 seenCount 步之后」的新步(防越界)。 */
export function newTraceSteps(persisted: ReasoningTraceStep[], seenCount: number): ReasoningTraceStep[] {
  return persisted.length > seenCount ? persisted.slice(seenCount) : [];
}
