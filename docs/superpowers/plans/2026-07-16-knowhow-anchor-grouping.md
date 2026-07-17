# Knowhow Anchor 分组显示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让转置/合并型 knowhow 表（EDA 违例知识沉淀那类）导入不丢数据、以"违例概念"为单位分组显示（G2 合并矩阵 + C 概念矩阵抽屉），保留同分支横向对应。

**Architecture:** 后端在导入落库前对 anchor 列做 forward-fill（合并单元格 fill 已交付），让同概念多分支的 anchor 值一致 → 复用既有 cell-level node model 自动归并成一个 anchor KO。前端把主网格从"物理行平铺"改为"相邻同值 rowspan 合并矩阵"，点概念打开"属性×分支"矩阵抽屉；合并/拆分靠相邻同值现算，不存 span 元信息。

**Tech Stack:** Python 3.13 / pytest（后端）；Next.js 15 + React 19 + TypeScript（前端）；前端纯逻辑抽到 `*-logic.ts` 用 `node --test *.test.mjs` 覆盖，`.tsx` 渲染靠浏览器验证（项目现状：无组件测试框架）。

## Global Constraints

以下为全局约束，每个 task 隐含遵守（值逐字取自 spec `2026-07-16-knowhow-anchor-grouping-display.md`）：

- **不改 KG 投影/检索召回逻辑**：`projection.py`、检索候选逻辑一律不动；anchor 归并复用既有 cell-level node model。
- **适用范围**：仅"有 anchor 列"的表启用分组视图；**记录型表（无 `anchorColumnId`）保持现状平铺网格，零改动**。运行期按 `detail.anchorColumnId` 是否存在切换。
- **合并靠相邻同值现算，不存 span 元信息**；改值后合并自动重算（拆开/并回免费）。
- **forward-fill 仅对 anchor 列、仅向下填充**（leading 空保持空）。
- **合并格改整组 = 批量写该组所有行该列**，单事务；浮层显示"影响 N 个分支"提示。
- **UI 文案**：违例概念下的多个诊断-对策叫「**分支**」。
- **前端纯逻辑必须抽到 `*-logic.ts` 并有 `*.test.mjs`**；`.tsx` 渲染改动靠浏览器 verification workflow 验证。
- **中文注释**沿用现有 knowhow 文件风格。
- **worktree/分支**：已在 `claude/knowhow-anchor-grouping-display`（基于 master）。合并 fill 已提交（`3a8820f`）。

---

## File Structure

**后端（改动）**
- `backend/app/services/knowhow/grid_parser.py` — 新增纯函数 `forward_fill_column`（与既有 `_expand_merged_ranges` 同族，导入层数据规整）。
- `backend/app/services/knowhow/api.py` — `import_table` 落库前调用 `forward_fill_column`。
- `backend/tests/test_knowhow_grid_parser.py` — forward-fill 单测。

**前端（新增）**
- `frontend/app/knowhow-grouping-logic.ts` — 分组视图全部纯逻辑：`groupRowsByAnchor` / `computeGridSpans` / `buildConceptMatrix` / `groupCellWriteTargets`。一个文件，一个职责（"把 rows 变成分组/合并/矩阵结构"）。
- `frontend/app/knowhow-grouping-logic.test.mjs` — 上述纯函数的 `node --test`。
- `frontend/app/knowhow-matrix-drawer.tsx` — C 概念矩阵抽屉组件（消费 `buildConceptMatrix`）。

**前端（改动）**
- `frontend/app/knowhow-panel.tsx` — 主网格有 anchor 时切 G2 合并矩阵渲染；点概念开矩阵抽屉；加分支/加概念/删概念入口；记录型表回退现状。
- `frontend/app/knowhow-cell-editor.tsx` — 合并格编辑"影响 N 个分支"提示（新 prop）。
- `frontend/app/answer-panel.tsx` / `frontend/app/knowhow-panel.tsx` — 引用跳转目标从行抽屉改为概念矩阵抽屉 + 高亮命中分支。

---

## Phase 1 — 后端：Anchor 列 forward-fill

### Task 1: `forward_fill_column` 纯函数

**Files:**
- Modify: `backend/app/services/knowhow/grid_parser.py`（在 `_expand_merged_ranges` 之后追加）
- Test: `backend/tests/test_knowhow_grid_parser.py`

**Interfaces:**
- Produces: `forward_fill_column(rows: list[list[str]], col_index: int) -> list[list[str]]` — 返回新列表，不改入参；只对 `col_index` 列向下填充，leading 空保持空。

- [ ] **Step 1: 写失败测试**

在 `test_knowhow_grid_parser.py` 末尾（合并测试之后）追加：

```python
# --- forward_fill_column（anchor 分组列填充）-------------------------------
from app.services.knowhow.grid_parser import forward_fill_column


def test_forward_fill_column_fills_blanks_below_first_value():
    # 分组列"只写一次"：首行有值，后续空 → 全部继承。
    rows = [["hold&setup", "case-A"], ["", "case-B"], ["", "case-C"]]
    assert forward_fill_column(rows, 0) == [
        ["hold&setup", "case-A"],
        ["hold&setup", "case-B"],
        ["hold&setup", "case-C"],
    ]


def test_forward_fill_column_restarts_at_next_nonempty():
    # 多概念分段：每遇到新非空值就换继承源。
    rows = [["A", "1"], ["", "2"], ["B", "3"], ["", "4"]]
    assert forward_fill_column(rows, 0) == [
        ["A", "1"], ["A", "2"], ["B", "3"], ["B", "4"],
    ]


def test_forward_fill_column_leading_blanks_stay_blank():
    # 开头就空（前面无非空可继承）→ 保持空。
    rows = [["", "1"], ["A", "2"], ["", "3"]]
    assert forward_fill_column(rows, 0) == [["", "1"], ["A", "2"], ["A", "3"]]


def test_forward_fill_column_only_target_column():
    # 只填目标列；其他列的空是真空，不动。
    rows = [["A", "x"], ["", ""], ["", "z"]]
    assert forward_fill_column(rows, 0) == [["A", "x"], ["A", ""], ["A", "z"]]


def test_forward_fill_column_does_not_mutate_input():
    rows = [["A", "1"], ["", "2"]]
    forward_fill_column(rows, 0)
    assert rows == [["A", "1"], ["", "2"]]  # 原 list 不变


def test_forward_fill_column_whitespace_only_counts_as_blank():
    # 纯空白视为空（与 _build_grid 的 strip 判空一致）。
    rows = [["A", "1"], ["   ", "2"]]
    assert forward_fill_column(rows, 0) == [["A", "1"], ["A", "2"]]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/hzf/workspace/silicon_notebook && PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_knowhow_grid_parser.py -k forward_fill -q`
