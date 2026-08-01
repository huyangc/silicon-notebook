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

export type QueryIntentSourceSnapshot = {
  source_id: string;
  notebook_id: string;
  title: string;
  source_file_name: string;
};

export type QueryIntentSourceScope = {
  mode: "selected";
  sources: QueryIntentSourceSnapshot[];
  /** Opaque, short-lived server capability echoed only when confirming scope. */
  preview_capability?: string;
};

export type QueryIntentContract = {
  objective: string;
  resolved_question: string;
  intent_type: string;
  result_scope: "ranked" | "complete" | "aggregate" | "hybrid";
  completeness_required: boolean;
  entities: string[];
  /** 用户明确写出的来源引用；缺省/空数组保持历史全来源行为。 */
  source_refs?: string[];
  /** 服务端在授权目录中解析、签发的只读来源范围快照。 */
  source_scope?: QueryIntentSourceScope;
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

// 镜像后端 AskIntentConfirmation.understanding_ms 的上限(models/ask.py 的 le)。
// 超限必须在这里夹住而不是原样上送:后端会 422,整个已确认的提问就此打不出去 ——
// 一个「这次理解花了多久」的展示字段没有资格否掉用户的提问。浏览器休眠、机器
// 挂起都能轻易让这个计时跨过一小时。
const UNDERSTANDING_MS_LIMIT = 3_600_000;

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
      ? {
        understanding_ms: Math.min(
          Math.round(understandingMs), UNDERSTANDING_MS_LIMIT,
        ),
      }
      : {}),
  };
}
