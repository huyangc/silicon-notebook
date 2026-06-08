// 纯函数：根据会话"最后一轮"是否走了推理，决定恢复会话时推理按钮的默认开关。
// 信号 = 该轮 AskResponse.reasoning_trace 为非空数组（详见 2026-06-08 设计）。
type TurnLike = { response: { reasoning_trace?: unknown[] | null } };

export function lastTurnUsedReasoning(turns: TurnLike[]): boolean {
  const last = turns[turns.length - 1];
  return !!(last?.response?.reasoning_trace && last.response.reasoning_trace.length > 0);
}