Expected: FAIL — `ImportError: cannot import name 'forward_fill_column'`

- [ ] **Step 3: 写实现**

在 `grid_parser.py` 的 `_expand_merged_ranges` 函数之后追加：

```python
def forward_fill_column(rows: list[list[str]], col_index: int) -> list[list[str]]:
    """Forward-fill ONE column: each blank cell inherits the last non-blank
    value above it in that column. Applied to the anchor column at import
    time (see ``app.services.knowhow.api.import_table``) so a "分组只写一次"
    concept column — its value written once on a group's first row, blank on
    the sibling rows below — becomes a fully-populated column that the
    projector (``projection.py``: anchor-blank rows are dropped) and the grid
    can both treat as one concept per group.

    Only fills DOWN from a value already seen: a LEADING blank (no non-blank
    above it yet) stays blank, handled downstream as an unnamed/independent
    row. Whitespace-only is blank (matches ``_build_grid``'s strip test).
    Returns a NEW list of NEW rows; never mutates the input."""
    result: list[list[str]] = []
    last = ""
    for row in rows:
        new_row = list(row)
        if col_index < len(new_row):
            cell = new_row[col_index]
            if cell.strip():
                last = cell
            elif last:
                new_row[col_index] = last
        result.append(new_row)
    return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/hzf/workspace/silicon_notebook && PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_knowhow_grid_parser.py -k forward_fill -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/knowhow/grid_parser.py backend/tests/test_knowhow_grid_parser.py
git commit -m "feat(knowhow): forward_fill_column — anchor 分组列填充纯函数"
```

---

### Task 2: `import_table` 落库前 forward-fill anchor 列

**Files:**
- Modify: `backend/app/services/knowhow/api.py:194-201`（`import_table` 主体）
- Test: `backend/tests/test_knowhow_api.py`

**Interfaces:**
- Consumes: `forward_fill_column`（Task 1）
- Produces: 导入后底层行的 anchor 列已填满（同概念分支行 anchor 值一致）。

- [ ] **Step 1: 写失败测试**

先看 `backend/tests/test_knowhow_api.py` 现有 import 测试的 fixture/repo 用法（找一个已有 `import_table` 用例，复用其 repo 构造）。追加：

```python
def test_import_table_forward_fills_anchor_column(knowhow_repo):
    # 概念列(第0列)只在首行写一次，后续分支行空——导入后应被填满，
    # 使同概念分支共享 anchor 值（下游归并成一个概念 KO）。
    import io
    from openpyxl import Workbook
    from app.services.knowhow.api import import_table

    wb = Workbook(); ws = wb.active
    ws.append(["违例概念", "根因", "修复"])
    ws.append(["hold&setup", "inst 变化", "换 VT"])
    ws.append(["", "cell delay", "底层走线"])
    ws.append(["", "noise", "提高 victim"])
    buf = io.BytesIO(); wb.save(buf)

    columns_json = '[{"name":"违例概念","kind":"attribute"},{"name":"根因","kind":"procedure"},{"name":"修复","kind":"procedure"}]'
    table_id = import_table(
        knowhow_repo, "nb-1", "t.xlsx", buf.getvalue(),
        "违例表", columns_json, anchor_index=0,
    )

    detail = knowhow_repo.get_knowhow_table(table_id)
    anchor_col_id = detail["columns"][0]["id"]
    anchor_values = [r["cells"].get(anchor_col_id, "") for r in detail["rows"]]
    assert anchor_values == ["hold&setup", "hold&setup", "hold&setup"]
```

> 注：若 `test_knowhow_api.py` 无名为 `knowhow_repo` 的 fixture，改用文件里既有的 repo 构造方式（搜索现有 `import_table(` 用例照抄其 repo 变量）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/hzf/workspace/silicon_notebook && PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_knowhow_api.py -k forward_fills_anchor -q`
Expected: FAIL — anchor_values 后两项为 `""`（未填充）

- [ ] **Step 3: 写实现**

`api.py` 顶部 import 处加入 `forward_fill_column`（与 `parse_grid` 同一 import 行）：

```python
from app.services.knowhow.grid_parser import ParsedGrid, guess_kinds, parse_grid, forward_fill_column
```

把 `import_table` 的行循环（api.py:198-200）改为：

```python
    # 分组型表：anchor 列可能是"只写一次"的分组列（转置/合并型表的
    # 违例概念列），落库前 forward-fill 使同概念分支行共享 anchor 值，
    # 下游 cell-level 投影据此归并成一个概念 KO（见 projection.py）。
    rows = forward_fill_column(grid.rows, anchor_index) if anchor_index is not None else grid.rows
    for row in rows:
        cells = {column_ids[i]: value for i, value in enumerate(row) if value}
        repo.add_knowhow_row(table_id, cells)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/hzf/workspace/silicon_notebook && PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_knowhow_api.py -k forward_fills_anchor -q`
Expected: PASS

- [ ] **Step 5: 跑全量 knowhow 后端测试防回归**

Run: `cd /Users/hzf/workspace/silicon_notebook && PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_knowhow_api.py backend/tests/test_knowhow_grid_parser.py -q`
Expected: 全 PASS

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/knowhow/api.py backend/tests/test_knowhow_api.py
git commit -m "feat(knowhow): import_table 落库前 forward-fill anchor 分组列"
```

