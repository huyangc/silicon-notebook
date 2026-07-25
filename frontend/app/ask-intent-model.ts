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
): AskIntentConfirmation {
  return {
    contract,
    resolved_question: resolvedQuestion.trim(),
    answers: contract.ambiguities
      .map((item) => ({ id: item.id, answer: (answers[item.id] || "").trim() }))
      .filter((item) => item.answer),
  };
}
