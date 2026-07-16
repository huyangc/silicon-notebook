// Knowhow anchor 分组视图的纯逻辑（转置/合并型表格支持，设计见
// docs/superpowers/specs/2026-07-16-knowhow-anchor-grouping-display.md）。
// 只做「rows → 分组 / rowspan 合并 / 概念矩阵」的形状变换，不含 React、
// 不含 fetch；供 knowhow-grouping-logic.test.mjs 直接 import。
import type { KnowhowRow, KnowhowColumn } from "./knowhow-model.ts";

export interface AnchorGroup {
  /** 该组的概念值；空串表示「无概念」的独立行（leading-blank，未 forward-fill 到）。 */
  anchorValue: string;
  rows: KnowhowRow[];
}

/** 按 anchor 列值把行分组：同值聚成一组（组顺序 = 该值首次出现顺序，组内
 * 保持原相对顺序）。空 anchor 值不聚合——每个空行单独成组（anchorValue=""），
 * 保留在其自然位置，不并入任何概念（spec §4.2.2）。 */
export function groupRowsByAnchor(rows: KnowhowRow[], anchorColumnId: string): AnchorGroup[] {
  const groups: AnchorGroup[] = [];
  const indexByValue = new Map<string, number>();
  for (const row of rows) {
    const value = (row.cells[anchorColumnId] ?? "").trim();
    if (!value) {
      groups.push({ anchorValue: "", rows: [row] });
      continue;
    }
    const existing = indexByValue.get(value);
    if (existing === undefined) {
      indexByValue.set(value, groups.length);
      groups.push({ anchorValue: value, rows: [row] });
    } else {
      groups[existing].rows.push(row);
    }
  }
  return groups;
}