---

## Phase 2 — 前端纯逻辑（`knowhow-grouping-logic.ts`）

### Task 3: `groupRowsByAnchor` — 按概念分组

**Files:**
- Create: `frontend/app/knowhow-grouping-logic.ts`
- Create: `frontend/app/knowhow-grouping-logic.test.mjs`

**Interfaces:**
- Consumes: `KnowhowRow`、`KnowhowColumn`（`./knowhow-model.ts`）
- Produces:
  - `interface AnchorGroup { anchorValue: string; rows: KnowhowRow[] }`
  - `groupRowsByAnchor(rows: KnowhowRow[], anchorColumnId: string): AnchorGroup[]` — 按 anchor 值分组；组顺序 = 该值首次出现顺序；组内保持原相对顺序；**空 anchor 值的行各自单行成组**（`anchorValue: ""`），排在其自然位置。

- [ ] **Step 1: 写失败测试**

Create `frontend/app/knowhow-grouping-logic.test.mjs`:

```javascript
import { test } from "node:test";
import assert from "node:assert/strict";
import { groupRowsByAnchor } from "./knowhow-grouping-logic.ts";

const mk = (id, anchorVal, colId = "c0") => ({
  id, position: 0, projectionStatus: "synced", cells: { [colId]: anchorVal },
});

test("groupRowsByAnchor: 同值聚成一组，保持首现顺序", () => {
  const rows = [mk("r1", "A"), mk("r2", "A"), mk("r3", "A")];
  const groups = groupRowsByAnchor(rows, "c0");
  assert.equal(groups.length, 1);
  assert.equal(groups[0].anchorValue, "A");
  assert.deepEqual(groups[0].rows.map((r) => r.id), ["r1", "r2", "r3"]);
});

test("groupRowsByAnchor: 多概念按首现顺序分组", () => {
  const rows = [mk("r1", "A"), mk("r2", "A"), mk("r3", "B")];
  const groups = groupRowsByAnchor(rows, "c0");
  assert.deepEqual(groups.map((g) => g.anchorValue), ["A", "B"]);
  assert.deepEqual(groups[1].rows.map((r) => r.id), ["r3"]);
});

test("groupRowsByAnchor: 不相邻同值也聚到一组（稳定聚合）", () => {
  const rows = [mk("r1", "A"), mk("r2", "B"), mk("r3", "A")];
  const groups = groupRowsByAnchor(rows, "c0");
  assert.deepEqual(groups.map((g) => g.anchorValue), ["A", "B"]);
  assert.deepEqual(groups[0].rows.map((r) => r.id), ["r1", "r3"]);
});

test("groupRowsByAnchor: 空 anchor 行各自单行成组", () => {
  const rows = [mk("r1", "A"), mk("r2", ""), mk("r3", "")];
  const groups = groupRowsByAnchor(rows, "c0");
  assert.equal(groups.length, 3);
  assert.deepEqual(groups.map((g) => g.anchorValue), ["A", "", ""]);
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/knowhow-grouping-logic.test.mjs`
Expected: FAIL — 无法解析模块 / `groupRowsByAnchor` 未定义

- [ ] **Step 3: 写实现**

Create `frontend/app/knowhow-grouping-logic.ts`:

```typescript
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/knowhow-grouping-logic.test.mjs`
Expected: PASS（4 tests）

- [ ] **Step 5: 提交**

```bash
git add frontend/app/knowhow-grouping-logic.ts frontend/app/knowhow-grouping-logic.test.mjs
git commit -m "feat(knowhow): groupRowsByAnchor 分组纯逻辑"
```

---

### Task 4: `computeGridSpans` — G2 相邻同值 rowspan 合并

**Files:**
- Modify: `frontend/app/knowhow-grouping-logic.ts`
- Modify: `frontend/app/knowhow-grouping-logic.test.mjs`

**Interfaces:**
- Consumes: `AnchorGroup`（Task 3）、`KnowhowColumn`
- Produces:
  - `interface GridCell { columnId: string; text: string; rowSpan: number }`（`rowSpan>1` 合并起始格；`rowSpan===0` 被上方覆盖、渲染时跳过）
  - `interface GridDisplayRow { row: KnowhowRow; cells: GridCell[] }`
  - `computeGridSpans(groups: AnchorGroup[], columns: KnowhowColumn[]): GridDisplayRow[]` — 组内每列相邻同值合并；跨组不合并。

- [ ] **Step 1: 写失败测试**

追加到 `knowhow-grouping-logic.test.mjs`:

