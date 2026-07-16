# Task 10 报告：加分支 / 加概念 / 删概念

## 实现了什么

`frontend/app/knowhow-matrix-drawer.tsx`（`KnowhowMatrixDrawer`）新增 7 个可选 prop：
- `onAddBranch?: () => void` + `addingBranch?: boolean` —— footer「+ 分支」按钮（`canEdit && onAddBranch` 时渲染，`.kh-modal-footer`/`.kh-footer-actions`，disabled 期间文案换「添加中…」）。
- `confirmDeleteConcept?/onRequestDeleteConcept?/onCancelDeleteConcept?/onConfirmDeleteConcept?/deletingConcept?` —— header 右上「删除整个概念」入口，五个 prop 的形状**逐字镜像** `KnowhowTableGrid` 表级 `confirmDelete/onRequestDelete/onCancelDelete/onConfirmDelete/deleting`，复用同一套 `.knowhow-confirm/-yes/-no` 内联确认样式（只换文案为「删除整个概念？其下 N 个分支将一并删除」）。原本裸露的关闭按钮现在和这个新按钮一起包进新增的 `.kh-modal-header-actions`（这个类此前已在 `knowhow-code.tsx`/`knowhow-cell-editor.tsx` 用过，本文件之前只有一个按钮所以没用到）。
- 新增 `Plus`、`Trash2` 两个 lucide 图标导入（原来只有 `X`）。

`frontend/app/knowhow-panel.tsx`：
- 新增 import `deleteKnowhowRow`（`knowhow-model.ts` 里已有导出，之前没人消费）。
- 新增状态 `confirmDeleteConcept`/`deletingConcept`（镜像表级 `confirmDelete`/`deleting`），加一个 `useEffect` 在 `openConceptValue` 变化（切概念/关抽屉）时统一重置两者——不然上一个概念点了「删除」没确认就关抽屉，下次开（同一个或另一个概念）抽屉会带着确认态弹出来。
- 新增三个函数：`addBranch(anchorValue)`、`addConcept()`、`deleteConcept()`（细节见下方"落库+刷新路径"）。
- `KnowhowTableGrid` 新增 `onAddConcept: () => void` prop，在 `.knowhow-grid-scroll` 之后（表格之后、类型判断的 `<>` fragment 内）渲染「+ 概念」按钮（`canEdit && anchorColumnId` 时），复用 `sort-button knowhow-reproject-button`（与「添加行」同一套类，零新 CSS）。同时把 0 行空表的引导文案按 `anchorColumnId` 分支：有 anchor 时提示"点下方「概念」"，无 anchor（记录型表）保持原文案"点上方「添加行」"不变。
- `KnowhowMatrixDrawer` 调用点接上全部 7 个新 prop：`onAddBranch={() => addBranch(openConceptGroup.anchorValue)}`、`addingBranch={addingRow}`、以及 5 个删概念 prop。

## tsc 输出（真实）

```
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/knowhow-checkbox-purpose-a6ea81/frontend && ./node_modules/.bin/tsc --noEmit
```
无输出，exit code 0。（另外单独用一个最小 repro 文件验证过"函数声明里引用比它晚声明的同作用域 const"在 TS 里不报 `used before declaration`——`deleteConcept` 定义在 `openConceptGroup`（`useMemo`）之前但引用了它，这是合法的闭包写法，tsc 确认无报错。）

未跑 `node --test`（gate 未要求；本 task 没有新增/改动任何 `*-logic.ts` 纯函数，`knowhow-grouping-logic.ts`/`knowhow-panel-logic.ts` 分毫未动，现有测试不受影响）。未做浏览器验证（Phase 7，brief 明确排除）。

## 三个操作的落库 + 刷新路径

