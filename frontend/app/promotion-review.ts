type UnknownRecord = Record<string, unknown>;

export type PromotionReviewCandidate = {
  objectType: string;
  fields: Array<[string, string]>;
};

export type PromotionReviewEvidence = {
  sourceTitle: string;
  locationLabel: string;
  quotedSpan: string;
};

export type PromotionReviewSections = {
  sourceRevision: number;
  candidates: PromotionReviewCandidate[];
  evidence: PromotionReviewEvidence[];
};

function record(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord)
    : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function list(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => text(item)).filter(Boolean);
  }
  if (value && typeof value === "object") {
    return Object.entries(value as UnknownRecord).map(([name, description]) => {
      const detail = text(description);
      return detail ? `${name}: ${detail}` : name;
    });
  }
  return [];
}

function typedFields(objectType: string, payload: UnknownRecord): Array<[string, string]> {
  const definitions: Record<string, Array<[string, string]>> = {
    concept: [["名称", text(payload.name)], ["定义", text(payload.definition)]],
    claim: [["陈述", text(payload.statement)]],
    formula: [
      ["表达式", text(payload.expression)],
      ["变量", list(payload.variables).join("、")],
    ],
    procedure: [
      ["目标", text(payload.goal)],
      ["步骤", list(payload.steps).map((step, index) => `${index + 1}. ${step}`).join("\n")],
    ],
  };
  return (definitions[objectType] ?? []).map(([label, value]) => [
    label,
    value || "未提供",
  ]);
}

/** Build the strict admin-review projection; unknown/private fields are dropped. */
export function promotionReviewSections(candidate: {
  source_kind?: unknown;
  source_revision?: unknown;
  payload?: unknown;
  evidence?: unknown;
}): PromotionReviewSections {
  const payload = record(candidate.payload);
  const rawCandidates = Array.isArray(payload.candidates) ? payload.candidates : [];
  const candidates = rawCandidates.flatMap((raw): PromotionReviewCandidate[] => {
    const item = record(raw);
    const objectType = text(item.object_type);
    if (!["concept", "claim", "formula", "procedure"].includes(objectType)) return [];
    return [{ objectType, fields: typedFields(objectType, record(item.payload)) }];
  });
  const rawEvidence = Array.isArray(candidate.evidence) ? candidate.evidence : [];
  const evidence = rawEvidence.map((raw): PromotionReviewEvidence => {
    const item = record(raw);
    return {
      sourceTitle: text(item.source_title),
      locationLabel: text(item.location_label),
      quotedSpan: text(item.quoted_span),
    };
  });
  return {
    sourceRevision: Number.isInteger(candidate.source_revision)
      ? Number(candidate.source_revision)
      : 0,
    candidates,
    evidence,
  };
}