```javascript
import { computeGridSpans } from "./knowhow-grouping-logic.ts";

const col = (id) => ({ id, name: id, role: "attribute", position: 0 });
const rowC = (id, cells) => ({ id, position: 0, projectionStatus: "synced", cells });

test("computeGridSpans: 组内同值列合并成 rowSpan，被覆盖格 rowSpan=0", () => {
  const groups = [{
    anchorValue: "A",
    rows: [
      rowC("r1", { a: "A", b: "shared", c: "x1" }),
      rowC("r2", { a: "A", b: "shared", c: "x2" }),
    ],
  }];
  const cols = [col("a"), col("b"), col("c")];
  const grid = computeGridSpans(groups, cols);
  // 第一行：a 合并(2)、b 合并(2)、c 独立(1)
  assert.deepEqual(grid[0].cells.map((c) => [c.text, c.rowSpan]),
    [["A", 2], ["shared", 2], ["x1", 1]]);
  // 第二行：a/b 被覆盖(0)、c 独立(1)
  assert.deepEqual(grid[1].cells.map((c) => [c.text, c.rowSpan]),
    [["A", 0], ["shared", 0], ["x2", 1]]);
});

test("computeGridSpans: 跨组不合并（即使同值）", () => {
  const groups = [
    { anchorValue: "A", rows: [rowC("r1", { a: "A", b: "pt" })] },
    { anchorValue: "B", rows: [rowC("r2", { a: "B", b: "pt" })] },
  ];
  const grid = computeGridSpans(groups, [col("a"), col("b")]);
  assert.deepEqual(grid[0].cells.map((c) => c.rowSpan), [1, 1]);
  assert.deepEqual(grid[1].cells.map((c) => c.rowSpan), [1, 1]);
});

test("computeGridSpans: 组内某列部分同值只合并相邻段", () => {
  const groups = [{
    anchorValue: "A",
    rows: [
      rowC("r1", { a: "A", tool: "pt" }),
      rowC("r2", { a: "A", tool: "innovus" }),
      rowC("r3", { a: "A", tool: "innovus" }),
    ],
  }];
  const grid = computeGridSpans(groups, [col("a"), col("tool")]);
  assert.deepEqual(grid.map((r) => r.cells[1].rowSpan), [1, 2, 0]);
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/knowhow-grouping-logic.test.mjs`
Expected: FAIL — `computeGridSpans` 未定义

- [ ] **Step 3: 写实现**

追加到 `knowhow-grouping-logic.ts`:

```typescript
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/knowhow-grouping-logic.test.mjs`
Expected: PASS（全部）

- [ ] **Step 5: 提交**

```bash
git add frontend/app/knowhow-grouping-logic.ts frontend/app/knowhow-grouping-logic.test.mjs
git commit -m "feat(knowhow): computeGridSpans — 相邻同值 rowspan 合并"
```

---

### Task 5: `buildConceptMatrix` + `groupCellWriteTargets` — C 抽屉矩阵 & 批量写目标

**Files:**
- Modify: `frontend/app/knowhow-grouping-logic.ts`
- Modify: `frontend/app/knowhow-grouping-logic.test.mjs`

**Interfaces:**
- Consumes: `AnchorGroup`、`KnowhowColumn`
- Produces:
  - `interface MatrixAttrRow { columnId: string; columnName: string; values: string[]; sharedSpan: boolean }`
  - `interface ConceptMatrix { anchorValue: string; branchRowIds: string[]; attrRows: MatrixAttrRow[] }`
  - `buildConceptMatrix(group: AnchorGroup, columns: KnowhowColumn[], anchorColumnId: string): ConceptMatrix` — 属性为行（排除 anchor 列）、分支为列；`values` 与 `branchRowIds` 对齐；`sharedSpan` = 全分支同值（trim）。
  - `groupCellWriteTargets(group: AnchorGroup, columnId: string): string[]` — 合并格改整组时要写的 rowId 列表（= 组内全部行 id）。

- [ ] **Step 1: 写失败测试**

追加到 `knowhow-grouping-logic.test.mjs`:

```javascript
import { buildConceptMatrix, groupCellWriteTargets } from "./knowhow-grouping-logic.ts";

test("buildConceptMatrix: 属性行×分支列，共享属性标记 sharedSpan", () => {
  const group = {
    anchorValue: "hold&setup",
    rows: [
      rowC("r1", { con: "hold&setup", sym: "共享现象", root: "根因1" }),
      rowC("r2", { con: "hold&setup", sym: "共享现象", root: "根因2" }),
    ],
  };
  const cols = [col("con"), { id: "sym", name: "现象", role: "attribute", position: 1 },
    { id: "root", name: "根因", role: "procedure", position: 2 }];
  const m = buildConceptMatrix(group, cols, "con");
  assert.equal(m.anchorValue, "hold&setup");
  assert.deepEqual(m.branchRowIds, ["r1", "r2"]);
  // anchor 列(con)不进属性行
  assert.deepEqual(m.attrRows.map((r) => r.columnId), ["sym", "root"]);
  const sym = m.attrRows.find((r) => r.columnId === "sym");
  assert.deepEqual(sym.values, ["共享现象", "共享现象"]);
  assert.equal(sym.sharedSpan, true);
  const root = m.attrRows.find((r) => r.columnId === "root");
  assert.deepEqual(root.values, ["根因1", "根因2"]);
  assert.equal(root.sharedSpan, false);
});

test("groupCellWriteTargets: 返回组内所有行 id", () => {
  const group = { anchorValue: "A", rows: [rowC("r1", {}), rowC("r2", {}), rowC("r3", {})] };
  assert.deepEqual(groupCellWriteTargets(group, "any"), ["r1", "r2", "r3"]);
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/knowhow-grouping-logic.test.mjs`
Expected: FAIL — 两个新函数未定义

- [ ] **Step 3: 写实现**

追加到 `knowhow-grouping-logic.ts`:

```typescript
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/knowhow-grouping-logic.test.mjs`
Expected: PASS（全部）

- [ ] **Step 5: 提交**

```bash
git add frontend/app/knowhow-grouping-logic.ts frontend/app/knowhow-grouping-logic.test.mjs
git commit -m "feat(knowhow): buildConceptMatrix + groupCellWriteTargets"
```

---

## Phase 3 — 前端渲染：G2 主网格合并矩阵

> **测试方式（本 Phase 全部 task）**：`.tsx` 渲染无组件测试框架，改动靠 **浏览器 verification workflow** 验证（preview_start → read_page/screenshot）。纯逻辑已在 Phase 2 覆盖。用户在 root master 跑 dev server；本 worktree 改动需先 rebase/merge 或在 root 验证——执行时按 `multi-agent-shared-checkout` 惯例：在 root checkout 改+验证或用 preview。