**加分支**（`addBranch(anchorValue)`，由抽屉 footer「+ 分支」触发，`anchorValue` = 当前打开概念的 `openConceptGroup.anchorValue`）：
1. 守卫：`selectedTableId`/`detail.anchorColumnId` 存在且当前没有其它「新增行」在途（复用 `addingRow`，见下方"复用"一节）。
2. `addKnowhowRow(notebookId, selectedTableId, { cells: { [detail.anchorColumnId]: anchorValue } })`。
3. `setDetail` 用 `appendRowOptimistically` 把新行本地拼进 `detail.rows`（同步生效，不等网络）→ 后台 `loadDetail(selectedTableId)` 校准 + `loadTables()` 刷新表卡片行数。
4. 不主动打开任何格子编辑器，也不关抽屉——`openConceptGroup` 是从 `detail.rows` 按 `anchorValue` 派生的 `useMemo`，新分支落地后自动被同一个概念组捕获，矩阵抽屉原地多一列「分支 N+1」（不需要专门代码去"刷新抽屉"）。
5. 失败：`setActionError(extractErrorMessage(err, "添加分支失败，请重试"))`。

**加概念**（`addConcept()`，由主网格底部「+ 概念」触发）：
1. 守卫同上（不需要 `anchorValue`，因为是新建一个全新的概念）。
2. `addKnowhowRow(notebookId, selectedTableId, { cells: {} })`（空行，不预填 anchor 列）。
3. `appendRowOptimistically` 本地拼接 + `loadDetail` + `loadTables()`。
4. `openCellEdit(newRow.id, detail.anchorColumnId)`——直接打开这一格的编辑态让用户填概念名，复用现有格子浮窗（`KnowhowCellEditor`），不新造任何编辑 UI。
5. 失败：`setActionError(extractErrorMessage(err, "添加概念失败，请重试"))`。

**删概念**（`deleteConcept()`，由抽屉 header「删除整个概念」→ 二次确认→"确认删除"触发）：
1. 守卫：`selectedTableId`/`openConceptGroup` 存在。
2. `Promise.all(openConceptGroup.rows.map((row) => deleteKnowhowRow(notebookId, selectedTableId, row.id)))`——组内每一行并发删除。
3. 全部成功：`setOpenConceptValue(null)` 关抽屉 + `loadDetail(selectedTableId)` 刷新 + `loadTables()` 刷新行数。
4. 任一失败：`setActionError("删除失败，请重试")` + 二次确认态收起（`setConfirmDeleteConcept(false)`）+ `deletingConcept` 复位，抽屉留在原地（不误报"已删除"，不清 detail——已成功删除的那几行下次 `loadDetail` 会照实体现）。

## 如何复用现有模式（而非新造）

1. **`addRow` 模式** → `addBranch`/`addConcept`：三者共享同一个 `addingRow`/`setAddingRow` 状态和 guard（`if (!... || addingRow) return`）+ `setActionError(null)` 起手式 + `try/catch/finally` + `extractErrorMessage` 兜底文案 + `appendRowOptimistically` 乐观拼接 + `loadDetail`/`loadTables` 双重刷新——与 `addRow` 完全同构，只是「加分支」预填 anchor 列、「加概念」目标列从"网格首列"换成"明确的 anchor 列"（两者今天因为 `orderColumnsForGrid` 把 anchor 排首位而结果相同，但显式表达意图更稳）。三者共用一个 loading 态是本 task 的取舍：加分支/加概念/添加行本质都是"新增一行"，同一时刻只会有一路在途，没必要拆三份状态。
2. **表删除 `confirmDelete` 模式** → 删概念：`confirmDeleteConcept`/`onRequestDeleteConcept`/`onCancelDeleteConcept`/`onConfirmDeleteConcept`/`deletingConcept` 五个 prop 的命名和语义逐一对应 `KnowhowTableGrid` 现有的 `confirmDelete`/`onRequestDelete`/`onCancelDelete`/`onConfirmDelete`/`deleting`；JSX 结构（未确认时图标按钮，确认后变成 `.knowhow-confirm` 内联条：文案 + 黄底"确认删除"/白底"取消"）逐字复制表删除那一段，只换了提示文案和图标（`Trash2` 换个 `title`）。失败时的收尾（`setActionError` + 确认态收回 + loading 复位）也和 `confirmDeleteTable` 的 catch 块同构。
3. **样式**：零新 CSS。「+ 分支」「+ 概念」两个按钮直接用既有 `.sort-button`/`.kh-footer-actions button`（footer 场景）/`.knowhow-reproject-button`（网格场景）；删概念确认条用既有 `.knowhow-confirm*`；header 多按钮布局用既有但本文件之前没用到的 `.kh-modal-header-actions`（`knowhow-code.tsx` 已有先例）。

