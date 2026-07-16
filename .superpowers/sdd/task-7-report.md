# Task 7 报告：`KnowhowMatrixDrawer` 组件（C 概念矩阵抽屉）

## 新建了什么

`frontend/app/knowhow-matrix-drawer.tsx`（新文件，166 行）——导出单个组件 `KnowhowMatrixDrawer`：

- Props：`{ group: AnchorGroup; columns: KnowhowColumn[]; anchorColumnId: string; notebookId: string; apiBase: string; canEdit: boolean; highlightRowId?: string | null; onEditCell: (rowId, columnId) => void; onClose: () => void }`（与 brief 一致，**未**加 brief「Interfaces」一行里额外提到的 `anchorValue`——见下方"与 brief 的偏差①"）。
- 用 `buildConceptMatrix(group, columns, anchorColumnId)`（Phase 2 已交付+单测）算出矩阵，渲染：
  - 外壳：`kh-modal-overlay` + `kh-modal-card kh-matrix-card`，`role="dialog"` `aria-modal="true"`，背景点击关闭。
  - Header：徽章 + 概念值（`matrix.anchorValue`）+ 分支数 + 关闭按钮。
  - `<table className="kh-matrix">`：表头一行"分支 N"（`highlightRowId` 命中列加 `kh-matrix-branch--hi`）；每属性一行，行头是列名（`kh-matrix-rowhead`），`sharedSpan` 的属性行渲染成一个跨分支 `colSpan` 格（`kh-matrix-shared`），否则按分支各自渲染（命中 `kh-matrix-cell--hi`）。
  - 格子内容一律经 `KnowhowMarkdown`（`./knowhow-cell-editor.tsx`）渲染。

`frontend/app/knowhow-panel.tsx`（修改，仅新增 CSS，+72 行）——在顶层 `<style jsx global>` 里、`.kh-code-status-error button` 之后、收尾的 `@media (max-width: 720px)` 之前，新增 `.kh-matrix-card` / `.kh-matrix` / `.kh-matrix th,td` / `.kh-matrix thead th` / `.kh-matrix-rowhead` / `.kh-matrix-shared` / `.kh-matrix-branch--hi` / `.kh-matrix-cell--hi` 一整套，未改动本文件任何既有代码。

## tsc 输出（真实，三次独立运行：初次改动后、proofread 后、commit 后各一次）

```
$ cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/knowhow-checkbox-purpose-a6ea81/frontend && ./node_modules/.bin/tsc --noEmit
（无输出）
$ echo "EXIT_CODE=$?"
EXIT_CODE=0
```
三次结果一致：无 stdout/stderr、exit code 0。另用 `tsc --noEmit --listFiles` 确认 `knowhow-matrix-drawer.tsx` 真的被纳入编译图（不是被 include glob 意外漏掉，`grep -c` = 1）。**通过。**

额外零风险健全性检查（非本 task gate 要求）：
```
$ node --test app/knowhow-grouping-logic.test.mjs
# tests 9 / # pass 9 / # fail 0
```
（本 task 未改动该文件，确认消费的 `buildConceptMatrix` 等仍健康。）

ESLint：同 Task 6 的发现，本项目无 `eslint.config.js`，不在工作流内，跳过。

工作目录本身没有 node_modules（已按任务说明用 worktree 内 symlink 到 root 的 `frontend/node_modules`），未走 brief Step 3 字面写的 "cp→tsc→还原" 流程（该流程是 Task 6 brief 写作时假设 worktree 无 node_modules 的产物；Task 6 report 已实测确认 worktree 内可直接跑 tsc，本 task 沿用同一（更简单、无副作用）路径，与任务说明里给的 gate 命令完全一致）。

## CSS 加在哪

`frontend/app/knowhow-panel.tsx` 顶层唯一的 `<style jsx global>` 块内（第 763-2257 行区间），插入点在 `.kh-code-status-error button { ... }`（Task 11 代码附件样式的最后一条规则）之后、收尾的 `@media (max-width: 720px) { .kh-modal-card {...} ... }` 之前——这是本文件既有的"新增 task 专属样式段追加在收尾响应式媒体查询之前"的位置惯例（Task 6 的 `.knowhow-cell-merged`、Task 11 的 `.kh-code-*` 都是这个模式）。**没有**在 `knowhow-matrix-drawer.tsx` 自己开 `<style jsx>`，符合 brief 与既有约定（knowhow-code.tsx 头注释里写明的同一条理由：styled-jsx global 样式注入绑定"声明该标签的组件是否渲染过"，KnowhowPanel 是唯一保证任何时候都已挂载的容器）。

