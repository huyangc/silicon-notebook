// 格子编辑浮窗 — 纯逻辑（无 JSX，可被 knowhow-cell-editor.test.mjs 直接
// import）。knowhow-cell-editor.tsx 含 JSX，Node 原生 TS 类型剥离不支持
// .tsx（仅 .ts/.mts/.cts 可被 node --test 直接 import），故本文件把草稿键/
// 恢复决策、「保存并下一格」的行主序推进（含跨行/末格语义）、textarea 光标
// 插入（工具栏 列表/代码/图片 与图片粘贴/拖拽共用同一套插入原语）这些可测
// 纯逻辑单独抽出，镜像 knowhow-panel.tsx <-> knowhow-panel-logic.ts 的既有
// 拆分方式。knowhow-cell-editor.tsx 只调用本文件导出的函数/常量，不重复
// 实现判断逻辑或复制粘贴文案字符串。

// --- UI 文案常量（规格②路A/我方任务简报逐字对照；组件侧只引用，不内联硬编
// 码字符串，使「byte-exact vs 规格」可以在这里用简单的相等断言锁住）--------------

// 规格②路A 原文：「用有序列表写步骤，系统会识别为可执行步骤」——procedure
// 内容类型（原 identify/root_cause/fix 三角色 2026-07-15 合并为 procedure）
// 列的格子编辑器带的轻提示。
export const PROCEDURE_HINT_TEXT = "用有序列表写步骤，系统会识别为可执行步骤";

// 规格②路A 原文三个底部按钮：「保存并下一格」/「保存」/「取消」。
export const SAVE_LABEL = "保存";
export const SAVE_AND_NEXT_LABEL = "保存并下一格";
export const CANCEL_LABEL = "取消";

// 预览态→编辑态切换按钮（规格⑤「每节「编辑」按钮」同词）。
export const EDIT_LABEL = "编辑";

// 行上下文条展开按钮（我方任务简报原话：「本行其他格子」）。
export const ROW_CONTEXT_TOGGLE_LABEL = "本行其他格子";

// 草稿恢复提示的两个按钮（我方任务简报原话：「恢复/丢弃」）。
export const RESTORE_DRAFT_LABEL = "恢复";
export const DISCARD_DRAFT_LABEL = "丢弃";
export const DRAFT_BANNER_TEXT = "检测到这一格有未保存的草稿";

// Esc/背景关闭前的未保存内容提醒（规格只描述行为「未保存内容提醒」，未给
// 逐字文案，以下为本组件自定的友好中文文案）。
export const CLOSE_GUARD_MESSAGE = "有未保存的修改，确定要关闭吗？（草稿已自动保存，下次打开可恢复）";
export const CLOSE_GUARD_CONTINUE_LABEL = "继续编辑";
export const CLOSE_GUARD_DISCARD_LABEL = "放弃并关闭";

// 轻工具栏三个按钮（规格②路A 原话：「轻工具栏（列表/代码/图片）」）。
export const TOOLBAR_LIST_LABEL = "列表";
export const TOOLBAR_CODE_LABEL = "代码";
export const TOOLBAR_IMAGE_LABEL = "图片";

// --- 草稿键 / 恢复决策 -----------------------------------------------------------

// 草稿存储键：cell id 即 (rowId, columnId) 二元组——两者都是全局唯一的 128
// 位 id（见 surrogate-id-40bit-collision 教训后统一的 _new_id()），组合天然
// 不需要再叠 notebookId/tableId 前缀去消歧。
export function draftStorageKey(rowId: string, columnId: string): string {
  return `kh-cell-draft:${rowId}:${columnId}`;
}

// 是否应展示「检测到草稿，恢复/丢弃」提示：草稿不存在时不提示；草稿存在但
// 与当前已保存内容完全相同时也不提示（没有真正待恢复的差异，视为陈旧草稿，
// 调用方应静默清掉这个 key，而不是打扰用户）。
export function shouldOfferDraftRestore(draft: string | null, savedContent: string): boolean {
  return draft !== null && draft !== savedContent;
}

// 当前编辑内容是否偏离已保存内容——驱动「未保存」判定：自动草稿是否需要写入
// /清理、Esc/背景关闭是否需要弹未保存提醒，都基于这个布尔值。
export function hasUnsavedChanges(content: string, savedContent: string): boolean {
  return content !== savedContent;
}

