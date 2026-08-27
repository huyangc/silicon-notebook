export type ReasoningTraceStep = {
  step_type: string;
  summary: string;
  detail: Record<string, unknown>;
  // 该步墙钟耗时(毫秒),由后端在 record() 处按相邻两步的时间差测得;
  // 合成态(start 合成步)可能缺失 —— 前端按 0 处理。
  duration_ms?: number;
};

export type AskStreamEvent<TResponse> =
  | { event: "started"; job_id: string; conversation_id: string }
  | { event: "progress"; step: ReasoningTraceStep }
  | { event: "final"; response: TResponse }
  | { event: "cancelled" }
  | { event: "error"; error: string };

export { takeNdjsonLines } from "./ndjson.ts";
