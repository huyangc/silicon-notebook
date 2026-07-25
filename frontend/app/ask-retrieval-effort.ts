// Reasoning Ask 的检索档位单一真源。这里是产品/前端可见的阈值契约；后端
// app/core/ask_retrieval_policy.py 保持同一套数值，测试同时锁住这两处。

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

export function retrievalEffort(id: AskRetrievalEffortId): AskRetrievalEffort {
  const effort = ASK_RETRIEVAL_EFFORTS.find((item) => item.id === id);
  if (!effort) throw new Error(`unknown Ask retrieval effort: ${id}`);
  return effort;
}

export function retrievalEffortFromTurn(
  turn: { response?: { retrieval_effort?: string } } | undefined,
): AskRetrievalEffortId {
  const effort = turn?.response?.retrieval_effort;
  return ASK_RETRIEVAL_EFFORTS.some((item) => item.id === effort)
    ? effort as AskRetrievalEffortId
    : DEFAULT_ASK_RETRIEVAL_EFFORT;
}

/** 供控件的原生 title：把所有可影响结果量的数字直接给用户看。 */
export function retrievalEffortThresholdText(id: AskRetrievalEffortId): string {
  const effort = retrievalEffort(id);
  const ranked = effort.ranked;
  const enumeration = STRUCTURED_ENUMERATION_LIMITS;
  return [
    `${effort.label}：每条查询取 ${ranked.perQuery} 条；最终证据至少 ${ranked.finalFloor} 条、每个问题方面增加 ${ranked.finalAspect} 条、最多 ${ranked.finalCap} 条；最多 ${ranked.maxSteps} 个推理步骤、${ranked.maxSubqueries} 条首轮子查询；知识图谱（含记忆/推导链）/原文（含结构化预览/来源元素）上下文最多 ${ranked.kgContextChars}/${ranked.chunkContextChars} 字符。`,
    "模型可在上述上限内提前停止，但不能突破上限；显式“全部”不会因档位较低而缩小枚举范围。",
    `整表物理行清单/直接计数：每页 ${enumeration.pageRows} 行、最多 ${enumeration.maxPages} 页/${enumeration.maxRows} 行、${enumeration.maxTables} 表/每表 ${enumeration.maxColumns} 列、模型单元格摘录 ${enumeration.cellExcerptChars} 字符、结构化载荷 ${enumeration.payloadChars} 字符；正文及混合分析最多看到 ${enumeration.inlineAnswerRows} 行，结果卡初始显示 ${enumeration.initialVisibleRows} 行。条件筛选、排除重复项、分组目前不声称完整；超过上限明确标记 ${enumeration.overflowReason}。`,
  ].join(" ");
}