顺带确认：`.kh-matrix-card` 的加宽（`min(1040px, 96vw)`）在收尾媒体查询**之前**声明，故窄屏（≤720px）时仍会被后面 `.kh-modal-card { width: 100vw; ... }`（源码序更靠后、同选择器特异性）覆盖回全屏——不需要额外补一条 `.kh-matrix-card` 的媒体查询规则。

## 改动文件

- `frontend/app/knowhow-matrix-drawer.tsx`（新建）
- `frontend/app/knowhow-panel.tsx`（仅追加 CSS，其余零改动，`git diff` 已核对只有一段 `+` 无任何 `-`）

## 与 brief 给的骨架代码的偏差（均为主动判断，非疏漏，逐条说明理由）

写之前对照了 `docs/superpowers/plans/2026-07-16-knowhow-anchor-grouping.md`（brief 的源头计划文件，Task 7/8 都在里面，比抽出来的 brief 信息更完整）和 spec `docs/superpowers/specs/2026-07-16-knowhow-anchor-grouping-display.md`，以及 Task 6 刚交付、本组件要转置呼应的主网格 G2 代码，据此做了 4 处偏离 brief 字面代码的改动：

1. **去掉 `anchorValue` 单独 prop**：brief 的「Interfaces」一行文字写 props 含 `anchorValue`，但 Step 1 给的代码骨架里函数签名根本没有这个字段（只有 `group` 等 8 个），组件内部靠 `buildConceptMatrix(...).anchorValue` 派生（本质等于 `group.anchorValue`）。这是 plan 文件自身「一句话摘要」与「代码骨架」不一致（我对照了 plan 原文，两处不一致原样存在，不是 brief 抽取时引入的）。字面代码骨架更具体、更可信，且 `anchorValue` 本就能从 `group` 无损派生，没有必要再加一个可能与 `group` 脱钩的独立入参。**按代码骨架实现，未加 `anchorValue` prop。**

2. **header 徽章文案"违例概念"→"概念"，class 从 `concept-badge`→复用既有 `knowhow-status-badge knowhow-status-badge--info`**：
   - 文案：spec 正文（§4.2-§4.4，脱离背景例子的通用描述部分）通篇只说"概念"，"违例概念"只在 spec §1 背景里作为**用户那张具体表的列名**出现一次。硬编码"违例概念"会让其他领域的 anchor 列（如"故障类型""组件"）在抽屉里都显示错误的标签——这正是 `knowhow-model.ts` 里 `CellKind`/`KIND_LABELS` 从旧的时序修复专属五角色词表改成域中立四值词表这一既有决定要避免的倒退（该文件注释原话："五角色只是时序修复域的实例，被用户纠正为域中立行为类型"）。改成域中立的"概念"。
   - class：Step 2 的指令只列了要加 `.kh-matrix` 一套（"border-collapse、rowhead 灰底、shared 琥珀底、--hi 高亮蓝框"），**没有**提到要新加 `.concept-badge` 样式——如果照抄 brief 的 `className="concept-badge"` 但不补样式，这个徽章会是完全无背景/无边框的裸文字，视觉上明显不完整。改用本文件里已经成熟、蓝色语义、专门就是给"信息小标签"用的 `knowhow-status-badge knowhow-status-badge--info`（比如"优化整行"进度徽章就是这么用的），零新增 CSS，不违背 Step 2"只加 .kh-matrix 一套"的字面范围。