### Task 6: 主网格按 anchor 有无切换 G2 / 平铺

**Files:**
- Modify: `frontend/app/knowhow-panel.tsx`（`KnowhowTableGrid` 的 `<tbody>`，约 2403-2500 区域；import 处加 grouping-logic）

**Interfaces:**
- Consumes: `groupRowsByAnchor`、`computeGridSpans`（Phase 2）、`detail.anchorColumnId`
- Produces: 有 anchor 表渲染合并矩阵；记录型表（无 `anchorColumnId`）走原平铺分支不变。

- [ ] **Step 1: 加 import + 计算分组网格**

在 `knowhow-panel.tsx` import 区加：

```typescript
import { groupRowsByAnchor, computeGridSpans } from "./knowhow-grouping-logic.ts";
```

在 `KnowhowTableGrid` 组件体内、`orderedColumns`/`filteredRows` 之后加：

```typescript
  // 有 anchor 列 → 分组合并矩阵（spec §4.2）；无 anchor（记录型表）→ 原平铺。
  const anchorColumnId = detail?.anchorColumnId ?? null;
  const gridDisplayRows = useMemo(() => {
    if (!anchorColumnId) return null; // 记录型表：走原平铺路径
    const groups = groupRowsByAnchor(filteredRows, anchorColumnId);
    return computeGridSpans(groups, orderedColumns);
  }, [anchorColumnId, filteredRows, orderedColumns]);
```

> 确认 `KnowhowTableDetail` 有 `anchorColumnId` 字段（`knowhow-model.ts`）；若命名不同，用实际字段名。

- [ ] **Step 2: `<tbody>` 分叉渲染**

把现有 `<tbody>` 的 `filteredRows.map(...)` 包一层条件：`gridDisplayRows === null` 时保持**现有平铺渲染**（原样不动）；否则渲染合并矩阵：

```tsx
{gridDisplayRows === null ? (
  /* 记录型表：保持原平铺渲染（现有代码原样保留） */
  filteredRows.map((row) => (/* …现有行渲染… */))
) : (
  gridDisplayRows.map(({ row, cells }) => (
    <tr key={row.id}>
      {cells.map((cell) =>
        cell.rowSpan === 0 ? null : (
          <td
            key={cell.columnId}
            rowSpan={cell.rowSpan > 1 ? cell.rowSpan : undefined}
            className={cell.rowSpan > 1 ? "knowhow-cell-merged" : undefined}
            onClick={() => onOpenCell(row.id, cell.columnId)}
          >
            {cellSummary(cell.text) || (canEdit ? <span className="knowhow-cell-empty">＋</span> : null)}
          </td>
        ),
      )}
      <td>{/* 同步状态：合并组取组首行状态，或每行显示——沿用现有 ProjectionStatusBadge(row) */}</td>
    </tr>
  ))
)}
```

- [ ] **Step 3: 加合并格样式**

在 `knowhow-panel.tsx` 的 `<style jsx global>` 里加：

```css
.knowhow-cell-merged { background: #fafbfd; vertical-align: middle; }
@media (prefers-color-scheme: dark) { .knowhow-cell-merged { background: #20262e; } }
```

- [ ] **Step 4: TypeScript 校验**

Run（在 root，因 worktree 无 node_modules）：
```bash
cp /Users/hzf/workspace/silicon_notebook/.claude/worktrees/knowhow-checkbox-purpose-a6ea81/frontend/app/knowhow-panel.tsx /Users/hzf/workspace/silicon_notebook/frontend/app/knowhow-panel.tsx
cp /Users/hzf/workspace/silicon_notebook/.claude/worktrees/knowhow-checkbox-purpose-a6ea81/frontend/app/knowhow-grouping-logic.ts /Users/hzf/workspace/silicon_notebook/frontend/app/knowhow-grouping-logic.ts
/Users/hzf/workspace/silicon_notebook/frontend/node_modules/.bin/tsc --noEmit --project /Users/hzf/workspace/silicon_notebook/frontend
```
Expected: 无输出（通过）。完成后 `git -C /Users/hzf/workspace/silicon_notebook checkout -- frontend/app` 还原 root。

- [ ] **Step 5: 浏览器验证**

导入用户 `know-how沉淀.xlsx`（先手动 Excel 转置）→ 打开表 → 确认违例概念/现象列 rowspan 合并、根因/修复/工具分支独立。preview_start + screenshot 留证。

- [ ] **Step 6: 提交**

```bash
git add frontend/app/knowhow-panel.tsx
git commit -m "feat(knowhow): 主网格 G2 相邻同值合并矩阵（记录型表回退平铺）"
```

---

## Phase 4 — C 概念矩阵抽屉

### Task 7: `KnowhowMatrixDrawer` 组件

**Files:**
- Create: `frontend/app/knowhow-matrix-drawer.tsx`

**Interfaces:**
- Consumes: `buildConceptMatrix`、`ConceptMatrix`（Phase 2）、`KnowhowMarkdown`（`./knowhow-cell-editor.tsx`）
- Produces: `KnowhowMatrixDrawer` 组件，props `{ anchorValue, group, columns, anchorColumnId, notebookId, apiBase, canEdit, highlightRowId?, onEditCell, onClose }`；属性为行、分支为列，`sharedSpan` 行跨分支合并；`highlightRowId` 对应分支列高亮。

- [ ] **Step 1: 写组件**

Create `frontend/app/knowhow-matrix-drawer.tsx`（关键结构，样式复用 knowhow-panel 既有 kh-modal-*）：