## 改动文件

- `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/knowhow-checkbox-purpose-a6ea81/frontend/app/knowhow-matrix-drawer.tsx`
- `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/knowhow-checkbox-purpose-a6ea81/frontend/app/knowhow-panel.tsx`

## 与 brief/plan 的出入（brief 明确要求"以实际为准并说明"）

1. **文案**：brief 明确要求用域中立「概念」而不是 plan 文档里字面的"+ 违例概念"——已照办，按钮文案是「分支」「概念」，图标（`Plus`）承担视觉上的"+"，不是字面拼一个 "+" 字符到文案里（比照现有「新建表」按钮 `<Plus size={16} /> 新建表` 的既有写法，没有哪个按钮把 "+" 打进文本）。
2. **`addBranch`/`addConcept` 比 brief 给的代码骨架多了 `addingRow` guard 和 `loadTables()`**：brief Step 1 给的 `addBranch` 代码骨架是裸函数（没有 loading guard，没有 try/catch，没有 `loadTables`）。我按 brief Step 2 自己的指示——"加概念就是 addRow 的变体"——把 `addRow` 的健壮性写法（guard/错误捕获/`loadTables`）原样搬到 `addBranch`/`addConcept` 上，理由：(a) 防止连点造出多个空分支/空概念；(b) 加行会改变表的 `rowCount`，`loadTables()` 让表列表卡片上的行数保持准确，这与 `addRow` 现有行为一致；(c) 与仓库里其它写操作函数（`addRow`/`confirmDeleteTable`/`retryReproject`/`handleRenameColumn`）统一走 `actionError` 兜底，不生吞异常。
3. **未实现"删分支"（删除单个物理行/分支）**：spec 文档 §4.4 的编辑交互表里列了"删分支"这一行（"删该物理行"），但 task-10-brief.md 的任务标题、Step 1-3、Interfaces 都只提"加分支/加概念/删概念"三项，通篇没有"删分支"的实现指示，plan 文档里翻查全文也没有把"删分支"分配给任何其它 task。判断这是 plan 里一个尚未认领的遗留缺口，不在本 task 范围内，未实现，仅在此标注供后续排查。
4. **"添加行"工具栏按钮未删除/未对 anchor 表隐藏**：brief 约束只写"记录型表保持现有'添加行'不变"，没有指示要不要在有 anchor 的表上隐藏/替换这个按钮；design spec 里有一句"'添加行'按钮语义化为'加分支/加概念'"暗示可能想替换，但没有落到 task 10 的具体 Step 里。我保守选择**两者并存**——anchor 表上"添加行"（工具栏顶部）和新的"+ 概念"（网格底部）同时存在。理由：(a) brief 没有明确指示移除；(b) 对 anchor 表，"添加行"今天已经等价于"加概念"（`orderColumnsForGrid` 把 anchor 列排首位，"添加行"打开的编辑器目标列已经是 anchor 列），保留它不会产生错误行为，只是入口略有冗余；(c) 移除是有风险的改动（可能有用户习惯或未预见的依赖），不在 brief 明确授权范围内。如果产品意图是"anchor 表上应该隐藏/替换'添加行'"，需要另开一个小改动，本报告标出以便复审判断。
5. **Interfaces 里提到的 `groupCellWriteTargets` 实际未使用**：brief 顶部"Consumes"列了 `groupCellWriteTargets`，但删概念只需要"组内每一行的 id"（`openConceptGroup.rows.map(r => r.id)`），brief 自己 Step 3 给的代码骨架也是直接 `group.rows.map(r => deleteKnowhowRow(...))`，没有经过 `groupCellWriteTargets`。`groupCellWriteTargets(group, columnId)` 的第二个参数是"这一列"，语义是"批量写某一列时要写回哪些行"，套在"删整个概念"（跟列无关）上需要传一个无意义的 `columnId` 参数，反而不清楚。判断 Interfaces 里的这一条是 Task 9 遗留的宽泛列举，未实际调用，按 brief Step 3 的代码骨架实现。

