# 设计：knowhow 格子编辑器——三态视图（编辑/并列/预览）+ 切格子未保存守卫

- 日期：2026-07-18
- 范围：仅 `KnowhowCellEditor`（格子浮窗**编辑态**）。两件事：
  - **A. 视图三态**：编辑态默认单栏专注写作，预览可切「并列对比」或「铺满预览」；全屏保持正交、用户手动触发。
  - **B. 切格子守卫**：编辑态点「本行其他格子」的兄弟格时，若有未保存改动，走与「关闭」一致的「未保存提醒 + 明确丢弃」守卫，不再静默落草稿/丢字。
- **不在本 spec**：只读预览弹窗 `KnowhowCellPreview`（不动，其 `查看`/全屏/切兄弟格保持现状）；`KnowhowMatrixDrawer`/代码浮层/优化整行弹窗；关闭/取消/背景/Esc 现有守卫语义（已合规，本 spec 只是把「切格子」并入同一套）。

## 背景与动机

现状 `KnowhowCellEditor` 有两处体验问题（均见 [knowhow-cell-editor.tsx](../../../frontend/app/knowhow-cell-editor.tsx)）：

1. **强制双栏**：`.kh-split` 恒为 `grid-template-columns: 1fr 1fr`（[knowhow-cell-editor.tsx:973-993](../../../frontend/app/knowhow-cell-editor.tsx#L973)、[knowhow-panel.tsx:2231](../../../frontend/app/knowhow-panel.tsx#L2231)），编辑器和实时预览永远各占一半，无法专注写作、也无法把预览放大看。

2. **切兄弟格静默丢改动**：点「本行其他格子」的兄弟格走 [`switchCell`](../../../frontend/app/knowhow-panel.tsx#L765)，它**只换 `columnId`、完全不提交后端**，编辑器靠 `key` 重挂载。刚打的字只落进 localStorage 草稿，而草稿是 [300ms 防抖写入](../../../frontend/app/knowhow-cell-editor.tsx#L588)、组件一卸载 `clearTimeout`——**打完立刻切格子，草稿都来不及写、直接丢**；即便写进了，表格那格显示的仍是旧值，用户以为编辑消失，要重开该格点「恢复草稿」才找得回。

用户明确要求：切格子与「关闭」一致——弹未保存提醒、明确丢弃，而非静默落草稿或静默自动提交。

## 架构

纯前端。两件事互相独立、共处编辑态组件：

- **A** 是一个 `viewMode: "edit" | "split" | "preview"` 展示状态（sessionStorage 记忆、默认 `edit`），驱动工具栏一个三选一分段控件 + body 布局类；**全屏（现有 `useFullscreenToggle` + 最大化按钮）不变、与 `viewMode` 正交**——「编辑+全屏=专注写作」「预览+全屏=真·全屏预览」自然组合，不新增全屏概念、不自动进全屏。
- **B** 把编辑态现有的 `closeGuard: boolean` 泛化成 `pendingLeave`（`close` | `switch`），让「切格子」复用同一条「未保存 → 守卫 → 明确丢弃」路径。`knowhow-panel.tsx` 的 `switchCell` **不改**（仍只换列）；守卫完全在 `KnowhowCellEditor` 内部拦在调用 `onSwitchCell` 之前。

无后端改动、无数据流新增。可测纯逻辑（视图态归一化、新增文案常量）落 [knowhow-cell-editor-logic.ts](../../../frontend/app/knowhow-cell-editor-logic.ts)，供 [knowhow-cell-editor.test.mjs](../../../frontend/app/knowhow-cell-editor.test.mjs) 直接 import（组件侧只引用常量、不内联字符串，延续现有 byte-exact 锁定习惯）。

## 组件

### A. 视图三态 + 正交全屏

**1. 状态 `viewMode`（sessionStorage、默认 edit）**
- 仿 [`useFullscreenToggle`](../../../frontend/app/knowhow-cell-editor.tsx#L242) 写一个 `useCellViewMode(): [ViewMode, (m: ViewMode) => void]`，sessionStorage key `knowhow.cellEditor.viewMode`，读到非法值/无值时归一化为 `"edit"`（用纯函数 `normalizeViewMode`）。per-session 记忆（填一张表时保持选定布局），跨会话回落 `edit`。全屏另用其现有 key，两者互不影响。

**2. 分段控件（工具栏右侧，始终可见）**
- 在 `.kh-toolbar` 末尾加一个 `.kh-view-switch` 分段控件：`[✎ 编辑 | ▥ 并列 | 👁 预览]`（lucide `Pencil`/`Columns2`/`Eye`；`Columns2` 若该版本无则退 `SquareSplitHorizontal`），`margin-left: auto` 与录入按钮拉开，选中态高亮，`aria-pressed`/`title` 齐备，对齐精致符合项目 UI 标准。
- `viewMode === "preview"` 时**隐藏录入按钮**（列表/代码/图片/优化表达/上传状态——无可编辑对象），**但分段控件常驻**（否则回不去）；工具栏行始终渲染。procedure 提示 [`kh-procedure-hint`](../../../frontend/app/knowhow-cell-editor.tsx#L912) 同样仅 edit/split 显示。

**3. body 布局按 `viewMode`**
- 复用 `.kh-split`，加修饰类 `kh-split--{viewMode}`，条件渲染两个 pane：
  - `edit`（默认）：只渲染 `.kh-editor-pane`（textarea），单栏铺满 → 专注写作（拖拽/粘贴上传保持挂在编辑 pane 上）。
  - `split`：编辑 + 预览并排（现状双栏）。
  - `preview`：只渲染预览 pane（`KnowhowMarkdown md={content}`——展示的是当前**编辑中**内容，含未保存改动），单栏铺满。
- CSS：`.kh-split--edit`/`.kh-split--preview { grid-template-columns: 1fr }`；`.kh-split--split { grid-template-columns: 1fr 1fr }`；移动端 media query（[knowhow-panel.tsx:2758](../../../frontend/app/knowhow-panel.tsx#L2758)）强制所有变体单栏堆叠。

**4. 全屏（不改）**
- 现有 [`useFullscreenToggle(FULLSCREEN_STORAGE_KEY)`](../../../frontend/app/knowhow-cell-editor.tsx#L535) + header 最大化按钮 + `.kh-modal-card--fullscreen` 铺满视口，保持原样。任一 `viewMode` 下都生效（body flex 自适应）。**默认不全屏、只用户点触**。
- 「优化表达」对照态（`optimizeState.status === "ready"`）仍独占 body（现有 `? 对照 : <>工具栏+split</>` 分支不变），此时工具栏连同分段控件自然不渲染；对照结束后恢复。

### B. 切格子未保存守卫

**1. `closeGuard: boolean` → `pendingLeave: PendingLeave | null`**
- `type PendingLeave = { kind: "close" } | { kind: "switch"; columnId: string }`。
- `handleSwitchCell(columnId)`（新，包住传给 [`KnowhowRowContext`](../../../frontend/app/knowhow-cell-editor.tsx#L877) 的 `onSwitchCell`）：`hasUnsavedChanges(content, savedContent)` 为真 → `setPendingLeave({ kind: "switch", columnId })`；否则直接 `onSwitchCell?.(columnId)`（无改动不打扰）。
- `requestClose()` 调整：`pendingLeave?.kind === "close"` → `onClose()`（保留「再按一次 Esc 强制关闭」）；`pendingLeave?.kind === "switch"`（守卫弹着时按 Esc）→ `setPendingLeave(null)` 取消切换回到编辑；无 pendingLeave 时同现状（未保存→`{kind:"close"}`，否则 `onClose`）。

**2. 守卫 UI（footer，按 kind 取文案）**
- 复用现有 footer 守卫块（[knowhow-cell-editor.tsx:999-1010](../../../frontend/app/knowhow-cell-editor.tsx#L999)），文案按 `pendingLeave.kind` 切：
  - `close`：`CLOSE_GUARD_MESSAGE` / `继续编辑` / `放弃并关闭` → `onClose`（不变）。
  - `switch`：`SWITCH_GUARD_MESSAGE`（新）/ `继续编辑` / `放弃并切换`（新）→ `flushDraft()` 后 `onSwitchCell(columnId)`。
- **`flushDraft()`**：放弃前**同步**把当前 `content` 写进 [`draftStorageKey(rowId, columnId)`](../../../frontend/app/knowhow-cell-editor-logic.ts#L50)，兜住「组件卸载抢在 300ms 防抖前、草稿没写成」的漏洞，兑现守卫文案「草稿已自动保存、可恢复」。close 放弃分支复用同一 helper（顺带修正同一漏洞）。语义与关闭一致：放弃=不立即提交，但草稿保留、重开该格弹恢复条。

**3. 边界**
- 只读预览态兄弟格切换（`KnowhowCellPreview` 的 `onSwitchCell`）不经本组件，**无内容可存、保持直接切**。
- 合并共享格无关：切格子从不提交，不涉及 `handleCellSave` 的批量写；用户选择「继续编辑」后点 `保存/保存并下一格` 才提交（那条路已含批量写，不变）。

## 数据流 / 边界

- **A**：`viewMode`（sessionStorage）→ 分段控件 class + `.kh-split--*` + pane 条件渲染，单向、纯展示。全屏独立状态、独立 key，互不干涉。
- **B**：`pendingLeave` 单个组件内状态；`switchCell`（panel）签名与行为不变，守卫只在其调用前拦截。draft 读写沿用现有 key 与降级（隐私模式 try/catch 静默）。
- **移动端**：≤720px 三态均单栏；分段控件照常（触屏可点）。
- **可访问性**：分段控件 `aria-pressed` 表达选中；守卫按钮延续现有键盘可达。

## 测试

- **纯逻辑单测**（knowhow-cell-editor.test.mjs，node:test）：
  - `normalizeViewMode`：`null`/非法 → `"edit"`；`"split"`/`"preview"`/`"edit"` 原样。
  - 新文案 byte-exact：`SWITCH_GUARD_MESSAGE`、`SWITCH_GUARD_DISCARD_LABEL="放弃并切换"`、三个视图态标签（`编辑`/`并列`/`预览`），延续现有「文案与设计逐字一致」断言风格。
- **tsc** 干净 + 现有前端 `node --test app/*.test.mjs` 全绿（含 knowhow-cell-editor / knowhow-optimize）。
- **视觉/交互验证**（preview 浏览器）：
  - A：开一格默认单栏专注；切「并列」出双栏、切「预览」编辑器隐藏预览铺满；点全屏在三态下分别得到 大编辑器 / 大对照 / 真·全屏预览；刷新同会话保持选定视图、默认不全屏。
  - B：打字后点兄弟格 → 弹「放弃并切换/继续编辑」；「继续编辑」留在原格；「放弃并切换」跳过去且重开原格能「恢复草稿」；无改动点兄弟格直接切、不弹守卫。
- 分段控件对齐精致、选中态清晰，符合项目 UI 对齐标准。

## 风险

- **命名重叠**：编辑器新「预览」视图态 vs 只读弹窗的「查看」态措辞相近。两者上下文不同（一个是编辑内子视图、一个是独立只读弹窗），保留现有「编辑中」header 标签消歧；不改只读弹窗文案。
- **移动端 `--split` 双栏回退**：确保移动 media query 覆盖 `.kh-split--split`（同类名等特异度靠源码序，media 规则须在其后），否则窄屏并列会挤。实现时显式在断点内把三态都压 `1fr`。
- **草稿同步写**：`flushDraft` 直接 `localStorage.setItem`，与 300ms 防抖 effect 并存不冲突（切格子即卸载、effect 已随卸载清理）；隐私模式写失败静默降级（守卫仍关闭/切走，只是这次草稿没留下，行为不比现状差）。
- **sessionStorage 记忆的意外**：同会话记住「预览」后开下一格仍是预览态，可能一时意外；但与全屏记忆同款、且默认 `edit`、可一键切回，收益（填表保持布局）大于风险。

## 考虑过的替代方案

- **切格子自动提交**（初版提案）：点兄弟格即 `handleCellSave` 提交再切。被否——用户要求与关闭一致的「明确丢弃」，不要静默自动提交。守卫「放弃并切换」旁可另加「保存并切换」第三选项，本 spec 暂不加（保持与关闭守卫两选项一致），日后需要再补。
- **一键「专注模式」按钮**（捆绑全屏+关预览）：多一个与现有全屏语义重叠的概念，改用「三态 + 正交全屏」的可组合模型，用户已确认全屏由用户单独触发。