// --- 行标题兜底 --------------------------------------------------------------

// 无行标题列 / 行标题格本身为空时的展示兜底（规格①「全空则「行 N」」，N 取
// 行在表内从 1 开始的序号——row.position 是后端 0-based 存储位置）。
export function rowFallbackTitle(position: number): string {
  return `行 ${position + 1}`;
}

// --- 行排序 + 「保存并下一格」行主序推进 ------------------------------------------

export function sortRowsByPosition<T extends { position: number }>(rows: T[]): T[] {
  return [...rows].sort((a, b) => a.position - b.position);
}

export type CellCoordinates = { rowId: string; columnId: string };

// 「保存并下一格」的下一格坐标（规格②路A：「行内按列顺序推进，行末跳下一行
// 首格」）：同行内未到最后一列 → 同行下一列；已在本行最后一列 → 跳到下一行
// 第一列（若还有下一行）；已在整张表的最后一格（最后一行的最后一列）→
// 返回 null，调用方据此收尾（保存后关闭，而非继续跳转——这正好覆盖「添加行」
// 引导流程天然终止的情形：新增行总是追加在表尾，填完它的最后一格后自然收尾）。
//
// columnsInOrder 由调用方传入「已排好序」的列数组（组件侧用 knowhow-panel-
// logic.ts 的 orderColumnsForGrid，即网格实际展示的从左到右顺序——用户在
// 屏幕上看到的顺序，"下一格"应与视觉顺序一致，而非裸的 position 排序，两者
// 仅在行标题列不在 position 0 时才会不同）；rowsInOrder 同理由调用方传入
// sortRowsByPosition 排好序的行数组。本函数不做排序，只做纯粹的坐标推进，
// 便于单测直接构造任意顺序数组验证遍历语义。
export function nextCellCoordinates(
  columnsInOrder: { id: string }[],
  rowsInOrder: { id: string }[],
  current: CellCoordinates,
): CellCoordinates | null {
  if (columnsInOrder.length === 0) return null;
  const colIndex = columnsInOrder.findIndex((column) => column.id === current.columnId);
  const rowIndex = rowsInOrder.findIndex((row) => row.id === current.rowId);
  if (colIndex === -1 || rowIndex === -1) return null;
  if (colIndex < columnsInOrder.length - 1) {
    return { rowId: current.rowId, columnId: columnsInOrder[colIndex + 1].id };
  }
  if (rowIndex < rowsInOrder.length - 1) {
    return { rowId: rowsInOrder[rowIndex + 1].id, columnId: columnsInOrder[0].id };
  }
  return null;
}

// --- textarea 光标插入原语（工具栏 列表/代码/图片 与粘贴/拖拽图片共用）------------

export type TextareaSelection = { value: string; start: number; end: number };
export type InsertResult = { value: string; cursor: number };

// 最基础的原语：把 insertText 插入/替换到 [start,end) 处，返回新全文与新光标
// 位置（插入文本末尾）。有选区时相当于「替换选中内容」，无选区(start===end)
// 时相当于「在光标处插入」。
export function insertAtCursor(sel: TextareaSelection, insertText: string): InsertResult {
  const before = sel.value.slice(0, sel.start);
  const after = sel.value.slice(sel.end);
  return { value: before + insertText + after, cursor: before.length + insertText.length };
}

// 列表按钮：在光标处插入一个列表项标记，光标停在标记之后等待输入项内容。若
// 光标不在行首（前面还有非换行字符），先补一个换行再插标记，避免把标记接在
// 已有文字后面变成同一行的乱码；已经在行首（前面是换行或全文为空）则直接
// 插标记，不产生多余空行。ordered=true 用有序列表标记("1. ")，否则用无序
// 列表标记("- ")——组件侧对 procedure 列默认传 true(呼应规格②路A「procedure
// 提示行」建议用有序列表写步骤)。
export function insertListMarker(sel: TextareaSelection, ordered: boolean): InsertResult {
  const before = sel.value.slice(0, sel.start);
  const needsNewline = before.length > 0 && !before.endsWith("\n");
  const marker = ordered ? "1. " : "- ";
  return insertAtCursor(sel, (needsNewline ? "\n" : "") + marker);
}