3. **格子可点条件从 `canEdit` 改成 `canEdit || 该格有内容`**：brief 骨架里所有格子的 `onClick` 都写死 `canEdit ? handler : undefined`——只读成员（`canEdit=false`）在矩阵抽屉里**任何格子都点不开**，包括已经填了长文本、按 spec §4.3"长文本在矩阵格内可滚动/截断 + 点开看全"应该能查看全文的格子。核对了两处证据都指向这是 brief 骨架的疏漏而非有意为之：
   - 主网格 G2（Task 6，一个 task 之前、同一个 phase 刚交付）对完全同构的问题用的判据是 `clickable = filled || canEdit`（`knowhow-panel.tsx` 约 2497/2537 行），只读成员一样能点开填了内容的格子查看。
   - Task 8（plan 文件里已经写好、我这个 task 不实现但读取来核对接口）的调用点是 `onEditCell={(rowId, columnId) => openCellAuto(rowId, columnId)}`，而 `openCellAuto` 自己的判据是 `if (!content.trim() && !canEdit) return;`——同样是"只读+空内容"才短路，"只读+有内容"照样打开预览态。
   
   若照抄 brief 骨架的 `canEdit ? ... : undefined`，等 Task 8 把 `onEditCell` 接上 `openCellAuto` 后，只读成员在矩阵抽屉里会因为 `<td>` 根本没挂 `onClick`，永远够不到 `openCellAuto` 内部那条"有内容就该显示"的逻辑——功能性 bug，且没有任何后续计划任务会回头改这个文件来修它。改成 `isClickable(text) = canEdit || Boolean(text.trim())`，与 G2/`openCellAuto` 三处判据保持一致。

4. **`kh-modal-overlay` 的背景点击关闭从行内箭头函数改成具名 `handleBackdropClick` 函数**：纯风格对齐，零行为差异——`knowhow-cell-editor.tsx`（2 处）、`knowhow-panel.tsx`（`KnowhowRowDrawer`/optimize modal）里所有同类浮层清一色是"具名函数 + `ReactMouseEvent<HTMLDivElement>` 类型注解"，brief 给的行内箭头函数是这批组件里唯一的例外写法。改成一致写法，未新增/删减任何 prop 或行为。

以上 4 点里，②③是明确的正确性/完整性问题（不改会有真实可观察的坏效果：无样式裸徽章、只读用户点不开有内容的格子），①是消歧 brief 自身的一处矛盾，④是零风险的风格对齐。全部不影响本 task 的 gate（组件类型/props/exports 与 brief 描述的接口形状一致，`tsc` 照常通过）。

## 已知遗留（有意不做，已在组件文件头注释里写明）

**Esc 关闭监听器没有加。** 这不是疏漏，是核实过风险后主动不做的决定：

- 本文件外壳复用的 `kh-modal-*`/`knowhow-drawer` 体系里，**每一个**同类浮层（`KnowhowCellPreview`/`KnowhowCellEditor`/`KnowhowCodeModal`/`KnowhowRowDrawer`/optimize modal/`KnowhowManageModal`×2/import 向导×2，共 8 处）都有 Esc 监听器，缺席会是明显的不一致。
- 但 `KnowhowRowDrawer`（C 抽屉在有 anchor 表里要取代的那个角色）的 Esc 监听器带一个 `cellModalOpen` 短路参数，其注释原话记录了一次真实 bug 修复：抽屉打开时若上层又堆了一个格子浮窗，两个独立的 `window keydown` 监听器会同时响应同一次 Esc，导致一次按键误关两层。
- Task 8 起 `onEditCell` 会接 `openCellAuto`，即点矩阵格子会在本抽屉上层再堆一个格子浮窗——结构与 `KnowhowRowDrawer` 完全一样，会撞上同一个坑。但本组件当前的 props 里没有能表达"上层是否有浮窗"的信号；Task 8 计划里已经写好的调用点（`docs/superpowers/plans/...md:850-861`）也没有传这样的 prop。
- 现在就无条件加 Esc 监听器 = 重新引入这个已经在 `KnowhowRowDrawer` 修过一次的 bug；现在加一个 `cellModalOpen?: boolean` 之类的可选 prop 但没人传 = 等价于无条件加（同样的 bug），只是多了一个看似有防护、实则没生效的假安全感。判断"完全不加，留一段清楚的注释说明为什么不加 + 该怎么补"比这两种半成品都更安全。已在 `knowhow-matrix-drawer.tsx` 文件头注释里详细写明，供 Task 8 实现者一并处理。

## 自审