```tsx
"use client";
import { X } from "lucide-react";
import { buildConceptMatrix, type AnchorGroup } from "./knowhow-grouping-logic.ts";
import { KnowhowMarkdown } from "./knowhow-cell-editor.tsx";
import type { KnowhowColumn } from "./knowhow-model.ts";

export function KnowhowMatrixDrawer({
  group, columns, anchorColumnId, notebookId, apiBase, canEdit,
  highlightRowId, onEditCell, onClose,
}: {
  group: AnchorGroup;
  columns: KnowhowColumn[];
  anchorColumnId: string;
  notebookId: string;
  apiBase: string;
  canEdit: boolean;
  highlightRowId?: string | null;
  onEditCell: (rowId: string, columnId: string) => void;
  onClose: () => void;
}) {
  const matrix = buildConceptMatrix(group, columns, anchorColumnId);
  return (
    <div className="kh-modal-overlay" onClick={(e) => { if (e.currentTarget === e.target) onClose(); }}>
      <div className="kh-modal-card kh-matrix-card" role="dialog" aria-modal="true"
           aria-label={`概念 ${matrix.anchorValue}`} onClick={(e) => e.stopPropagation()}>
        <header className="kh-modal-header">
          <div className="kh-modal-header-top">
            <div className="kh-modal-breadcrumb">
              <span className="concept-badge">违例概念</span>
              <span className="kh-modal-row-title">{matrix.anchorValue}</span>
              <span className="kh-modal-sep">·</span>
              <span>{matrix.branchRowIds.length} 个分支</span>
            </div>
            <button type="button" className="icon-button" title="关闭" onClick={onClose}><X size={18} /></button>
          </div>
        </header>
        <div className="kh-modal-body">
          <table className="kh-matrix">
            <thead>
              <tr>
                <th className="kh-matrix-corner"></th>
                {matrix.branchRowIds.map((rid, i) => (
                  <th key={rid} className={rid === highlightRowId ? "kh-matrix-branch--hi" : undefined}>
                    分支 {i + 1}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrix.attrRows.map((attr) => (
                <tr key={attr.columnId}>
                  <td className="kh-matrix-rowhead">{attr.columnName}</td>
                  {attr.sharedSpan ? (
                    <td className="kh-matrix-shared" colSpan={matrix.branchRowIds.length}
                        onClick={canEdit ? () => onEditCell(matrix.branchRowIds[0], attr.columnId) : undefined}>
                      <KnowhowMarkdown md={attr.values[0]} notebookId={notebookId} apiBase={apiBase} />
                    </td>
                  ) : (
                    matrix.branchRowIds.map((rid, i) => (
                      <td key={rid}
                          className={rid === highlightRowId ? "kh-matrix-cell--hi" : undefined}
                          onClick={canEdit ? () => onEditCell(rid, attr.columnId) : undefined}>
                        <KnowhowMarkdown md={attr.values[i] ?? ""} notebookId={notebookId} apiBase={apiBase} />
                      </td>
                    ))
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: 加矩阵样式**

在 `knowhow-panel.tsx` 的 `<style jsx global>` 里加 `.kh-matrix` 一套（`border-collapse`、`.kh-matrix-rowhead` 灰底、`.kh-matrix-shared` 琥珀底、`--hi` 高亮蓝框）。参照 spec §4.3 与 C mockup 配色。

- [ ] **Step 3: TypeScript 校验**

同 Task 6 Step 4 的 cp→tsc→还原流程，纳入 `knowhow-matrix-drawer.tsx`。
Expected: 通过。

- [ ] **Step 4: 提交**

```bash
git add frontend/app/knowhow-matrix-drawer.tsx frontend/app/knowhow-panel.tsx
git commit -m "feat(knowhow): C 概念矩阵抽屉组件"
```

---

### Task 8: 点概念打开矩阵抽屉（接线）

**Files:**
- Modify: `frontend/app/knowhow-panel.tsx`（新增 `openConceptGroup` 状态；合并的概念格 onClick 打开抽屉；渲染 `KnowhowMatrixDrawer`）

**Interfaces:**
- Consumes: `KnowhowMatrixDrawer`（Task 7）、`groupRowsByAnchor`
- Produces: 点主网格概念格（anchor 列的合并格）→ 打开该组矩阵抽屉；抽屉内点格 → 复用现有 `openCellAuto/openCellEdit`。

- [ ] **Step 1: 状态 + 打开逻辑**

在 `KnowhowPanel`（持有 `detail`/`cellModal` 的组件）加：

```typescript
  const [openConceptValue, setOpenConceptValue] = useState<string | null>(null);
  const openConceptGroup = useMemo(() => {
    if (!detail?.anchorColumnId || openConceptValue === null) return null;
    const groups = groupRowsByAnchor(detail.rows, detail.anchorColumnId);
    return groups.find((g) => g.anchorValue === openConceptValue) ?? null;
  }, [detail, openConceptValue]);