## 自审

- 三个函数的错误路径都不会把 UI 卡死：`addBranch`/`addConcept` 的 `finally` 保证 `addingRow` 一定复位；`deleteConcept` 的 `try`/`catch` 两条路径都显式复位 `deletingConcept`。
- `confirmDeleteConcept`/`deletingConcept` 是 Panel 级状态，跨概念/关抽屉不会残留（新增的 `useEffect` 兜底 + `deleteConcept` 成功/失败路径也各自显式复位，双重保险，不依赖 effect 的严格执行时序）。
- `openConceptGroup.anchorValue`（在 `onAddBranch={() => addBranch(openConceptGroup.anchorValue)}` 里）在 TS 里能正确窄化为非 null——因为这段 JSX 整体包在 `{openConceptGroup && detail?.anchorColumnId && (...)}` 条件里，且 `openConceptGroup` 是 `const`（不会被重新赋值打断窄化），已用最小 repro 单独验证过 tsc 不会报 "used before declaration"（`deleteConcept` 定义早于 `openConceptGroup` 的 `useMemo` 但引用了它，闭包合法）。
- 删概念确认文案里的分支数（`matrix.branchRowIds.length`）与实际会被删除的行数（`openConceptGroup.rows.length`）同源（`matrix.branchRowIds = group.rows.map(r => r.id)`），不会出现"提示 3 个、实删 4 个"的错位。
- 只在 `canEdit` 为真时渲染全部三个新入口（抽屉 footer「+ 分支」、header「删除整个概念」、网格底部「+ 概念」），只读成员看不到任何一个，符合规格⑦。
- "+ 概念"按钮只在 `anchorColumnId` 非空（有 anchor/分组视图）时出现；记录型表（`anchorColumnId === null`）不受影响，"添加行"及其原有 `firstColumnId` 逻辑一行未动。

## Concerns

1. **失败时错误提示在抽屉打开期间不可见**：`addBranch`/`addConcept`/`deleteConcept` 失败都写入 Panel 级 `actionError`，但这个状态只在 `KnowhowTableGrid`（网格视图）里渲染成一条红色横幅——矩阵抽屉是全屏 overlay（z-index 65），挡在网格前面，用户在抽屉开着的时候看不到这条横幅，得先关掉抽屉（或对删概念来说，删除失败时抽屉本来就还开着）才能看到失败原因。这是刻意的取舍：brief 没有要求为这三个新操作新开一套"抽屉内错误提示"UI，复用现成的 `actionError` 管道是最小改动；如果这个体验缺口需要补，值得开一个小 task 把 `actionError`（或它的文本）透传进抽屉渲染一份。
2. **"添加行"与"+ 概念"在 anchor 表上并存**：见上方"出入"第 4 条，这是我在 brief 未明确指示下做出的保守选择，如果产品期望是替换而非并存，需要额外确认。
3. **"删分支"（删单个物理行）未实现**：spec §4.4 提到但没有任何 task 认领，纯属信息记录，不是本 task 的缺陷。

## Fix: 抽屉内错误显示

复审 Important finding：加分支/删概念这两个操作从 `KnowhowMatrixDrawer` 内部触发，失败错误此前只写入 panel 级 `actionError`，而该状态只在 `KnowhowTableGrid` 里渲染成横幅——被抽屉的 `.kh-modal-overlay`（z-index 65）整个盖住，用户在抽屉开着期间看不到任何失败提示（对应上方 Concerns #1）。本 fix 让这两个操作的错误在抽屉内可见。