1. **`buildConceptMatrix` 的输入契约核对**：`columns: KnowhowColumn[]` 直接透传给 `buildConceptMatrix`，该函数内部自己 `.filter((col) => col.id !== anchorColumnId)` 排除 anchor 列——不需要调用方预先过滤，也不需要 `columns` 是否包含 anchor 列做特判，Task 8 传入 `orderColumnsForGrid(detail.columns)`（含 anchor 列在内的全部列，已按 position 排序）能直接работать。
2. **单分支概念（组内只有 1 行）**：`buildConceptMatrix` 对 `values.length > 1` 才判 `sharedSpan`，1 行时恒为 `false`，走 per-branch 分支渲染，实际效果等同"正常单列显示"，不需要在本组件里特判（spec §6"单分支概念…不发生合并，正常单行显示（无特判）"，本组件天然满足）。
3. **`attr.values[0]`/`attr.values[i] ?? ""` 的越界风险**：`group.rows.length` 恒 ≥ 1（`groupRowsByAnchor` 构造时至少 push 一行），`attr.values` 与 `branchRowIds` 同长度且一一对齐，`values[0]` 在 `sharedSpan` 分支永远有值（`sharedSpan` 要求 `length > 1`）；tsconfig 未开 `noUncheckedIndexedAccess`，`values[i]` 类型是 `string` 非 `string | undefined`，`?? ""` 是防御性但非必需，照抄 brief 保留。
4. **`highlightRowId` 类型 `string | null | undefined` 与 `rid: string` 的 `===` 比较**：TS 允许无收窄的跨可空类型 `===` 比较（结果恒是 `boolean`），编译无警告，运行时语义正确（`highlightRowId` 为 `null`/`undefined` 时没有任何 `rid` 会匹配，等价于"不高亮"）。
5. **表格宽度/横向滚动**：`.kh-modal-body` 只显式设了 `overflow-y: auto`，未显式设 `overflow-x`；按 CSS Overflow 规范，`overflow-y` 为非 `visible` 值而 `overflow-x` 仍为初始值 `visible` 时，`overflow-x` 会被强制计算为 `auto`（防止内容在一个轴上溢出可见、另一个轴上却被裁切的错位)——分支数很多、`.kh-matrix-card` 加宽到 1040px 仍不够时，表格会在卡片内横向滚动而不是撑破卡片布局。这是标准化行为，不需要额外补 `overflow-x: auto` 规则，但依赖浏览器正确实现该规范条款，未做真机验证（本 task 明确排除浏览器验证）。

## Concerns

1. **4 处 brief 偏离需要 controller/reviewer 复核**（详见上方"与 brief 骨架代码的偏差"）：其中②③改变了实际渲染结果（徽章 class/文案、格子可点条件），如果 reviewer 期望逐字符复刻 brief 骨架，需要明确是否认可这些改动。
2. **Esc 关闭监听器缺席，且需要 Task 8 一并补**（详见"已知遗留"）：如果 Task 8 的实现者没有注意到文件头注释、直接照抄 plan 文件里 Task 8 Step 3 给的调用点代码，本组件会一直没有 Esc 关闭能力（功能欠缺，但不是错误行为，不会复现 `KnowhowRowDrawer` 那个"一次 Esc 误关两层"的 bug——只是"矩阵抽屉自己按 Esc 没反应"这一项体验缺口）。建议 Task 8 实现时同步加 `cellModalOpen` 等价机制。
3. **未做浏览器验证**（按任务说明，本 task 明确排除，Phase 7 统一做）：`.kh-matrix*` 样式的实际观感（表头对齐、rowspan/colSpan 合并视觉、高亮蓝框在真实数据下是否清晰）没有肉眼验证，包括自审⑤提到的"横向滚动依赖浏览器 overflow 规范隐式行为"这一点。
4. `.superpowers/sdd/` 目录下 `task-7-report.md`（旧版）、`task-8-report.md`、`task-9-report.md`、以及 4 个 `branch-fix-*.md`/`conflict-resolution-report.md` 在本 session 开始时就已经是"工作区已删除、未 commit"的状态（本 task 未触碰这些文件，`git add` 严格限定在 brief 指定的两个文件上，这些删除不会进入本 task 的 commit）——与 Task 6 report 里的观察一致（Task 6 report concern #5 已提前记录过这些文件是历史遗留），本 task 的新报告直接覆盖写入同名的 `task-7-report.md`（Write 工具在这种"文件不存在于磁盘但 git 索引里有"的情况下按"新建"处理，不需要先 Read）。仅供 controller 知悉，未额外处理。

## 报告文件路径

`/Users/hzf/workspace/silicon_notebook/.claude/worktrees/knowhow-checkbox-purpose-a6ea81/.superpowers/sdd/task-7-report.md`

## Fix: 矩阵格子 a11y