```

- [ ] **Step 2: 网格概念格改为打开抽屉**

Task 6 的合并矩阵渲染里，anchor 列的格子（`cell.columnId === anchorColumnId`）onClick 改为 `setOpenConceptValue(cell.text)`，非 anchor 格保持 `onOpenCell`。

- [ ] **Step 3: 渲染抽屉**

在 `KnowhowPanel` 的 cellModal 渲染附近加：

```tsx
{openConceptGroup && detail?.anchorColumnId && (
  <KnowhowMatrixDrawer
    group={openConceptGroup}
    columns={orderColumnsForGrid(detail.columns)}
    anchorColumnId={detail.anchorColumnId}
    notebookId={notebookId}
    apiBase={apiBase}
    canEdit={canEdit}
    onEditCell={(rowId, columnId) => openCellAuto(rowId, columnId)}
    onClose={() => setOpenConceptValue(null)}
  />
)}
```

- [ ] **Step 4: TypeScript 校验 + 浏览器验证**

cp→tsc→还原；preview 导入转置表 → 点"hold和setup打架"概念格 → 确认弹出矩阵抽屉、属性×分支排列、共享现象跨分支合并、点分支格进编辑浮层。screenshot 留证。

- [ ] **Step 5: 提交**

```bash
git add frontend/app/knowhow-panel.tsx
git commit -m "feat(knowhow): 点概念打开矩阵抽屉接线"
```

---

## Phase 5 — 编辑交互

### Task 9: 合并格编辑「影响 N 个分支」提示 + 批量写

**Files:**
- Modify: `frontend/app/knowhow-cell-editor.tsx`（`KnowhowCellEditor` 加可选 prop `affectedBranchCount`，>1 时 header 显示提示）
- Modify: `frontend/app/knowhow-panel.tsx`（保存合并格时用 `groupCellWriteTargets` 批量 `patchKnowhowCell`）

**Interfaces:**
- Consumes: `groupCellWriteTargets`（Phase 2）、`patchKnowhowCell`（`knowhow-model.ts`）
- Produces: 编辑合并的共享格 → 提示影响范围 + 保存写回该组所有行该列。

- [ ] **Step 1: editor 加提示 prop**

`KnowhowCellEditor` props 加 `affectedBranchCount?: number`；在 header breadcrumb 后加：

```tsx
{affectedBranchCount && affectedBranchCount > 1 && (
  <span className="kh-affect-hint">改动将同步到该概念下全部 {affectedBranchCount} 个分支</span>
)}
```

- [ ] **Step 2: panel 批量写**

在 `handleCellSave`（panel 里落库合并回 detail 处）判断：若该 (rowId, columnId) 属于一个合并共享格（同组同列全同值），用 `groupCellWriteTargets(group, columnId)` 拿到所有 rowId，`Promise.all` 批量 `patchKnowhowCell`，再合并回 detail。否则维持单格写。

```typescript
  // 判断是否共享合并格：该行所属概念组内、该列是否全分支同值。
  const group = detail && detail.anchorColumnId
    ? groupRowsByAnchor(detail.rows, detail.anchorColumnId)
        .find((g) => g.rows.some((r) => r.id === rowId))
    : null;
  const targets =
    group && isSharedColumn(group, columnId) // isSharedColumn: 组内该列全同值
      ? groupCellWriteTargets(group, columnId)
      : [rowId];
  await Promise.all(targets.map((rid) => patchKnowhowCell(notebookId, tableId, rid, columnId, contentMd)));
```

> `isSharedColumn(group, columnId)` 加到 `knowhow-grouping-logic.ts`（组内长度>1 且该列全 trim 同值），并补一个 `*.test.mjs` 用例（同 Task 5 模式：全同值 true、有异值 false、单行 false）。

- [ ] **Step 3: TypeScript 校验 + 逻辑测试**

Run: `cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/knowhow-grouping-logic.test.mjs`（含新 `isSharedColumn` 用例）+ cp→tsc→还原。
Expected: PASS + tsc 通过。

- [ ] **Step 4: 浏览器验证**

编辑共享现象格 → 确认提示"影响 N 个分支"→ 保存 → 所有分支的现象同步更新（矩阵抽屉与主网格都变）。screenshot 留证。

- [ ] **Step 5: 提交**

```bash
git add frontend/app/knowhow-cell-editor.tsx frontend/app/knowhow-panel.tsx frontend/app/knowhow-grouping-logic.ts frontend/app/knowhow-grouping-logic.test.mjs
git commit -m "feat(knowhow): 合并格编辑影响范围提示 + 批量写整组"
```

---

### Task 10: 加分支 / 加概念 / 删概念

**Files:**
- Modify: `frontend/app/knowhow-panel.tsx`（矩阵抽屉底部"+ 分支"；主网格底部"+ 违例概念"；概念组"删除整个概念"）
- Modify: `frontend/app/knowhow-matrix-drawer.tsx`（"+ 分支"按钮 → 回调）

**Interfaces:**
- Consumes: `addKnowhowRow`、`deleteKnowhowRow`（`knowhow-model.ts`）、`groupCellWriteTargets`
- Produces: 加分支 = `addKnowhowRow(cells={[anchorColumnId]: anchorValue})`；加概念 = `addKnowhowRow(cells={})` 后进编辑填概念；删概念 = 对组内每行 `deleteKnowhowRow`。

- [ ] **Step 1: 加分支**

矩阵抽屉加 `onAddBranch` prop + 底部按钮；panel 实现：

```typescript
  async function addBranch(anchorValue: string) {
    if (!selectedTableId || !detail?.anchorColumnId) return;
    const newRow = await addKnowhowRow(notebookId, selectedTableId, {
      cells: { [detail.anchorColumnId]: anchorValue },
    });
    setDetail((prev) => prev ? { ...prev, rows: appendRowOptimistically(prev.rows, newRow) } : prev);
    loadDetail(selectedTableId);
  }