// 代码按钮：插入一对代码围栏。有选区时把选中内容包进围栏内（光标落在代码块
// 内容之后，围栏之前）；无选区时插入一个空代码块并把光标停在中间空行，等待
// 输入代码。同 insertListMarker，非行首时先补换行再起围栏。
export function insertCodeFence(sel: TextareaSelection): InsertResult {
  const before = sel.value.slice(0, sel.start);
  const needsNewline = before.length > 0 && !before.endsWith("\n");
  const body = sel.value.slice(sel.start, sel.end);
  const opening = (needsNewline ? "\n" : "") + "```\n";
  const snippet = opening + body + "\n```";
  const { value } = insertAtCursor(sel, snippet);
  return { value, cursor: sel.start + opening.length + body.length };
}

// 图片插入（工具栏「图片」按钮选完文件、或粘贴/拖拽图片，上传拿到 asset id
// 后统一走这个原语插入 `![alt](asset://<id>)`）。alt 为空时产出 `![]()`，
// 仍是合法 markdown（渲染态显示空 alt 的图片，机器侧剥图占位退化为「（图示）」
// 无冒号形式，与 knowhow-model.ts 的 cellSummary/stripMarkdownMarks 约定一致）。
export function insertImageMarkdown(sel: TextareaSelection, assetId: string, alt: string): InsertResult {
  return insertAtCursor(sel, `![${alt}](asset://${assetId})`);
}

// 从文件名派生图片 alt 文本：去掉最后一个扩展名、去首尾空白。多个点时只切
// 掉最后一段（"a.b.png" → "a.b"，与 knowhow-import-logic.ts 的
// deriveDefaultTitle 同规则）；不含扩展名/点在开头（隐藏文件式命名）时原样
// 返回整个文件名（trim 后）。
export function deriveAltFromFilename(filename: string): string {
  const name = filename || "";
  const dotIndex = name.lastIndexOf(".");
  const base = dotIndex > 0 ? name.slice(0, dotIndex) : name;
  return base.trim();
}

// 粘贴/拖拽的候选文件里筛出图片：只看 MIME 类型前缀，真正的大小/白名单校验
// 交给后端 AssetService（规格⑦「单图 ≤10MB；mime 白名单」），前端这里只是
// 决定「要不要拦截这次粘贴/拖拽走图片上传流程」的轻量判断。
export function isImageFile(file: { type: string }): boolean {
  return typeof file?.type === "string" && file.type.startsWith("image/");
}

// --- 编辑器视图三态（编辑/并列/预览）--------------------------------------------

// 编辑态浮窗的三种视图：edit=单栏专注写作（默认）、split=编辑+预览并列对比、
// preview=预览铺满（隐藏编辑器）。全屏与本视图态正交（见 knowhow-cell-editor.tsx
// 的 useFullscreenToggle），各用各的 sessionStorage 键、互不影响。
export type CellViewMode = "edit" | "split" | "preview";

// per-session 记忆键（跨会话回落 edit）；与全屏键 FULLSCREEN_STORAGE_KEY 分开。
export const CELL_VIEW_MODE_STORAGE_KEY = "knowhow.cellEditor.viewMode";

// sessionStorage 读到的原始值归一化：非 "split"/"preview" 的一切（null、旧值、
// 脏值）都回落默认 "edit"。抽纯函数便于单测，也保证组件侧默认口径只有一处。
export function normalizeCellViewMode(raw: string | null): CellViewMode {
  return raw === "split" || raw === "preview" ? raw : "edit";
}

// 三态分段控件按钮文案。
export const VIEW_MODE_EDIT_LABEL = "编辑";
export const VIEW_MODE_SPLIT_LABEL = "并列";
export const VIEW_MODE_PREVIEW_LABEL = "预览";

// 切兄弟格的未保存守卫（与关闭守卫 CLOSE_GUARD_* 同款「未保存提醒 + 明确丢弃」，
// 只是离开动作是"切到另一格"而非"关闭"；放弃时草稿同步保留、可恢复）。「继续
// 编辑」复用 CLOSE_GUARD_CONTINUE_LABEL。
export const SWITCH_GUARD_MESSAGE =
  "有未保存的修改，确定要切换到另一格吗？（草稿已自动保存，下次打开可恢复）";
export const SWITCH_GUARD_DISCARD_LABEL = "放弃并切换";
