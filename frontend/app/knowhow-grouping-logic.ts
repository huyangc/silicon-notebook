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

export interface GridCell {
  columnId: string;
  text: string;
  /** >1：合并起始格（跨 rowSpan 行）；1：独立格；0：被上方合并覆盖，渲染跳过。 */
  rowSpan: number;
}

export interface GridDisplayRow {
  row: KnowhowRow;
  cells: GridCell[];
}

/** 把分组后的行展开成带 rowspan 的网格（spec §4.2.1）：每一组内、每一列，
 * 相邻同值（trim 后）的连续段合并成一个 rowSpan 起始格，段内其余行该列
 * rowSpan=0（渲染跳过）。跨组绝不合并——每组第一行的每列都是新的起始格。
 * 值以 trim 比较但 text 保留原样（首行原文）。 */
export function computeGridSpans(groups: AnchorGroup[], columns: KnowhowColumn[]): GridDisplayRow[] {
  const out: GridDisplayRow[] = [];
  for (const group of groups) {
    const n = group.rows.length;
    for (let i = 0; i < n; i++) {
      const row = group.rows[i];
      const cells: GridCell[] = columns.map((col) => {
        const text = row.cells[col.id] ?? "";
        const key = text.trim();
        // 被上一行同列同值覆盖？（i>0 且上一行该列 trim 相同）
        if (i > 0) {
          const prev = (group.rows[i - 1].cells[col.id] ?? "").trim();
          if (prev === key) return { columnId: col.id, text, rowSpan: 0 };
        }
        // 合并起始：向下数连续同值行数。
        let span = 1;
        for (let j = i + 1; j < n; j++) {
          if ((group.rows[j].cells[col.id] ?? "").trim() === key) span++;
          else break;
        }
        return { columnId: col.id, text, rowSpan: span };
      });
      out.push({ row, cells });
    }
  }
  return out;
}
