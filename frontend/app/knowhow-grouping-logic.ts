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
 * 保留在其自然位置，不并入任何概念（spec §4.2.2）。
 *
 * 同值判定只用 `.trim()`（去首尾空白），比后端 KG 合并用的
 * `textops.value_key`（backend/app/services/knowhow/textops.py：casefold +
 * 整体剔除所有空白/标点 run，不止首尾）更细——这是刻意的分歧，不是遗漏的
 * 对齐项：trim 相等 ⟹ value_key 相等（对同一段字面量做 casefold/去标点是
 * 纯函数，输入相同输出必相同），反之不然——如"示波器"/"示波器。"/"示波器 "
 * 三者 value_key 相同（KG 视为同一概念），但 trim 后仍是三个不同字符串，
 * 这里会分成三组。分歧只在异构手写输入时出现，且方向恒安全：网格里合并
 * 显示的两行，KG 必然也把它们合并成同一概念（更细的分组是更粗的分组的
 * 加细），所以网格绝不会显示一个 KG 里其实不存在的合并；反过来 KG 合并了
 * 而网格没合并，只是显示更保守，不会误导用户。 */
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

export interface MatrixAttrRow {
  columnId: string;
  columnName: string;
  /** 每个分支的值，与 ConceptMatrix.branchRowIds 一一对齐。 */
  values: string[];
  /** 全分支同值（trim）→ C 抽屉里跨分支合并成一格（spec §4.3）。 */
  sharedSpan: boolean;
}

export interface ConceptMatrix {
  anchorValue: string;
  branchRowIds: string[];
  attrRows: MatrixAttrRow[];
}

/** 把一个概念组构造成 C 抽屉的「属性×分支」矩阵（spec §4.3）：非 anchor 列
 * 成属性行，组内每行成一个分支列；某属性行全分支同值 → sharedSpan 让抽屉
 * 跨分支合并显示。列顺序按传入 columns 顺序（调用方已排好）。 */
export function buildConceptMatrix(
  group: AnchorGroup,
  columns: KnowhowColumn[],
  anchorColumnId: string,
): ConceptMatrix {
  const branchRowIds = group.rows.map((r) => r.id);
  const attrRows: MatrixAttrRow[] = columns
    .filter((col) => col.id !== anchorColumnId)
    .map((col) => {
      const values = group.rows.map((r) => r.cells[col.id] ?? "");
      const first = values.length ? values[0].trim() : "";
      const sharedSpan = values.length > 1 && values.every((v) => v.trim() === first);
      return { columnId: col.id, columnName: col.name, values, sharedSpan };
    });
  return { anchorValue: group.anchorValue, branchRowIds, attrRows };
}

/** 合并格改整组时要写回的 rowId 列表（spec §4.4：= 组内全部行）。 */
export function groupCellWriteTargets(group: AnchorGroup, _columnId: string): string[] {
  return group.rows.map((r) => r.id);
}

/** 该列在这个概念组内是否是「合并共享格」（spec §4.4：组内多于一行、且该列
 * 所有行的值 trim 后完全相同）——与 buildConceptMatrix 的 sharedSpan 同一套
 * 判定标准，供 panel 决定编辑一格是批量写整组还是只写这一行。单行组没有
 * 「其他分支」可言，恒 false（即便技术上"全同值"，语义上不构成共享）。
 * 同值判定同样只用 `.trim()`——与 groupRowsByAnchor 用的是同一套「比后端 KG
 * value_key 更细、分歧方向恒安全」的刻意选择，理由见该函数注释，不在此重复。 */
export function isSharedColumn(group: AnchorGroup, columnId: string): boolean {
  if (group.rows.length <= 1) return false;
  const first = (group.rows[0].cells[columnId] ?? "").trim();
  return group.rows.every((row) => (row.cells[columnId] ?? "").trim() === first);
}