Review finding（重要级）：矩阵格子（`sharedSpan` 合并格 + per-branch 格）是裸
`<td onClick={...}>`——没有 `cursor: pointer`、没有 hover 态、不可键盘操作
（无 role/tabIndex/onKeyDown），与同文件既有约定不一致（G2 网格
`knowhow-panel.tsx` 的 `<button type="button">` + `.knowhow-cell-open`
的 `cursor:pointer`/蓝色/hover 下划线，以及 `KnowhowRowDrawer`）——Task 8
接线后键盘/读屏用户无法到达或激活任何矩阵格子，鼠标用户也看不出格子可点。

### 改了什么

`frontend/app/knowhow-matrix-drawer.tsx`：

- import 里补 `type KeyboardEvent as ReactKeyboardEvent`。
- 在 `isClickable` 之后新增 helper `clickableCellProps(clickable, rowId,
  columnId)`：`clickable` 为假时返回 `{}`（不挂任何属性，保持"不可点=不出现
  在 tab 顺序里"）；为真时返回
  `{ role: "button", tabIndex: 0, onClick: activate, onKeyDown }`，
  `onKeyDown` 在按下 `Enter`/`" "`/`"Spacebar"` 时 `preventDefault()` +
  调用同一个 `activate` 闭包——鼠标 `onClick` 与键盘 `onKeyDown` 共用同一个
  `(rowId, columnId)` 落点，不再各写一遍该格的坐标。
- 两处 `<td>`（`kh-matrix-shared` 合并格、per-branch 格）的
  `onClick={isClickable(...) ? () => onEditCell(...) : undefined}` 替换成
  `{...clickableCellProps(isClickable(...), rowId, columnId)}`——点击门控
  逻辑（`canEdit || filled`）完全不变，只是把"要不要挂交互属性"的判断从只
  覆盖 `onClick` 扩展到同时覆盖 `role`/`tabIndex`/`onKeyDown`。
- 没有用 `<button>` 包裹格子内容：格子内容是 `KnowhowMarkdown`（可能渲染
  `<a>`/`<img>`/`<p>` 等块级/交互元素），`<button>` 内嵌这些是非法 HTML
  嵌套；改为让可点的 `<td>` 自身成为 `role="button"` 的可聚焦控件。

`frontend/app/knowhow-panel.tsx`（`.kh-matrix` CSS 段，仍在顶层
`<style jsx global>` 内，未新开样式容器）：

- 在 `.kh-matrix th, .kh-matrix td` 基础规则之后新增
  `.kh-matrix [role="button"] { cursor: pointer; }`（两种可点格子共用）+
  `.kh-matrix td[role="button"]:not(.kh-matrix-shared):hover { background:
  #f4f7ff; }`（per-branch 格 hover，色值复用既有
  `.knowhow-grid-table tbody tr:hover td` 的浅蓝底语义，限定单格而非整行）。
- 在 `.kh-matrix-shared` 规则之后新增
  `.kh-matrix-shared[role="button"]:hover { background: #f0dab3; }`（共享格
  hover 保持琥珀色系而非分支格的浅蓝，复用既有
  `.knowhow-status-badge--warning` 一套的边框色 `#f0dab3` 做加深一档的
  hover 底，不新开色值）。两条 hover 规则用 `:not(.kh-matrix-shared)` /
  `.kh-matrix-shared` 做互斥选择器分管，不依赖谁的 CSS 特异性/源码序压过谁。
- 不可点的格子（`isClickable` 为假、没有 `role="button"`）不受任何新规则
  影响——所有新选择器都以 `[role="button"]` 为必要条件。

### 未改动

- 点击门控逻辑（`canEdit || filled`）——原样保留，reviewer 已认可，只是
  同一个判断现在同时驱动 `onClick` 和 `role`/`tabIndex`/`onKeyDown`。
- 本 task 之前已被 reviewer 认可的其余 3 处偏离（去掉 `anchorValue` prop、
  badge 文案"概念"/`knowhow-status-badge--info`、无 Esc 监听器）——未触碰。

### tsc 输出（真实）

```
$ cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/knowhow-checkbox-purpose-a6ea81/frontend && ./node_modules/.bin/tsc --noEmit
（无输出）
$ echo "EXIT_CODE=$?"
EXIT_CODE=0
```

无浏览器验证（按任务说明明确排除）。
