// Reasoning Ask 的检索档位单一真源。这里是产品/前端可见的阈值契约；后端
// app/core/ask_retrieval_policy.py 保持同一套数值，测试同时锁住这两处。

import type { EffortOption } from "./effort-picker";

export type AskRetrievalEffortId =
  | "overview"
  | "standard"
  | "deep"
  | "thorough"
  | "exhaustive";

export type AskRetrievalEffort = {
  id: AskRetrievalEffortId;
  label: string;
  description: string;
  ranked: {
    perQuery: number;
    finalFloor: number;
    finalAspect: number;
    finalCap: number;
    maxSteps: number;
    maxSubqueries: number;
    kgContextChars: number;
    chunkContextChars: number;
  };
};

export const ASK_RETRIEVAL_EFFORTS: readonly AskRetrievalEffort[] = Object.freeze([
  {
    id: "overview", label: "概览", description: "快速确认方向",
    ranked: { perQuery: 4, finalFloor: 8, finalAspect: 2, finalCap: 12, maxSteps: 4, maxSubqueries: 2, kgContextChars: 4_000, chunkContextChars: 12_000 },
  },
  {
    id: "standard", label: "标准", description: "日常问答默认",
    ranked: { perQuery: 8, finalFloor: 20, finalAspect: 3, finalCap: 36, maxSteps: 8, maxSubqueries: 5, kgContextChars: 6_000, chunkContextChars: 30_000 },
  },
  {
    id: "deep", label: "深入", description: "多方向查证",
    ranked: { perQuery: 8, finalFloor: 24, finalAspect: 4, finalCap: 48, maxSteps: 16, maxSubqueries: 6, kgContextChars: 8_000, chunkContextChars: 50_000 },
  },
  {
    id: "thorough", label: "详尽", description: "更广覆盖与核对",
    ranked: { perQuery: 12, finalFloor: 32, finalAspect: 5, finalCap: 64, maxSteps: 32, maxSubqueries: 8, kgContextChars: 12_000, chunkContextChars: 80_000 },
  },
  {
    id: "exhaustive", label: "穷尽", description: "优先覆盖，不把枚举伪装成大 TopN",
    ranked: { perQuery: 16, finalFloor: 40, finalAspect: 6, finalCap: 96, maxSteps: 50, maxSubqueries: 10, kgContextChars: 16_000, chunkContextChars: 120_000 },
  },
] as const);

export const DEFAULT_ASK_RETRIEVAL_EFFORT: AskRetrievalEffortId = "standard";

/**
 * 喂给共享档位控件(effort-picker)的档位表。与深度报告的「研究深度」共用同一个控件,
 * 所以这里只做投影:id/档名照搬,description 就是控件里给选中档显示的那句说明。
 */
export const ASK_RETRIEVAL_EFFORT_OPTIONS: readonly EffortOption[] = Object.freeze(
  ASK_RETRIEVAL_EFFORTS.map((effort) => ({
    id: effort.id,
    label: effort.label,
    hint: effort.description,
  })),
);

/** complete / aggregate / hybrid 的逐页枚举上限；触顶必须回传 explicit_partial。 */
export const STRUCTURED_ENUMERATION_LIMITS = Object.freeze({
  pageRows: 25,
  maxPages: 50,
  maxRows: 1_250,
  maxTables: 8,
  maxColumns: 8,
  cellExcerptChars: 1_000,
  payloadChars: 256_000,
  inlineAnswerRows: 100,
  initialVisibleRows: 20,
  overflowReason: "explicit_partial",
});

export function retrievalEffortFromTurn(
  turn: { response?: { retrieval_effort?: string } } | undefined,
): AskRetrievalEffortId {
  const effort = turn?.response?.retrieval_effort;
  return ASK_RETRIEVAL_EFFORTS.some((item) => item.id === effort)
    ? effort as AskRetrievalEffortId
    : DEFAULT_ASK_RETRIEVAL_EFFORT;
}

// 档位阈值曾以一整段文案铺在界面上（控件 title + 「阈值」折叠块）。产品决定不再向用户
// 呈现这些数字，档位只用一句说明表达，于是那段拼接文案随控件一起删除。数值契约本身没有
// 松动：它由上面的 ASK_RETRIEVAL_EFFORTS / STRUCTURED_ENUMERATION_LIMITS 与后端
// app/core/ask_retrieval_policy.py 逐项锁住。