### 改了什么

**`frontend/app/knowhow-matrix-drawer.tsx`**：
- 新增可选 prop `error?: string | null`（插入在 `onClose` 之后、`onAddBranch` 之前——drawer 级通用关注点，不专属加分支或删概念其中一个）。
- 在 `<header className="kh-modal-header">` 内、`.kh-modal-header-top` 之后新增一行 `{error && <p className="kh-inline-error">{error}</p>}`。放在 header 内（而非 header 与 body 之间当 sibling）是刻意的：`.kh-modal-header` 自带 `padding: 16px 20px` + `flex-direction: column; gap: 8px`，错误文案作为 header 的第二行天然获得正确的左右内边距和与上一行的间距，不需要另开任何 CSS wrapper 或新类——若放在 header 外面当独立 sibling，`<p class="kh-inline-error">` 没有自带 padding，文字会贴着卡片圆角边缘。`.kh-inline-error` 本身零新增——复用 `knowhow-panel.tsx` 顶层 `<style jsx global>` 里已经存在的规则（`color:#ba2d2d; font-size:12.5px; margin:0 auto 0 0;`），`KnowhowCellEditor`/`KnowhowCodeModal` 的 `saveError`/`uploadError`/`deleteError` 已在用同一个类。

**`frontend/app/knowhow-panel.tsx`**：
- 新增 state `conceptDrawerError`（紧邻 `confirmDeleteConcept`/`deletingConcept` 声明处）。**没有直接把既有 `actionError` 透传给抽屉**——考虑过这个更简做法但否决了：`actionError` 是全 panel 共享的单一状态，被改名/重新投影/下载模板/添加行/加概念等一堆无关操作共用，其中任何一个失败后如果用户没点横幅上的关闭就去开一个全新的矩阵抽屉，直接透传会让抽屉莫名其妙显示一条不相关的陈旧错误（例如"重新投影失败，请重试"出现在一个从没重新投影过的抽屉里）。改用独立 state，靠既有的 `useEffect(() => { setConfirmDeleteConcept(false); setDeletingConcept(false); }, [openConceptValue])`（原本就在切概念/关抽屉时复位二次确认态）顺带复位，保证每次打开抽屉都是干净状态。
- `addBranch`/`deleteConcept` 的 catch 分支：同一条错误文案现在双写——`setActionError(...)` 保留（不删除既有行为，关闭抽屉后主网格横幅照常可见），新增 `setConceptDrawerError(...)` 供抽屉在打开期间就地显示。两个函数起手也都加了 `setConceptDrawerError(null)`（与既有的 `setActionError(null)` 并列），让连续重试时上一次的错误不会残留到下一次尝试的显示状态里。
- `<KnowhowMatrixDrawer>` 调用点新增 `error={conceptDrawerError}`。

`addConcept`（主网格「+ 概念」触发，banner 本来就可见）未改动——按 finding 要求保持现状。

### 真实 tsc 输出

```
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/knowhow-checkbox-purpose-a6ea81/frontend && ./node_modules/.bin/tsc --noEmit
```
无输出，退出码 0。

另外顺手跑了 `node --test app/knowhow-panel.test.mjs app/knowhow-cell-editor.test.mjs`（本 fix 未改任何 `*-logic.ts` 纯函数，理论上不影响，实测确认）：80 个测试全部通过，0 失败。未做浏览器验证（本 fix 的 Gate 明确只要求 tsc，不要求浏览器验证）。

### 改动文件

- `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/knowhow-checkbox-purpose-a6ea81/frontend/app/knowhow-matrix-drawer.tsx`
- `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/knowhow-checkbox-purpose-a6ea81/frontend/app/knowhow-panel.tsx`

### 本 fix 范围外

「删分支」（spec §4.4 的单行删）仍是 plan gap，按上游 brief 交代，Phase 7 由 controller 决定，本 fix 未处理。