```

- [ ] **Step 2: 加概念**

主网格底部"+ 违例概念"按钮 → `addKnowhowRow(cells={})` → 打开该行 anchor 格编辑态让用户填概念名（复用 `openCellEdit`）。

- [ ] **Step 3: 删概念**

概念组（矩阵抽屉 header 或主网格概念格右键/按钮）"删除整个概念"→ 二次确认 → `Promise.all(group.rows.map(r => deleteKnowhowRow(...)))` → 刷新 detail。

- [ ] **Step 4: TypeScript 校验 + 浏览器验证**

cp→tsc→还原；preview 验证：加分支后矩阵多一列空分支；加概念后主网格多一组；删概念后整组消失。screenshot 留证。

- [ ] **Step 5: 提交**

```bash
git add frontend/app/knowhow-panel.tsx frontend/app/knowhow-matrix-drawer.tsx
git commit -m "feat(knowhow): 加分支/加概念/删概念入口"
```

---

## Phase 6 — 引用跳转改目标

### Task 11: ask 引用命中 knowhow → 跳概念矩阵抽屉 + 高亮命中分支

**Files:**
- Modify: `frontend/app/knowhow-panel.tsx`（`initialRowId` 打开逻辑：有 anchor 表时打开该行所属概念的矩阵抽屉 + `highlightRowId`）

**Interfaces:**
- Consumes: `onOpenKnowhowRow(tableId, rowId)`（`answer-panel.tsx:118` 既有回调）、`groupRowsByAnchor`
- Produces: 引用命中的 `rowId` → 定位其概念组 → 打开矩阵抽屉并高亮该分支列。记录型表仍走原行抽屉。

- [ ] **Step 1: 改 initialRowId 打开逻辑**

现有 `initialRowId` 设 `openRowId`（knowhow-panel.tsx:306）。改为：detail 加载后，若有 anchorColumnId，用 `groupRowsByAnchor` 找到含该 rowId 的组，`setOpenConceptValue(group.anchorValue)` + `setHighlightRowId(initialRowId)`；无 anchor（记录型）保持原 `setOpenRowId`。

```typescript
  useEffect(() => {
    if (!initialRowId || !detail) return;
    if (detail.anchorColumnId) {
      const group = groupRowsByAnchor(detail.rows, detail.anchorColumnId)
        .find((g) => g.rows.some((r) => r.id === initialRowId));
      if (group) { setOpenConceptValue(group.anchorValue); setHighlightRowId(initialRowId); }
    } else {
      setOpenRowId(initialRowId); // 记录型表：原行抽屉
    }
  }, [initialRowId, detail]);
```

加 `const [highlightRowId, setHighlightRowId] = useState<string | null>(null);`，传给 `KnowhowMatrixDrawer` 的 `highlightRowId`，抽屉关闭时清空。

- [ ] **Step 2: TypeScript 校验 + 浏览器验证**

cp→tsc→还原；preview：对含 knowhow 表的 notebook 提问 → 答案引用点"在表格中查看"→ 确认打开概念矩阵抽屉且命中分支列高亮。screenshot 留证。

- [ ] **Step 3: 提交**

```bash
git add frontend/app/knowhow-panel.tsx
git commit -m "feat(knowhow): 引用跳转改跳概念矩阵抽屉+高亮命中分支"
```

---

## Phase 7 — 收尾

### Task 12: 全量测试 + rebase + PR

- [ ] **Step 1: 后端全量**

Run: `cd /Users/hzf/workspace/silicon_notebook && PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_knowhow_grid_parser.py backend/tests/test_knowhow_api.py -q`
Expected: 全 PASS

- [ ] **Step 2: 前端逻辑全量 + tsc**

Run: `cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/knowhow-grouping-logic.test.mjs`（+ cp 全部改动文件到 root 跑 tsc，验证后还原 root）
Expected: PASS + tsc 无错

- [ ] **Step 3: rebase + push + PR**

```bash
git fetch origin master
git rebase origin/master
git push -u origin claude/knowhow-anchor-grouping-display
gh pr create --base master --title "feat(knowhow): 转置/合并型表格 anchor 分组显示" --body "见 docs/superpowers/specs/2026-07-16-knowhow-anchor-grouping-display.md"
```

- [ ] **Step 4: 告知用户 merge 后在 root 验证**（用户在 root master 跑服务）

---

## Self-Review

**1. Spec coverage**（逐节对照 spec）：
- §4.1.1 合并 fill → 已交付（`3a8820f`），plan 不含。✓
- §4.1.2 anchor forward-fill → Task 1-2。✓
- §4.2.1 相邻同值合并 → Task 4（`computeGridSpans`）+ Task 6（渲染）。✓
- §4.2.2 同概念排序 + 空概念独立 → Task 3（`groupRowsByAnchor` 空行单独成组、同值聚合）。✓
- §4.2.3 范围（记录型回退）→ Task 6 `anchorColumnId===null` 分支。✓
- §4.3 C 矩阵抽屉 → Task 5（`buildConceptMatrix`）+ Task 7（组件）+ Task 8（接线）。✓
- §4.4 编辑（普通格复用/合并格改整组/拆单个/加删）→ Task 8（普通格复用 openCellAuto）、Task 9（合并格批量写）、Task 10（加删）。拆单个 = 抽屉里改单格（Task 8 onEditCell 走单格）✓
- §4.5 检索召回不动 + 引用跳转改目标 → 召回无 task（不动，符合）；引用 Task 11。✓
- §4.6 文案「分支」→ Task 7/10 UI 用「分支」。✓
- §6 边界（单分支/单概念/空概念/长文本/合并格一致性/改名重投影）→ 单分支&单概念由 `computeGridSpans`/`groupRowsByAnchor` 自然覆盖；空概念 Task 3；合并格一致性 Task 9 批量写；改名走既有重投影（编辑 anchor 格触发，无需新 task）。✓

**2. Placeholder scan**：Task 6/7 的 tsx 渲染给了关键完整骨架 + 明确消费的 logic 函数 + 浏览器验证步骤（非 placeholder，是项目现实的测试方式）；`isSharedColumn`（Task 9）标注了需补函数+测试，非留空。无 TBD/TODO。✓

**3. Type consistency**：`AnchorGroup`/`GridCell`/`GridDisplayRow`/`ConceptMatrix`/`MatrixAttrRow` 跨 task 一致；`groupRowsByAnchor`/`computeGridSpans`/`buildConceptMatrix`/`groupCellWriteTargets`/`isSharedColumn` 签名在定义与消费处一致；`onOpenKnowhowRow(tableId, rowId)` 沿用既有。✓

> 补记：Task 9 引入的 `isSharedColumn(group, columnId)` 已在该 task Step 2 说明需加到 `knowhow-grouping-logic.ts` + 补测试——执行时作为 Task 9 的一部分先写测试再实现。
