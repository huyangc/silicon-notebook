export type QueryIntentTopic = {
  id: string;
  title: string;
  question: string;
  retrieval_queries: string[];
};

export type QueryIntentAmbiguity = {
  id: string;
  question: string;
  reason?: string;
  required?: boolean;
  options?: string[];
};

export type QueryIntentContract = {
  objective: string;
  resolved_question: string;
  intent_type: string;
  result_scope: "ranked" | "complete" | "aggregate" | "hybrid";
  completeness_required: boolean;
  entities: string[];
  mandatory_topics: QueryIntentTopic[];
  comparison_axes: string[];
  constraints: string[];
  excluded_topics: string[];
  expected_output: string;
  assumptions: string[];
  ambiguities: QueryIntentAmbiguity[];
  confidence: number;
  needs_clarification: boolean;
  confirmed: boolean;
  clarification_answers?: { id: string; question: string; answer: string }[];
};

export type AskIntentConfirmation = {
  contract: QueryIntentContract;
  resolved_question: string;
  answers: { id: string; answer: string }[];
  // 问题理解阶段的墙钟耗时。它整段发生在持久 job 之前,后端无从测量;回传后写进
  // 持久轨迹的 intent 步,重开会话回放时总耗时才不会凭空少掉这一段。
  understanding_ms?: number;
};

export function missingRequiredIntentAnswers(
  contract: QueryIntentContract,
  answers: Record<string, string>,
): boolean {
  return contract.ambiguities.some(
    (item) => item.required !== false && !(answers[item.id] || "").trim(),
  );
}

export function buildAskIntentConfirmation(
  contract: QueryIntentContract,
  resolvedQuestion: string,
  answers: Record<string, string>,
  understandingMs?: number,
): AskIntentConfirmation {
  return {
    contract,
    resolved_question: resolvedQuestion.trim(),
    answers: contract.ambiguities
      .map((item) => ({ id: item.id, answer: (answers[item.id] || "").trim() }))
      .filter((item) => item.answer),
    // 只在真的量到时才带上;缺省让后端保持 duration 未知,而不是记一个假的 0。
    ...(typeof understandingMs === "number" && understandingMs >= 0
      ? { understanding_ms: Math.round(understandingMs) }
      : {}),
  };
}
