import { cellSummary, type KnowhowColumn, type KnowhowRow, type KnowhowRowCompletionSuggestion } from "./knowhow-model.ts";
import { groupRowsByAnchor, isSharedColumn } from "./knowhow-grouping-logic.ts";
import { resolveRowTitleText, sortColumnsByPosition } from "./knowhow-panel-logic.ts";

export const COMPLETION_CONFIDENCE_LABELS = {
  high: "高置信度",
  medium: "中置信度",
  low: "低置信度",
} as const;

/** 只补存储值逐字等于空串的非行标题列。expected_before 固定用 ""，所以不能
 * 把空白字符视为空，否则客户端认为可补、服务端却必然按基线不一致拒绝。 */
export function completableKnowhowColumns(
  row: KnowhowRow,
  columns: KnowhowColumn[],
  anchorColumnId: string | null,
): KnowhowColumn[] {
  return sortColumnsByPosition(columns).filter(
    (column) => column.id !== anchorColumnId && (row.cells[column.id] ?? "") === "",
  );
}

export type CompletionSavePlan = {
  targetRowIds: string[];
  expectedByRowId: Map<string, string>;
  anchorGuard?: { anchorColumnId: string; expectedAnchorByRowId: Map<string, string> };
};

/**
 * 弹窗打开时冻结一格真正保存所需的完整并发守卫。共享空格写整组，并冻结组员、
 * 每成员空串基线和 anchor 原值；非共享列只冻结当前行，不误带成员守卫。
 */
export function completionSavePlan(
  rowId: string,
  columnId: string,
  rows: KnowhowRow[],
  anchorColumnId: string | null,
): CompletionSavePlan {
  const currentOnly = {
    targetRowIds: [rowId],
    expectedByRowId: new Map([[rowId, ""]]),
  };
  if (!anchorColumnId) return currentOnly;
  const group = groupRowsByAnchor(rows, anchorColumnId).find((candidate) =>
    candidate.rows.some((row) => row.id === rowId),
  );
  // isSharedColumn 按 trim 判同值，适合显示合并语义，却不足以支撑这里固定为
  // expected_before="" 的写计划：任一兄弟若实际存的是空格，批量请求必然 409。
  // 只有整组 raw 值都逐字为空（缺失按空串）才规划共享批量写；否则当前物理行
  // 按非共享单格保存，不把空白兄弟错误纳入冻结成员。
  if (
    !group
    || !isSharedColumn(group, columnId)
    || !group.rows.every((row) => (row.cells[columnId] ?? "") === "")
  ) return currentOnly;
  return {
    targetRowIds: group.rows.map((row) => row.id),
    expectedByRowId: new Map(group.rows.map((row) => [row.id, ""])),
    anchorGuard: {
      anchorColumnId,
      expectedAnchorByRowId: new Map(group.rows.map((row) => [row.id, row.cells[anchorColumnId] ?? ""])),
    },
  };
}

export function canAcceptCompletionSuggestion(suggestion: KnowhowRowCompletionSuggestion): boolean {
  return Boolean(suggestion.suggestionMd?.trim()) && !suggestion.abstainReason.trim();
}

/** 参考行的主文案永远是可读标题；id 只用于查找，不泄漏进降级文案。 */
export function completionReferenceLabel(
  rowId: string,
  rows: KnowhowRow[],
  columns: KnowhowColumn[],
): string {
  const row = rows.find((candidate) => candidate.id === rowId);
  if (!row) return "表内相关记录";
  return cellSummary(resolveRowTitleText(row, columns), 80) || `第 ${row.position + 1} 行`;
}
