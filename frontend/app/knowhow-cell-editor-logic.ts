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
// 措辞是前瞻式（「会留作」）而非过去式（「已自动保存」）：同步落盘发生在用户点
// 「放弃」之后，点之前并没有落盘成功这回事；万一落不进，执行器会改报
// DRAFT_FLUSH_FAILED_MESSAGE 并留在编辑器，不让这句承诺变成空头支票。
export const CLOSE_GUARD_MESSAGE = "有未保存的修改，确定要关闭吗？（放弃后会把未保存内容留作本地草稿，下次打开可恢复）";
export const CLOSE_GUARD_CONTINUE_LABEL = "继续编辑";
export const CLOSE_GUARD_DISCARD_LABEL = "放弃并关闭";

// 轻工具栏三个按钮（规格②路A 原话：「轻工具栏（列表/代码/图片）」）。
export const TOOLBAR_LIST_LABEL = "列表";
export const TOOLBAR_CODE_LABEL = "代码";
export const TOOLBAR_IMAGE_LABEL = "图片";

// 「规整格式」工具栏按钮标签（knowhow-md-normalize Task 8；任务简报原文
// 逐字一致）。与「优化表达」（knowhow-optimize-logic.ts 的
// OPTIMIZE_CELL_BUTTON_LABEL）并存于同一工具栏，语义刻意区分：规整=只改
// 格式（调用 reformat_cell，规则确定性重排，LLM 仅锦上添花且失败也有规则
// 兜底）；优化=改措辞（调用 optimize_cell，纯 LLM 改写）。
export const TOOLBAR_REFORMAT_LABEL = "规整格式";

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
  return insertAtCursor(sel, imageMarkdown(assetId, alt));
}

// 只产出 markdown 片段、不关心插到哪里——上传收尾要把片段插到「落笔时的实时内容」
// 上（而不是上传开始时的快照），那条路径需要拿到片段本身而非「插进某个选区」的结果。
export function imageMarkdown(assetId: string, alt: string): string {
  return `![${alt}](asset://${assetId})`;
}

// 上传落笔位置决策——**生产与测试共用这一个实现**。此前测试是自己手工调
// insertAtCursor 模拟，即便生产代码改回「从上传起始快照整篇写回」也照样全绿，
// 等于没锁住那条回归；把决策收进纯函数后，测试与生产至少共用同一份**决策**。
// （注意仍锁不住「组件确实调了它」——.tsx 在本仓库 node:test 模型里测不到，
// 若有人把调用点改回整篇写回，这些用例依旧会绿。）
//
// 正文在上传期间没变过 → 插回用户当初粘贴的光标处（保住「插在光标处」语义）；
// 变过（恢复草稿、接受建议等）→ 起始偏移已无意义，追加到末尾，绝不拿旧偏移
// 往新正文里劈开一刀。
export function resolveUploadInsertion(
  startSnapshot: string,
  startCaret: number,
  liveValue: string,
  snippet: string,
): InsertResult {
  const at =
    liveValue === startSnapshot ? Math.min(Math.max(startCaret, 0), liveValue.length) : liveValue.length;
  return insertAtCursor({ value: liveValue, start: at, end: at }, snippet);
}

// 中断（AbortController）导致的失败不是"上传失败"——是用户自己点「放弃上传并
// 离开」或组件被卸载时我们主动掐的，报红只会让用户以为出错了。fetch 中断抛的是
// name === "AbortError" 的 DOMException；兜底也认 AbortError 这个 name 的普通
// Error（测试与非标准实现）。
export function isAbortError(err: unknown): boolean {
  return typeof err === "object" && err !== null && (err as { name?: unknown }).name === "AbortError";
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
  "有未保存的修改，确定要切换到另一格吗？（放弃后会把未保存内容留作本地草稿，下次打开可恢复）";
export const SWITCH_GUARD_DISCARD_LABEL = "放弃并切换";

// 草稿同步落盘失败（浏览器存储不可用：隐私模式/配额）时的提示——此时绝不能
// 静默离开，否则守卫承诺的「可恢复」是假的、内容真丢了。改为原地报此错、留在
// 编辑框（内容还在），让用户先自行复制再决定去留。
export const DRAFT_FLUSH_FAILED_MESSAGE =
  "浏览器存储不可用，草稿未能保存；内容仍在编辑框，请先复制。再点一次「关闭/取消」将直接放弃这些内容。";

// --- 离开编辑态的意图与决策（关闭 / 切兄弟格，共用「未保存 → 守卫 → 明确丢弃」）---
//
// 组件里所有「确认离开」路径（Esc/背景/关闭按钮、切兄弟格、守卫的「放弃」按钮）
// 都先经这两个纯函数决出下一步，再交给组件里唯一的执行器 performLeave（先同步
// flushDraft、成功才真正 close/switch）。把决策与副作用分开：决策在此可单测，
// 副作用（localStorage/onClose/onSwitchCell）留在组件。这样「每条离开路径都会
// 先落草稿」由「只有一个执行器」这一结构保证，而不是各分支各写一遍（PR 首版正
// 是footer 分支落了草稿、二次 Esc 强制关闭那条没落，才漏了内容）。

// 一次「带未保存改动的离开」意图：关闭整窗，或切到本行另一格。
export type LeaveIntent = { kind: "close" } | { kind: "switch"; columnId: string };

// 决策的两个出口：next=守卫状态的下一个值（null 表示清除/不显示守卫）；leave=需
// 要立刻执行的离开动作（null 表示暂不离开，只是弹/收守卫）。
export type LeaveDecision = { next: LeaveIntent | null; leave: LeaveIntent | null };

// 关闭请求（Esc / 点背景 / 关闭按钮）：
// - 关闭守卫已弹 → 立刻关闭（第二次 Esc 强制关闭，保留既有习惯；仍经执行器，故
//   会先落草稿——这正是首版漏掉的点）；
// - 切格守卫已弹 → 取消这次切换、回到编辑（不误关整窗）；
// - 有未保存改动 → 弹关闭守卫；
// - 无改动 → 立刻关闭。
export function resolveCloseRequest(pending: LeaveIntent | null, hasChanges: boolean): LeaveDecision {
  if (pending?.kind === "close") return { next: null, leave: { kind: "close" } };
  if (pending?.kind === "switch") return { next: null, leave: null };
  if (hasChanges) return { next: { kind: "close" }, leave: null };
  return { next: null, leave: { kind: "close" } };
}

// 点兄弟格：有未保存改动 → 弹切格守卫、暂不切；无改动 → 立刻切。
export function resolveSwitchRequest(columnId: string, hasChanges: boolean): LeaveDecision {
  if (hasChanges) return { next: { kind: "switch", columnId }, leave: null };
  return { next: null, leave: { kind: "switch", columnId } };
}

// flushDraft 该对 localStorage 做什么（抽纯函数：既定死规则，也可单测——离开
// 执行器现在对「无改动」离开也调 flushDraft，若无脑清旧稿会抢在用户决定前删掉
// 「检测到上次草稿，恢复/丢弃」提示里那份还没恢复的草稿，与自动草稿 effect 的
// showRestoreBanner 守卫同款陷阱）：
// - 有未保存改动 → write（写当前内容）；
// - 无改动但恢复提示还开着 → keep（保住那份待恢复的旧草稿，不碰）；
// - 无改动且没有恢复提示 → remove（清掉可能残留的陈旧草稿）。
export type DraftFlushAction = "write" | "keep" | "remove";
export function draftFlushAction(hasChanges: boolean, restoreBannerOpen: boolean): DraftFlushAction {
  if (hasChanges) return "write";
  return restoreBannerOpen ? "keep" : "remove";
}

// 把 draftFlushAction 决出的动作真正作用到一个 Storage-like 对象上，并如实返回
// 「是否已安全落盘」：write 抛错（配额/隐私模式）→ false，调用方据此不许离开；
// keep/remove 恒真（本就没有要保存的内容）。storage 参数化是为了能用「会抛错的
// 假 storage」在 node:test 里覆盖这条真实副作用路径——组件里的 window.localStorage
// 没法在单测中触发抛错，而这正是「存储失败仍离开」那类 bug 的藏身处。
export interface DraftStorage {
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

// 首次渲染期间**同步**读草稿用（见组件侧注释：不能等 useEffect）。只读、幂等，因此
// 放在 render 里是安全的；清陈旧草稿那一步是副作用，仍留在 effect 里。
// 收「取 storage 的 thunk」而非 storage 值本身：`window.localStorage` 的属性 getter 在
// 受限环境（隐私模式、被禁持久化的 origin）会自己抛 SecurityError。值形态下这次属性
// 访问是在调用方求实参时发生的，落在本函数的 try 之外——render 期一抛就崩掉整个编辑器
// （第 8 轮复审的探针场景）。改收 thunk 后，属性访问也被拉进下面的 try，和 getItem()
// 共用同一道异常边界。
// 无论是 thunk 返回空（SSR：没有 window）还是取值/读取抛错（隐私模式/配额），一律当
// 「没有草稿」——读不到就没有可恢复的东西，也就没有「恢复会整段覆盖掉刚落笔的图片」那条
// 风险；这里只如实回答「读到了什么」，放不放行上传由调用方的门禁决定。
export interface DraftReadStorage {
  getItem(key: string): string | null;
}
export function readCellDraft(getStorage: () => DraftReadStorage | null | undefined, key: string): string | null {
  try {
    const storage = getStorage();
    if (!storage) return null;
    return storage.getItem(key);
  } catch {
    return null;
  }
}
export function applyDraftFlush(
  storage: DraftStorage,
  key: string,
  content: string,
  action: DraftFlushAction,
): boolean {
  if (action === "keep") return true;
  if (action === "write") {
    try {
      storage.setItem(key, content);
      return true;
    } catch {
      return false;
    }
  }
  try {
    storage.removeItem(key);
  } catch {
    /* 清旧稿失败无所谓——本就没有要保存的内容，离开是安全的 */
  }
  return true;
}

// --- ruleNormalize：Excel 习惯排版 → 干净 CommonMark 的确定性规整器（零 LLM）---
//
// 与后端 backend/app/services/knowhow/md_normalize.py 的 rule_normalize 严格
// 同语义——两侧共享同一份 golden fixture（backend/tests/fixtures/
// knowhow_normalize_golden.json，见 knowhow-normalize.test.mjs）做 parity，
// 防止两个语言实现随时间漂移。以下逐函数对应 Python 版：
//   indentDepth       ~ _indent_depth
//   classifyLine      ~ classify_line
//   isRichMarkdown    ~ is_rich_markdown（allow-list 门，rule_normalize 入口即调用）
//   normalizeImpl     ~ _normalize
//   ruleNormalize     ~ rule_normalize（唯一导出，永不抛）
//
// 与 Python 正则/字符串语义有意的差异点见各处注释（Unicode 数字、
// lstrip(俩字符) vs trimStart 等），完整讨论见任务简报/报告。
//
// content_signature / content_invariant（Python 侧的内容不变式校验）与 Python
// 的行式围栏扫描器 `_fence_spans`/`_is_closing_fence` 都是后端专属：它们只在
// 后端「LLM 重排候选是否安全落库」的把关路径上使用，前端没有对应调用点，故
// 不移植——只有 rule_normalize 有 TS 孪生。前端 isRichMarkdown 只需判定某行是
// 否**开启**围栏（FENCE_OPEN_RE），不需要配对闭合，故不移植 isClosingFence。

// 与 Python BULLET_GLYPHS = "•●◦▪‣·" 同一字符集，直接内联进字符类（这些字符
// 在 JS/Python 正则里都不是元字符，无需转义）。
// body 捕获组用 [^\n] 而非裸 `.`：JS 正则 `.` 排除全部 LineTerminator（\n \r
// U+2028 U+2029），Python `.`（无 DOTALL）只排除 \n 一个字符；若沿用裸 `.`，
// body 里混入 U+2028/U+2029 时 (.*)$ 会在 JS 侧因无法跨越该字符触达 $ 而整体
// 匹配失败，该行被错误降级为 prose，而 Python 一侧仍正常识别为列表/标题行——
// 换成 [^\n] 后两侧排除集合完全一致（只排除 \n），恢复 byte-identical parity。
const BULLET_RE = /^([•●◦▪‣·]|[-*+])[ \t]+([^\n]*)$/;
// Python `\d` 在 str 模式正则下默认按 Unicode 十进制数字（Nd 类）匹配，不是
// 单纯 ASCII 0-9；JS 裸 \d 永远只认 ASCII，需要 Unicode 属性转义 \p{Nd} 配
// u 标志才能等价（例如全角数字「１.」两侧都应识别为有序列表 marker）。
const ORDERED_RE = /^([\p{Nd}]+)[.)、][ \t]+([^\n]*)$/u;
const ALPHA_RE = /^([A-Za-z])[.)、][ \t]+([^\n]*)$/;

type LineKind = "bullet" | "ordered" | "alpha" | "prose" | "blank";

interface LineInfo {
  kind: LineKind;
  depth: number; // 缩进层级
  body: string; // marker 之后的正文
  marker: string; // ordered 的数字 / alpha 的字母（其余 kind 为空串）
}

// Python `line.lstrip("\t ")`：只剥前导 TAB / 半角空格这两种字符，不是通用
// 空白剥离——JS 的 trimStart() 会剥掉所有 Unicode 空白（含全角空格等），
// 若行首恰好是那类字符会被过度剥离、导致与 Python 端分类不一致，故手写。
function lstripTabSpace(line: string): string {
  let i = 0;
  while (i < line.length && (line[i] === "\t" || line[i] === " ")) i++;
  return line.slice(i);
}

// Python `str.isspace()` 为真的码点集合——即无参数 `str.strip()` 剥离的正是
// 这些字符，不多不少。与 JS bare String.prototype.trim() 的空白集不同，两处
// 差异都是真实分歧点（逐字符核对自 CPython Unicode 表，共 29 个码点）：
//   - Python 剥、JS trim() 不剥：U+001C-U+001F (FS/GS/RS/US) 与 U+0085 (NEL)
//   - JS trim() 剥、Python 不剥：U+FEFF (BOM)——不在这个集合里，因此下面
//     `pyStrip` 不会剥它，这正是与 bare .trim() 唯一但关键的行为差异之一
// （另一差异方向见上面第一条）。零宽空格 U+200B 同样两侧都不剥，故也不在表
// 内——不要因为“看起来像空白”就加进来。
function isPyWhitespace(code: number): boolean {
  return (
    (code >= 0x09 && code <= 0x0d) || // \t \n \v \f \r
    code === 0x20 || // space
    (code >= 0x1c && code <= 0x1f) || // FS GS RS US
    code === 0x85 || // NEL
    code === 0xa0 || // NBSP
    code === 0x1680 || // Ogham space mark
    (code >= 0x2000 && code <= 0x200a) || // en quad .. hair space
    code === 0x2028 || // line separator
    code === 0x2029 || // paragraph separator
    code === 0x202f || // narrow no-break space
    code === 0x205f || // medium mathematical space
    code === 0x3000 // ideographic space
  );
}

// Python 无参数 `str.strip()` 的等价物：两端剥掉上面这组「Python 认为是空白」
// 的字符。凡是移植代码里对应 Python `.strip()` 调用的站点都必须走这个函数，
// 不能用 bare `.trim()`（两者空白集不同，见上）；`lstripTabSpace()` 是另一个
// 独立、故意更窄的调用（对应 Python `lstrip("\t ")`），不受此影响、不要合并。
function pyStrip(s: string): string {
  let start = 0;
  let end = s.length;
  while (start < end && isPyWhitespace(s.charCodeAt(start))) start++;
  while (end > start && isPyWhitespace(s.charCodeAt(end - 1))) end--;
  return s.slice(start, end);
}

// 每个前导 TAB = 1 层；随后每 2 个前导空格 = 1 层（floor）。
function indentDepth(line: string): number {
  let i = 0;
  let tabs = 0;
  while (i < line.length && line[i] === "\t") {
    tabs += 1;
    i += 1;
  }
  let spaces = 0;
  while (i < line.length && line[i] === " ") {
    spaces += 1;
    i += 1;
  }
  return tabs + Math.floor(spaces / 2);
}

function classifyLine(line: string): LineInfo {
  if (!pyStrip(line)) {
    return { kind: "blank", depth: 0, body: "", marker: "" };
  }
  const depth = indentDepth(line);
  const stripped = lstripTabSpace(line);
  let m = BULLET_RE.exec(stripped);
  if (m) {
    return { kind: "bullet", depth, body: pyStrip(m[2]), marker: "" };
  }
  m = ORDERED_RE.exec(stripped);
  if (m) {
    return { kind: "ordered", depth, body: pyStrip(m[2]), marker: m[1] };
  }
  m = ALPHA_RE.exec(stripped);
  if (m) {
    return { kind: "alpha", depth, body: pyStrip(m[2]), marker: m[1] };
  }
  return { kind: "prose", depth, body: pyStrip(stripped), marker: "" };
}

type PrevKind = "start" | "prose" | "list" | "header";

// 围栏开口检测：某行 stripped 后以「``` 或 ~~~ 至少 3 个」的连续 run 开头。
// isRichMarkdown 的 allow-list 门用它把「以围栏开头的行」判为块级构造、整格
// 拒绝规整（只需判开口，无需配对闭合——见文件头注释：闭合配对是后端
// content_signature 专属，前端不移植 isClosingFence）。围栏开口不能用裸 `.`
// 排除 \n 之外的 LineTerminator 那套坑——这里只匹配前导重复字符本身，不含
// body，无需 [^\n] 那层考量。
const FENCE_OPEN_RE = /^(`{3,}|~{3,})/;

// 编辑器是否处在「有异步在飞」的忙态：保存中 / 图片上传中 / 优化表达请求中 /
// 规整格式请求中。用于门控**发起类**入口（保存、切兄弟格），也是「优化表达」与
// 「规整格式」两个工具栏按钮**互斥**的唯一真源——两者各自的 disabledReason 只看
// 自己的 loading，谁在飞就靠 busy 把对方（及切格/保存）一并置灰，避免两条候选
// 同时在途、两个建议面板互相顶掉（P2 修复：reformat 项曾在一次 rebase graft 中
// 从这里漏掉，规整在飞时优化按钮仍可点）。但刻意不门控**离开类**入口（关闭/Esc/
// 背景/取消）：离开的安全性由下面的 resolveSaveCompletion 陈旧回调守卫来保证，
// 若连离开都禁掉，请求卡住时用户会被关在弹窗里出不来。
export function isEditorBusy(
  saving: boolean,
  uploading: boolean,
  optimizing: boolean,
  reformatting: boolean,
): boolean {
  return saving || uploading || optimizing || reformatting;
}

// 发起「保存」的阻塞判定——刻意**不含** optimizing：优化表达是一次可能卡很久的
// LLM 请求（无超时/AbortController），若它把保存也锁死，用户在此期间敲的内容就
// 存不下去。数据安全优先；代价只是那次优化请求可能白跑（保存成功会关掉浮窗，
// 对照根本来不及展示）。上传中不许保存则是必要的——图片还没插进正文就保存会漏掉它。
export function isSaveBlocked(saving: boolean, uploading: boolean): boolean {
  return saving || uploading;
}

// 保存被上传挡住时的提示（按钮 title + ⌘↩ 无声无响应时的原地反馈）。
export const SAVE_BLOCKED_UPLOADING_HINT = "图片上传中，等上传完成后再保存。";

// 反向：保存在飞时不接新上传（保存收尾会关掉/切走本格，晚返回的上传会插进已卸载
// 的组件——服务端留下孤儿资产、用户这次粘贴无声消失）。
export const SAVE_IN_FLIGHT_UPLOAD_HINT = "正在保存，保存完成后再插入图片。";

// 其它异步（优化表达 / 另一次上传）在飞时同样不接新上传：paste/drop 不经工具栏
// 按钮，绕得过 busy 置灰，必须在入口再挡一次。
export const BUSY_UPLOAD_HINT = "有操作进行中，完成后再插入图片。";

// 「检测到上次草稿：恢复/丢弃」还没决出胜负时，也不接新上传。
// 因为「恢复」是**整段** setContent(draftText)：这期间落笔的图片只存在于当前正文
// 里，而自动草稿此刻是暂停的（否则会抢在用户决定前改写那份待恢复草稿），所以图片
// 引用没有第二份记录——用户一点「恢复」，刚插进来的图就随整段覆盖消失，服务端那条
// 资产再没有任何东西引用它。挡住入口是这里唯一不引入新状态的解法：另一条路（把每张
// 落笔的图同时并进 draftText/草稿键）等于在用户还没决定之前就去改写那份待恢复草稿，
// 正是前几轮复审明令禁止的事。
export const RESTORE_PENDING_UPLOAD_HINT = "请先处理上方的草稿提示（恢复或丢弃）再插入图片。";

// 上传入口的统一门禁：返回该拒绝的理由文案，null=放行。抽纯函数是为了让「哪些状态
// 挡上传、各自给哪句话」可单测——组件里 paste/drop/工具栏三个入口共用它一处。
// 顺序：在飞的异步先说（它们会自己结束，提示是「等一下」），恢复提示后说（它要用户
// 动手，提示是「先去处理」），最后才是笼统的忙态。
export function resolveUploadBlock(
  saving: boolean,
  uploading: boolean,
  optimizing: boolean,
  restorePending: boolean,
): string | null {
  if (saving) return SAVE_IN_FLIGHT_UPLOAD_HINT;
  if (restorePending) return RESTORE_PENDING_UPLOAD_HINT;
  if (uploading || optimizing) return BUSY_UPLOAD_HINT;
  return null;
}

// 上传在飞时不允许「接受」优化建议：接受会整段改写正文，而随后落地的上传要把图片
// 插到正文里，两者会互相覆盖。
export const ACCEPT_BLOCKED_UPLOADING_HINT = "图片上传中，等上传完成后再接受建议。";

// 上传在飞时的离开：**不放行、也不丢弃，而是延后**——记下这次离开意图，等上传
// 落进正文后由收尾自动执行（届时草稿里已含图片引用）。
//
// 为什么不是「警告一次、再点一次强制离开」：那条路会让上传的 continuation 落到
// 已卸载的组件上——图片进不了正文，资产却已在服务端落盘、没有任何东西引用它。
// 之前为救它在浏览器端自造过一套「待完成上传日志 + 认领」协议，但那等于在
// localStorage 上手写一个分布式交接协议（首次 mount 的 effect 顺序、跨标签页
// claim 的非原子读改写删、部分写入失败的丢失+重复），复审连续查出四条竞态。
// 现在改为结构上不可能发生：**上传绝不比编辑器实例活得久**——要么等它完成，
// 要么 abort 掉它（见 DISCARD_UPLOAD_AND_LEAVE_LABEL）。
export function resolveLeaveDuringUpload(uploading: boolean, intent: LeaveIntent): LeaveDecisionDuringUpload {
  return uploading ? { kind: "defer", intent } : { kind: "commit", intent };
}
export type LeaveDecisionDuringUpload = { kind: "defer" | "commit"; intent: LeaveIntent };

// 延后离开期间的提示与出口文案。「放弃上传并离开」走 AbortController 真正取消
// 这次上传：中断在服务端提交之前发生就什么都没建。**残留如实记账**——中断恰好落
// 在服务端提交之后的那个窄窗口里，仍会留下一条没有任何东西引用的资产行。服务端的
// 孤儿资产回收（sweep_orphan_assets）**有机会**收走它，但给不出「终会回收」的保证：
//   · 它是**前沿触发**的，没有周期性扫描——只在该 notebook **任一** knowhow 表投影
//     完成之后（另起后台 job，不占投影的单飞窗口）或某张表被删除时才跑，且按 notebook
//     限流。范围是 notebook 级而非表级：sweep 扫的是整个 notebook 的资产，所以编辑
//     **同 notebook 的别的表**同样能把这条孤儿行收走。但这个 notebook 若从此再无任何
//     knowhow 编辑，它就一直留着。
//   · 宽限期算的是「**持续未被引用**了多久」，不是「上传至今多久」：计时从某次 sweep
//     第一次观察到它无引用起跑、跨多次 sweep 累计；标记则要等**后续某次 sweep 观察到
//     它重新被引用**才清掉（不是引用一回来就立刻清——两次 sweep 之间「恢复引用又再次
//     移除」的话，旧时间戳仍在）。这样护住的是服务端根本看不见的活引用——「图片已
//     粘贴、格子还没保存」和「图片正从一个格子搬到另一个格子」，它们此刻只存在于
//     浏览器草稿里。
// 总之：这条孤儿行不影响用户看到的内容，能否回收取决于这个 notebook 此后还有没有
// knowhow 活动（任一表被投影或被删）。故文案只说「放弃」，不再向用户承诺「会尽量
// 留着」这种在存储不可用时兑现不了的话。
export const LEAVE_WAITING_UPLOAD_HINT = "图片上传中，完成后会自动离开。";
export const DISCARD_UPLOAD_AND_LEAVE_LABEL = "放弃上传并离开";

// 有其它异步在飞导致按钮置灰时的通用提示——置灰必须有对得上的原因，不能灰着却
// 显示「可点」的文案（knowhow-optimize-logic.ts 声明的不变量）。
export const BUSY_HINT = "有操作进行中，请稍候。";

// 优化建议的基线已失效：发起优化的前提是「无未保存改动」，但请求在飞期间
// textarea 并未禁用。若用户此时又敲了字，再点「接受」会用建议覆盖掉这些字（且
// 随后自动草稿把草稿也改写成建议内容，等于本地也没了）。故此时不接受，改为丢弃
// 这条已对不上原文的建议并提示重来——用户的字原样留在编辑框。
export const OPTIMIZE_SUGGESTION_STALE_MESSAGE =
  "编辑内容在优化期间已改动，这条建议已对不上原文，已丢弃；如需优化请保存后重新触发。";

// 保存完成后该做什么——关键是第三个参数 stillMounted：本格编辑器若已卸载（用户
// 保存途中关掉、又打开了别的格子），这次保存的收尾**绝不能**再调 onNavigate/
// onClose，否则会把后来打开的那一格关掉或跳走（其未落盘的输入也一并丢失）。
// 抽成纯函数是为了把这条陈旧回调规则单测锁住。
export type SaveCompletionAction =
  | { kind: "navigate"; rowId: string; columnId: string }
  | { kind: "close" }
  | { kind: "none" };
export function resolveSaveCompletion(
  mode: "save" | "next",
  next: CellCoordinates | null,
  stillMounted: boolean,
): SaveCompletionAction {
  if (!stillMounted) return { kind: "none" };
  if (mode === "next") return next ? { kind: "navigate", rowId: next.rowId, columnId: next.columnId } : { kind: "close" };
  return { kind: "close" };
}

// ---------------------------------------------------------------------------
// 架构性修复：把规整门从 deny-list 翻转成 allow-list（未知结构默认不动）。
//
// 修复前 isRichMarkdown 枚举「危险信号」，任何一行只要不匹配 bullet/ordered/
// alpha 就被当 prose 逐行重排。markdown 的块级结构是开放集合：四轮独立评审各
// 自发现一种它没学过的结构被打散（围栏代码块、无竖线 GFM 表格、列表延续行，
// 以及 deny-list 至今仍漏的 4 空格缩进代码块），而后端的 content_invariant 对
// 「文字没变、结构被毁」结构性失明，兜不住。与其每发现一种就补一个信号，不如
// 翻转判定方向：只有当**每一行**都命中封闭文法时才允许规整，否则整格原样返回。
// 门对未知结构 fail CLOSED，是唯一那道防线。逐函数对应 md_normalize.py 的
// is_rich_markdown / _line_normalizable / _starts_block_construct /
// _is_delim_like，完整文法与动机见那边的文档字符串。
//
// 封闭文法（每行必须是三者之一）：① 空行；② list-marker 行（classifyLine 判为
// bullet/ordered/alpha，复用它，含 Excel tab/空格缩进 marker——但前导空白须过
// markerIndentOk 的 CommonMark 4 空格消歧：含 TAB 或纯空格 <=3 才放行，纯空格 >=4
// 判为缩进代码块整格拒绝）；③ 顶格 prose 行
// （无前导 tab/空格，且不以其它 CommonMark 块级构造开头）。关键性质：任何有前
// 导空白、且本身不是 list marker 的行 -> 整格拒绝，这一条通用地关掉了缩进代码
// 块 / 列表延续行 / 缩进 prose 一整类。列 0 以 `**` 开头（`*` 不跟空白不是
// bullet）仍是允许的 prose，保住对已规整格子的幂等。
// ---------------------------------------------------------------------------

// F1：CommonMark HTML 块起始符 = `<` 后接 字母/`/`/`!`/`?`——`!` 覆盖注释
// `<!--`/声明 `<!DOCTYPE`/`<![CDATA[`，`?` 覆盖处理指令 `<?`。与 Python
// `_HTML_BLOCK_RE = re.compile(r"^<[A-Za-z/!?]")` 同一字符类（`!`/`?`/`/` 在
// JS 正则字符类内均为字面量，`/` 处于 `[...]` 中无需转义、不会终止正则字面量）。
const HTML_BLOCK_RE = /^<[A-Za-z/!?]/;

// 引用式链接定义：对应 Python `_REF_LINK_DEF_RE = re.compile(r"^\[[^\]]*\]:")`——
// allow-list 门只判「行以 [label]: 开头」（label 可空、冒号后不要求内容）即整
// 格拒绝，故是纯前缀匹配，不再叠旧版那层「冒号后 pyStrip 非空」的判定。
const REF_LINK_DEF_RE = /^\[[^\]]*\]:/;

// 整行（pyStrip 去两端空白后）只由 `| : - =` 加空白组成、至少含一个 `-`/`=`、
// 长度 >= 2。一并覆盖 GFM 表格分隔行（`--- | ---`）、setext 下划线（`===`/
// `---`）、以及 `-`/`=` 主题分隔线——纯字母/CJK/数字行（如 `A-B`、`2018`）不落
// 入此集合，仍是允许的 prose。对应 Python `_is_delim_like`。用 for..of 按 code
// point 迭代；集合成员全是单 ASCII，故含非 ASCII 字符时循环提前 return false，
// s.length（UTF-16 单元）与 Python len（code point）在会返回 true 的场景下一致。
const DELIM_LIKE_CHARS = new Set(["|", ":", "-", "=", " ", "\t"]);
function isDelimLike(line: string): boolean {
  const s = pyStrip(line);
  if (s.length < 2) return false;
  for (const ch of s) {
    if (!DELIM_LIKE_CHARS.has(ch)) return false;
  }
  return s.includes("-") || s.includes("=");
}

// 顶格行（已知无前导 tab/空格）是否以「段落以外的」CommonMark 块级构造开头——
// 是则整格拒绝规整。对应 Python `_starts_block_construct`。所有匹配都作用在
// 原始 line 上（调用方已保证列 0），与 Python 站点一致。
function startsBlockConstruct(line: string): boolean {
  if (FENCE_OPEN_RE.test(line)) return true; // 围栏代码块
  const first = line[0];
  if (first === "|" || first === ">" || first === "#") return true; // 竖线表格 / 引用块 / ATX 标题
  if (HTML_BLOCK_RE.test(line)) return true; // HTML 块（< 后接字母/`/`/`!`/`?`——标签/闭合/注释<!--/PI<?/声明<!/CDATA<![）
  if (REF_LINK_DEF_RE.test(line)) return true; // 引用式链接定义 [label]:
  if (isDelimLike(line)) return true; // GFM 分隔行 / setext 下划线 / 主题分隔线
  return false;
}

// CommonMark 的 4 空格消歧，作用在「已被 classifyLine 判为 marker」的行上：该行
// 的前导空白（只可能是 tab/半角空格）可被门接受，当且仅当含至少一个 TAB（Excel
// 指纹：目标语料全是 tab 缩进），或纯空格且个数 <= 3（CommonMark：1-3 个前导空格
// 仍是列表项，3 空格也正是本 emitter 单层嵌套发出的缩进）；纯空格且个数 >= 4 判为
// 缩进代码块 -> 拒绝（fail CLOSED，意图不可辨）。对应 Python `_marker_indent_ok`。
// leading 只含 tab/半角空格（都是单 UTF-16 单元），故 .length 与 Python len 一致。
function markerIndentOk(line: string): boolean {
  const leading = line.slice(0, line.length - lstripTabSpace(line).length);
  if (leading.includes("\t")) return true;
  return leading.length <= 3;
}

// CommonMark 主题分隔线：去掉**所有**空格/tab 后由 >=3 个同一字符组成，且该字符
// 取自 `* - _`。覆盖 `***`/`___`/`---` 及空格分隔变体 `* * *`/`- - -`/`_ _ _`。
// 对应 Python `_is_thematic_break`（Batch F 规则 2）：这类行会被 bullet 正则误判为
// 列表项（`* * *` = `*` + 正文 `* *` -> 规整成 `- * *`、毁掉水平分隔线），符号盲的
// content_invariant 兜不住；既有 isDelimLike 只认 `| : - =`（裸 `---`），本函数推广
// 到空格分隔与 `*`/`_` 形式，两条规则刻意并存、不合并。用 spread 按 code point 迭代
// 与 Python 一致——只有 ch ∈ `* - _`（ASCII）才会走到 every，故 length（此时全 ASCII，
// UTF-16 单元 = code point）与 Python len 结果一致。
const THEMATIC_BREAK_CHARS = new Set(["*", "-", "_"]);
function isThematicBreak(line: string): boolean {
  const stripped = line.replace(/[ \t]/g, "");
  if (stripped.length < 3) return false;
  const ch = stripped[0];
  return THEMATIC_BREAK_CHARS.has(ch) && [...stripped].every((c) => c === ch);
}

// F1：CommonMark 硬换行标记——**非空行** RAW 形态以 >=2 个尾随空格、或单个尾随
// 反斜杠结尾，都是段落**内部**的 <br>（deliberate hard line break）。normalizeImpl
// 会 strip 掉尾随标记（body 走 pyStrip）再把相邻 prose 用空行分段，于是段内硬换行
// 被静默改成**段落断裂**，符号盲的后端 content_invariant 兜不住 -> 门在此 fail
// CLOSED 整格拒绝。真实 Excel 粘贴的尾随空白最多一个空格（语料唯一形态，非硬换行）
// 仍放行；miss vs 腐蚀的代价不对称要求拒绝。对应 Python `_has_hard_break`：调用方
// 保证只对非空行调用（空行——含只有尾随空格的纯空白行——由 lineNormalizable 的
// blank 短路在此之前挡掉）。尾随空格只数半角空格（CommonMark 硬换行不认 tab），
// 逐字符倒数，与 Python rstrip(" ") 一致；反斜杠是单 BMP 字符，endsWith 与 len 计
// 数都无 UTF-16 vs code point 分歧。
function hasHardBreak(line: string): boolean {
  if (line.endsWith("\\")) return true;
  let end = line.length;
  while (end > 0 && line[end - 1] === " ") end--;
  return line.length - end >= 2;
}

// 强调定界符「侧翼」判据的空白检测——CommonMark flanking 把行首/行尾也算空白，调用方
// 用空串 "" 表示边界（本函数只判真实字符，边界由调用方短路成「非 open/close」）。对应
// Python `_flank_is_space`（`str.isspace()`）：复用既有的 isPyWhitespace（逐码点核对自
// CPython Unicode 表），故两侧对「delimiter 两侧是不是空白」结论一致（含全角空格 U+3000）。
// 侧翼字符若是星际字符，line[k] 取到的是代理对的一个半——低/高代理项恒非 isPyWhitespace，
// 判为非空白，与 Python 把该星际码点判为非空白一致（最终布尔等价）。
function flankIsSpace(ch: string): boolean {
  return isPyWhitespace(ch.charCodeAt(0));
}

// F2：CommonMark「词内下划线」判据里的「word 侧」——侧翼是不是字母/数字（含 CJK）。
// 对应 Python `_is_word_flank`（`str.isalnum()`）：`/[\p{L}\p{N}]/u` 匹配 Unicode 字母/
// 数字（ASCII 字母数字 + CJK 表意 + 各语系）。仅 `hasCrossLineEmphasis` 里 `_` 这一个
// 定界符用——两侧都是 word char 的 `_` 是词内字面、整段跳过；`*`/`~` 不受此约束。空串
// （边界）恒 false。侧翼是星际字符时 `line[k]` 取到代理对半（恒 false），与 Python 见完整
// 码点在最终布尔上对齐（星际字母作侧翼在语料里不出现，差分谐波零漂移）。
const WORD_FLANK_RE = /[\p{L}\p{N}]/u;
function isWordFlank(ch: string): boolean {
  return ch !== "" && WORD_FLANK_RE.test(ch);
}

// F1：一行是否以**未闭合**的行内构造收尾（会跨软换行）。对应 Python
// `_ends_with_open_inline_span`。normalizeImpl 注入空行会把该构造拆断，符号盲的后端
// content_invariant 兜不住 -> 门在此 fail CLOSED 整格拒绝。独立追踪三类构造（刻意保守、
// 互不影响；任一在行尾仍开着即拒绝）：
//   1. remark-math `$`：按 **run** 配对（`\$` 跳过——反斜杠转义下一字符）——长度 N 的 `$`-run
//      开一个公式 span，只由后面**同长度**的 run 闭合（`$` 行内、`$$` display，CommonMark-math
//      约定），行尾仍在开着的 `$`-run 内 = 未闭合跨行（刻意也拒绝孤立 `价格 $100`；旧逐字符
//      奇偶把 `$$` 当两 `$`＝偶＝闭合而漏掉 display 形态 `$$x\n+y$$`）；
//   2. 行内代码反引号：N 个反引号 run 由等长 run 闭合、run 内不同长度反引号是字面，
//      行尾仍在 span 内 = 未闭合。反引号侧不做转义处理（保守，只会多拒不会漏拒）；
//   3. 链接/图片文本方括号 `[`/`]`：未转义方括号深度（`[` +1、`]` 归零 floor 0），行尾
//      深度 >0 = 有个 `[label` 跨行没闭合（`[label\ncontinued](url)`）。只认 ASCII `[]`——
//      全角【】是不同字符、不影响（真实语料正是用【】）。
// 强调 `*`/`_` 与 GFM 删除线 `~` 的跨行配对**不在本函数**——它做不到逐行判定（词内 `R*C`
// 的孤立 run 不能因自身拒整格，但两行各一个 run 若成对就必须拒），需要跨整格的配对栈，改由
// hasCrossLineEmphasis（`*`/`_`/`~` 三个独立栈）在 isRichMarkdown 做一次全格扫描（见其文档）。
// 定界符 `$`/`` ` ``/`[`/`]`/`\` 都是单 UTF-16 单元的 ASCII；反斜杠 `i += 2` 即便跳进星际
// 字符的代理对中段也无害——低代理项恒非这些定界符，只会被随后 i+=1 跳过，最终落点与
// Python 按 code point 的 i+=2 一致，故与 Python 的计数在最终布尔上等价（差分谐波已覆盖
// 星际形状、零漂移）。围栏开口行由 startsBlockConstruct 在别处拒绝，本函数不为其特设分支
// （即便也命中反引号未闭合，结论一致、无害）。调用方保证只对非空行调用。
function endsWithOpenInlineSpan(line: string): boolean {
  const n = line.length;
  // 1) 未转义 `$` 的 run 配对（run 长度相等才闭合；`\$` 转义跳过——镜像下面的反引号处理）。
  //    长度 N 的 `$`-run 开一个公式 span，只由后面**同长度**的 run 闭合（`$` 行内、`$$`
  //    display，CommonMark-math 约定）。行尾仍在开着的 `$`-run 内 -> 未闭合、跨行。旧的
  //    逐字符奇偶把 `$$` 当两个 `$`（偶数＝已闭合）漏掉 display 形态 `$$x\n+y$$`。
  let dollarInside = false;
  let dollarOpenLen = 0;
  let i = 0;
  while (i < n) {
    const ch = line[i];
    if (ch === "\\") {
      i += 2; // 反斜杠转义下一字符（含 \$）——跳过被转义的那个
      continue;
    }
    if (ch === "$") {
      let run = 0;
      while (i < n && line[i] === "$") {
        run += 1;
        i += 1;
      }
      if (!dollarInside) {
        dollarInside = true;
        dollarOpenLen = run;
      } else if (run === dollarOpenLen) {
        dollarInside = false;
        dollarOpenLen = 0;
      }
      continue; // run 长度不等 -> span 内字面，保持 inside
    }
    i += 1;
  }
  if (dollarInside) return true;
  // 2) 行内代码反引号 run 配对（run 长度相等才闭合）。
  let inside = false;
  let openLen = 0;
  i = 0;
  while (i < n) {
    if (line[i] === "`") {
      let run = 0;
      while (i < n && line[i] === "`") {
        run += 1;
        i += 1;
      }
      if (!inside) {
        inside = true;
        openLen = run;
      } else if (run === openLen) {
        inside = false;
        openLen = 0;
      }
      continue; // run 长度不等 -> span 内字面，保持 inside
    }
    i += 1;
  }
  if (inside) return true;
  // 3) 链接/图片文本方括号深度（未转义、floor 0）。
  let depth = 0;
  i = 0;
  while (i < n) {
    const ch = line[i];
    if (ch === "\\") {
      i += 2; // 转义 \[ / \] 既不开也不合深度
      continue;
    }
    if (ch === "[") depth += 1;
    else if (ch === "]") depth = Math.max(0, depth - 1);
    i += 1;
  }
  return depth > 0;
}

// F1（review）：一行**任意位置**是否出现「标签形状」的未转义 `<`（`<` 紧跟 ASCII 字母
// 或 `/`——`<span`/`</span`/`<em` …）。命中即整格拒绝。对应 Python
// `_has_midline_inline_html`。startsBlockConstruct 的 HTML_BLOCK_RE 是 `^` 锚定的、只认
// **行首** HTML 块，`前缀 <span>\ncontinued</span>` 这类**行中**起头、跨软换行的行内 HTML
// 漏网——normalizeImpl 注入空行把它拆断、符号盲的后端 content_invariant 兜不住。只认
// 「`<`+字母/`/`」tag 形状：`a < b`（后接空格）、`x<0`/`i<3`（后接数字）仍可规整；`x<y`
// （后接字母）被拒（fail CLOSED，miss vs 腐蚀代价不对称）。`\<` 转义跳过。定界符 `<`/`\`
// 皆单 UTF-16 单元 ASCII；`nxt` 取自 `line[i+1]`，与 Python `line[i+1]` 在最终布尔上对齐
// （星际字符作 nxt 取到代理对半、非 ASCII 字母判否，与 Python 见完整码点一致——都不是
// ASCII 字母）。调用方保证只对非空行调用。
function isAsciiLetter(ch: string): boolean {
  return (ch >= "A" && ch <= "Z") || (ch >= "a" && ch <= "z");
}
function hasMidlineInlineHtml(line: string): boolean {
  const n = line.length;
  let i = 0;
  while (i < n) {
    const ch = line[i];
    if (ch === "\\") {
      i += 2; // 反斜杠转义下一字符：\< 不是标签起头
      continue;
    }
    if (ch === "<") {
      const nxt = i + 1 < n ? line[i + 1] : "";
      if (nxt === "/" || isAsciiLetter(nxt)) return true;
    }
    i += 1;
  }
  return false;
}

// F1/F2：全格（cell-global）强调配对——`*`/`_`/`~` 定界符 run 的 CommonMark-lite 开合，用
// **三个跨整格的栈——每个定界符字符各一个独立栈**（每个 entry 记它所在的行号）判定是否有一
// 对强调**跨两个不同的行**成对。成对且跨行 -> true（isRichMarkdown 据此整格拒绝）。对应
// Python `_has_cross_line_emphasis`。F2：`*`-run 只与 `*` 栈、`_`-run 只与 `_` 栈、`~`-run
// 只与 `~` 栈交互——旧单一无类型栈会让词内 `_`（BOTH run）弹掉一个 `*` opener（CommonMark
// 绝不把 `*` 与 `_` 配对），于是 `*open R_C\nclose*` 里真正的 `*...*` 跨行对被 `_` 偷走、检
// 不出、cell 放行；独立栈彻底规避交错，比单栈严格更精确。
//
// F1（review，GFM 删除线 `~`）：`~` 加入本函数（不在 per-line endsWithOpenInlineSpan 的
// `$`-run 机器里）。remark-gfm 4.0.1（前端 remarkGfm 默认选项）里单个 `~` 就是删除线且跨
// 软换行成对（repl 实测 `~a\nb~`→`<del>`、`foo~bar\nbaz~qux`→`<del>`、孤立 `约~5ns`→无
// `<del>`），只有跨整格配对栈能区分「孤立 `~` 须规整」与「跨行成对 `~` 须拒」。`~` 用与
// `*` **相同**的 BOTH 语义（词内也活跃），不走下面 `_` 的词内字面规则。
//
// 为什么必须全格、而非逐行（F1 动机）：词内乘法 `R*C`（两侧都是词字符）是一个 BOTH run，逐
// 行看它「既能开也能闭」，旧实现判它中性、放行——对孤立 `R*C` 是对的（承重用例）。但
// `这是*跨行\n强调*内容` 的两个 `*` 同样都是 BOTH run，CommonMark 跨软换行配成一对强调；
// normalizeImpl 注入空行拆断它，符号盲的后端 content_invariant（`*` 当标点剥掉）兜不住。只有
// 「跨整格配对、看成对两端是否落在不同行」才能既救 `R*C`、又拒 `这是*跨行\n强调*内容`。
//
// F2（review，词内 `_` 字面）：CommonMark 里两侧都是 word char 的下划线既不能开也不能闭
// （词内下划线是字面，不同于 `*`）。`_outer foo_bar\nclose_` 的真 opener 是行首 `_`、真 closer
// 是行尾 `_`（跨软换行成对）；`foo_bar` 中间的 `_` 是词内字面。旧 BOTH 分支把它当真 closer
// 弹掉行首真 opener，跨行对没登记、cell 放行、注入空行拆断强调。修复：仅 `_` run 两侧都过
// isWordFlank 时整段跳过（不压不弹）；`*`/`~` 保持 BOTH。侧翼是标点/符号（非 word）的 `_`
// 仍走常规逻辑（保守，兜住 `(_a\n b_)` 这类标点侧翼的真跨行对）。
//
// F1（按分隔符**个数**配对）：长度 N 的 run 携带 N 个定界符 unit（旧实现只压/弹一次）。
// `*outer\n*inner**` 里闭合 `**`（长度 2）须弹两个 unit——弹掉行 1 opener（同行对）后再弹行 0
// opener（跨行对）-> 拒绝。每个 run 按 CommonMark-lite 分类（行首/行尾算空白）、按 unit 作用：
// CAN-OPEN-only 压 N 个 unit；CAN-CLOSE-only 弹至多 N（各成一对）；BOTH 栈非空弹至多 N、否
// 则压 N。每弹一个 unit 即比较两端行号：不同 -> 跨行 -> true。
//
// **承重记账**：逐行、按 run 出现先后依次作用到**该字符对应的**全格栈上——一行内自成一对的
// `**bold**` 不会偷更早一行留栈底的悬空 opener；**未配对 leftover 自身不拒绝**（既救 `R*C`
// 孤立 run 永不成对，也让永不成对的悬空 opener 不误拒）。`\*`/`\_`/`\~` 转义定界符跳过。
// 与 Python 孪生逐字符对齐（`*`/`_`/`~`/`\` 皆单 UTF-16 单元 ASCII，反斜杠 i+=2 跳进代理对中段
// 无害——低代理项恒非定界符，随后 i+=1 跳过，最终布尔与 Python 按 code point 等价）。
function hasCrossLineEmphasis(lines: string[]): boolean {
  const starStack: number[] = []; // `*` 未配对 opener 的行号
  const underStack: number[] = []; // `_` 未配对 opener 的行号
  const tildeStack: number[] = []; // `~` 未配对 opener 的行号（三个定界符字符各一个独立栈）
  for (let lineno = 0; lineno < lines.length; lineno++) {
    const line = lines[lineno];
    const n = line.length;
    let i = 0;
    while (i < n) {
      const ch = line[i];
      if (ch === "\\") {
        i += 2; // 转义 \* / \_ / \~ 不是定界符
        continue;
      }
      if (ch === "*" || ch === "_" || ch === "~") {
        const runChar = ch;
        const stack = runChar === "*" ? starStack : runChar === "_" ? underStack : tildeStack; // F2：按分隔符字符分型
        const start = i;
        while (i < n && line[i] === runChar) i += 1;
        const runLen = i - start; // F1（本批）：run 携带的定界符**个数**
        const prevCh = start > 0 ? line[start - 1] : "";
        const nextCh = i < n ? line[i] : "";
        // F2：词内 `_`（且仅 `_`）—— 两侧都是 word char 时既不开也不闭，整段跳过。
        if (runChar === "_" && isWordFlank(prevCh) && isWordFlank(nextCh)) {
          continue;
        }
        const canOpen = nextCh !== "" && !flankIsSpace(nextCh);
        const canClose = prevCh !== "" && !flankIsSpace(prevCh);
        if (canOpen && canClose) {
          // BOTH（词内形状；`*`/`~` 会成对）：栈非空则弹至多 runLen 个 unit（各成一对），否则整段压栈
          if (stack.length > 0) {
            for (let k = 0; k < runLen && stack.length > 0; k++) {
              if (stack.pop() !== lineno) return true;
            }
          } else {
            for (let k = 0; k < runLen; k++) stack.push(lineno);
          }
        } else if (canOpen) {
          // CAN-OPEN-only：压 runLen 个 unit
          for (let k = 0; k < runLen; k++) stack.push(lineno);
        } else if (canClose) {
          // CAN-CLOSE-only：弹至多 runLen 个 unit，逐个成对
          for (let k = 0; k < runLen && stack.length > 0; k++) {
            if (stack.pop() !== lineno) return true;
          }
        }
        continue;
      }
      i += 1;
    }
  }
  return false;
}

// 封闭文法：一行是否落在「可规整」的允许集合内。对应 Python `_line_normalizable`。
// info 由调用方 isRichMarkdown 预先 classifyLine 传入（同一行只分类一次，也供其跨行
// 的懒延续判定复用）。
function lineNormalizable(line: string, info: LineInfo): boolean {
  if (info.kind === "blank") return true; // 规则 1：空行
  // F1：CommonMark 硬换行（>=2 尾随空格 / 单个尾随反斜杠）在**任何**非空行上都必须
  // 整格拒绝——规整会 strip 掉标记并把段内换行改成段落断裂（见 hasHardBreak）。放在
  // marker/prose 分派之前，故对 list-marker 行（body 会被 classifyLine 的 pyStrip
  // 悄悄吞掉尾随硬换行）同样生效。
  if (hasHardBreak(line)) return false;
  // Batch F 规则 2：CommonMark 主题分隔线必须在 marker 接受**之前**拦截——
  // `* * *`/`- - -`/`_ _ _`/`***`/`___` 会被当成 list marker 规整、毁掉分隔线。
  if (isThematicBreak(line)) return false;
  // 行尾仍开着的行内构造（未闭合的 `$` 公式 / 反引号代码 / `[` 链接·图片文本）会跨软
  // 换行——normalizeImpl 注入空行会把它拆断，符号盲的 content_invariant 兜不住 -> 整格
  // 拒绝。放在 marker/prose 分派之前，故对 list-marker 行的正文同样生效。强调 `*`/`_` 与
  // 删除线 `~` 的跨行配对改由 hasCrossLineEmphasis 在 isRichMarkdown 做全格判定。
  if (endsWithOpenInlineSpan(line)) return false;
  // F1（review）：行中「标签形状」的未转义 `<`（`<span`/`</em` …）跨软换行，
  // normalizeImpl 注入空行拆断它、符号盲 content_invariant 兜不住 -> 整格拒绝。
  // 放在 marker/prose 分派之前，故对 list-marker 行的正文同样生效。
  if (hasMidlineInlineHtml(line)) return false;
  if (info.kind === "bullet" || info.kind === "ordered" || info.kind === "alpha") return markerIndentOk(line); // 规则 2：list-marker 行（含 CommonMark 4 空格消歧）
  // 规则 3：顶格 prose——有前导 tab/空格的非 marker 行一律拒绝（缩进代码块 /
  // 列表延续行 / 缩进 prose 一整类），顶格但以其它块级构造开头的也拒绝。
  if (line[0] === " " || line[0] === "\t") return false;
  return !startsBlockConstruct(line);
}

// isRichMarkdown = 「不安全规整」的 allow-list 门（Python is_rich_markdown 的
// 语义反转，保留导出名以减少改动面）：当且仅当**每一行**都命中 lineNormalizable
// 的封闭文法、且不含 Batch F 规则 1 的懒延续相邻时返回 false（可规整）；任一行落
// 在文法之外（或触发懒延续）即返回 true（原样不动）。对未知结构 fail CLOSED。
// Batch F 规则 1（上下文感知）：顶格 prose 行**紧跟**（无空行）marker 行
// （bullet/ordered/alpha）时是该列表项的 CommonMark 懒延续，normalizeImpl 会注入空
// 行拆散它；marker 与 prose 间夹一个空行会按 CommonMark 断开延续，故 marker -> 空行
// -> prose 仍放行（已规整语料的常见形状）。空/纯空白输入返回 false（交给 normalizeImpl
// 归一为空串）。F1 强调/删除线跨行配对（全格）：hasCrossLineEmphasis 先做一次跨整格的
// `*`/`_`/`~` 配对扫描——任一对强调或删除线落在两个不同行即拒绝；孤立词内 `R*C`/`foo~bar`
// （永不成对）仍放行。
export function isRichMarkdown(raw: string): boolean {
  if (!raw || !pyStrip(raw)) return false;
  const src = raw.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const lines = src.split("\n");
  if (hasCrossLineEmphasis(lines)) return true; // F1：强调跨行成对，注入空行会拆断
  let prevKind: LineKind = "blank"; // 首行无前驱：非 marker，规则 1 不触发
  for (const line of lines) {
    const info = classifyLine(line);
    if (!lineNormalizable(line, info)) return true;
    if (info.kind === "prose" && (prevKind === "bullet" || prevKind === "ordered" || prevKind === "alpha")) {
      return true; // 规则 1：marker 后紧跟顶格 prose = 懒延续
    }
    prevKind = info.kind;
  }
  return false;
}

function normalizeImpl(raw: string): string {
  if (!raw || !pyStrip(raw)) return "";
  const src = raw.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const lines = src.split("\n");

  // isRichMarkdown 这道 allow-list 门已保证能走到 normalizeImpl 的 cell 每一行都
  // 是空行 / list-marker 行 / 顶格 prose，没有围栏、表格、缩进代码块之类需要原样
  // 透传的块级结构（那些在门口就被整格拒绝，ruleNormalize 直接原样返回）。故这里
  // 纯逐行分类/重排，不再有 verbatim 区间与保护掩码。
  const infos: LineInfo[] = lines.map((line) => classifyLine(line));
  const nonBlank = infos.filter((info) => info.kind !== "blank");
  const cellMin = nonBlank.length > 0 ? Math.min(...nonBlank.map((info) => info.depth)) : 0;

  const out: string[] = [];
  let prev: PrevKind = "start";
  let groupBase: number | null = null; // 当前列表组是否已开始（非 null = 组内）；具体层级判断见 depthStack
  // P2 修复：每个已打开层级的「源 depth」+ 实际发出的 marker 宽度（"- " ->
  // 2，"1. " -> 3，"10. " -> 4）。子项的缩进 = 其所有祖先层级宽度之和，而不是
  // 固定「每层 2 格」——CommonMark 要求子块缩进到父项 marker 之后内容开始的
  // 那一列；父项是 bullet/alpha（恒渲染成 "- "，宽度 2）时两者恰好相等，但
  // 父项是 ordered 时 marker 宽度随数字位数变化，固定 2 格会让 "1. parent\n
  //   - child" 解析成两个并列的顶层列表，而不是嵌套（用真实
  // remark/GFM 解析器验证过，见 md_normalize.py 对应注释）。
  //
  // 层级判断刻意用「栈 + 序数比较」（比当前栈顶更深 / 持平 / 更浅），不用
  // `info.depth - groupBase` 减法：indentDepth（tab 数 + 前导空格数 // 2）对
  // 「非 2 的倍数」的缩进宽度是有损的——比如宽度 4（双位数 ordered 的子项）
  // 反解出来的 depth 是 2 而不是 1，减法会把它错判成多一层，规整结果自身再
  // 规整一遍（幂等性）就会一次比一次多缩进（已用双位数 ordered / 三层全
  // ordered 嵌套两个场景验证过这个漂移）。栈序数比较只关心「比当前已打开的
  // 最深层级更深 / 一样 / 更浅」这个相对关系，而 indentDepth 对纯空格前缀
  // 始终单调不减，所以序数比较在这种有损换算下仍然稳定、可幂等。随
  // groupBase 一起重置——新列表组不继承上一组的祖先层级。
  let depthStack: number[] = []; // 每个已打开层级对应的源 depth，按层级顺序（浅→深）
  let levelWidth: number[] = []; // 与 depthStack 一一对应：该层级目前的 marker 渲染宽度

  for (const info of infos) {
    if (info.kind === "blank") continue; // 间距完全由 prev 重推；空行不重置列表组

    // 顶格 alpha（A. / B.，处在 cellMin）= 分节标题 → 加粗段落；缩进的 alpha 是子项
    if (info.kind === "alpha" && info.depth === cellMin) {
      if (out.length > 0) out.push("");
      out.push(`**${info.marker}. ${info.body}**`);
      prev = "header";
      groupBase = null;
      continue;
    }

    if (info.kind === "bullet" || info.kind === "ordered" || info.kind === "alpha") {
      if (prev !== "list" || groupBase === null) {
        groupBase = info.depth; // 新列表组已开始（关键：随 prose/header 重置）
        depthStack = [];
        levelWidth = [];
      }
      while (depthStack.length > 0 && depthStack[depthStack.length - 1] > info.depth) {
        depthStack.pop(); // 回到更浅的层级：先弹出已经更深的祖先
        levelWidth.pop();
      }
      let level: number;
      if (depthStack.length > 0 && depthStack[depthStack.length - 1] === info.depth) {
        level = depthStack.length - 1; // 命中已打开的同一层级 -> 该层的兄弟项
      } else {
        level = depthStack.length; // 比当前已打开的最深层级更深 -> 开一个新层级
        depthStack.push(info.depth);
        levelWidth.push(0); // 占位，下面立即写入真实宽度
      }
      const marker = info.kind === "ordered" ? `${info.marker}. ` : "- ";
      const ancestorWidth = levelWidth.slice(0, level).reduce((a, b) => a + b, 0);
      const indent = " ".repeat(ancestorWidth); // 缩进 = 祖先层级（不含自己）宽度之和
      // F3：宽度必须按 code point 计（`[...marker].length`），不能用
      // `marker.length`（UTF-16 单元数）——星际有序 marker 如 `𝟙. `（U+1D7D9 是
      // 代理对）的 UTF-16 单元数是 4、code point 数是 3，而子项缩进 = 祖先 marker
      // 宽度之和，用 UTF-16 单元数会比 Python len（按 code point）多缩进一格、两侧
      // 孪生实现漂移。marker 恒以 `. `/`- ` 收尾，仅 info.marker 可能含星际数字。
      levelWidth[level] = [...marker].length; // 这一层「当前」宽度（code point），供更深子项引用
      if (prev === "prose" || prev === "header") {
        out.push(""); // 列表开始前补空行
      }
      out.push(`${indent}${marker}${info.body}`);
      prev = "list";
    } else {
      // prose
      if (out.length > 0) out.push(""); // 相邻散行各自成段
      out.push(info.body);
      prev = "prose";
      groupBase = null;
    }
  }

  // 折叠连续空行（沿用旧版 `\n{3,}` 折叠的用意）。门反转后 normalizeImpl 只处理
  // 逐行 prose/marker，本身不会发出连续空行，这一步实际是防御性 no-op，保留以兜住
  // 未来的重排改动。
  const collapsed: string[] = [];
  {
    let j = 0;
    const m = out.length;
    while (j < m) {
      if (out[j] === "") {
        collapsed.push("");
        let k = j + 1;
        while (k < m && out[k] === "") k++;
        j = k;
        continue;
      }
      collapsed.push(out[j]);
      j++;
    }
  }

  return pyStrip(collapsed.join("\n"));
}

// 规整入口——永不抛：任何异常返回原文（与 Python rule_normalize 同约定）。
// 先过 isRichMarkdown 这道保守闸门：命中就直接原样返回 raw（连 CRLF 归一化、
// pyStrip 都不做——「完全不动」是字面意思），完全不进入 normalizeImpl 的逐行
// 分类/重排。
export function ruleNormalize(raw: string): string {
  try {
    if (isRichMarkdown(raw)) return raw;
    return normalizeImpl(raw);
  } catch {
    return raw;
  }
}

// ===========================================================================
// knowhow-md-normalize Task 8：编辑器「规整格式」按钮 + 粘贴即时规整
// ===========================================================================

// --- shouldNormalizePaste：粘贴触发判定 -------------------------------------
//
// 与 ruleNormalize 职责分离：ruleNormalize 回答"怎么规整"，这里只回答"要不要
// 拦截这次粘贴走规整"。
//
// 代价不对称，判定必须保守：漏判(miss)代价低——用户仍可手动点工具栏「规整
// 格式」按钮，那条路径带 before/after 对照预览，可以先看结果再决定接受/
// 放弃；误判(false positive)代价高且可能不可恢复——粘贴命中后走的插入路径
// (applyInsertion -> setContent) 是对受控 <textarea> 编程式设置 .value，不
// 走浏览器原生粘贴，不进原生撤销栈，Cmd+Z 大概率救不回被强行拆散的原文。
// 因此判定条件收紧为"必须有正的列表证据"，不能仅凭"含 TAB"就触发——曾经的
// 版本正是这样，会把整段 TAB 缩进的代码（Tcl/Python/Makefile 等）当成 Excel
// 粘贴内容拆散、逐行插空行（本仓库有专门的「代码」工具栏按钮与代码附件
// 功能，粘贴代码是预期内容形态，绝不能被误伤）。
//
// 现在只有以下两条之一成立才返回 true，其余一律 false（交给浏览器原生粘贴）：
//   (a) 存在至少一行，其去掉行首缩进（TAB/半角空格）后以 BULLET_GLYPHS 里的
//       字形开头、紧跟空白——即该字形是「行首 marker」，与 classifyLine 自己
//       判定 bullet 的标准完全同源（同一个 BULLET_RE，应用在同一次
//       lstripTabSpace 之后），不是"文本里任意位置出现这个字符"。
//       I3 修复：BULLET_GLYPHS 含 · (U+00B7)，中文里常见于人名内部的分隔符
//       （如"弗拉基米尔·普京"）——旧判定"字形出现在任意位置即触发"会把这类
//       含人名的普通段落误判成 Excel 列表粘贴，进而拆散成每行一段（该插入
//       路径不进浏览器原生撤销栈，见下）。收紧到"行首 marker"后，人名内部的
//       · 不再计入证据，但真正顶格或缩进的"· item"列表行不受影响——用同一套
//       "行首 marker"标准处理了这整类字形（不仅仅是 ·）。
//   (b) 存在至少一行"缩进"（行首为 TAB 或半角空格），且去掉该缩进后剩余
//       内容匹配 ORDERED_RE 或 ALPHA_RE（有序数字/单字母 marker）——这是
//       Excel"缩进子项"的典型形状（如"1. 步骤一"下一行缩进一个"a. 子项"）。
// 复用 classifyLine 同一套 BULLET_RE / ORDERED_RE / ALPHA_RE / lstripTabSpace，
// 不再另写判断正则——这里认定"有列表证据"的标准，理应与 ruleNormalize 自身
// 认定"这是列表行"的标准同源，不搞两份可能漂移的逻辑。注意 (a) 的 BULLET_RE
// 命中后还要求 marker 字符属于 BULLET_GLYPHS，排除 "-"/"*"/"+" 这几个 ASCII
// 记号（BULLET_RE 本身也匹配它们）——那几个字符在代码里太常见（CLI 参数
// "- v"、减法……），且"已是干净 markdown 列表"（如"- 一\n- 二"）本就不该
// 触发（拦截这次粘贴没有收益，ruleNormalize 对它是 no-op）；同理 (b) 故意不收
// BULLET_RE 那几个 ASCII 记号，只有数字/字母+分隔符这种更 Excel 特有的形状才
// 够格当缩进证据。
//
// 纯 TAB 分隔的一整行（如 Excel 复制的一行"列1\t列2\t列3"）不再触发：
// ruleNormalize 对单行输入是 no-op（没有列表 marker，整行归类成一个 prose
// 段，规整前后逐字相同），拦截这次粘贴没有任何收益，徒增"抢用户原生粘贴"的
// 风险（knowhow-cell-editor.test.mjs 用实际调用 ruleNormalize 验证了这一点）。
//
// BULLET_GLYPHS 与 Python 侧同名常量（backend/app/services/knowhow/
// md_normalize.py）同一字符集，这里独立导出成常量，不去改造 BULLET_RE 内联的
// 字符类字面量——那行正则的具体写法已被 parity 单测锁定
// （knowhow-normalize.test.mjs 与 backend/tests/test_md_normalize_rule.py 的
// golden fixture），重构成"共享同一个常量拼出字符类"对语义没有增益，却会让
// 未来 diff review 多一层"这一行到底有没有动语义"的辨认负担，两处各自持有
// 一份同样的字面量更省心。
export const BULLET_GLYPHS = "•●◦▪‣·";

export function shouldNormalizePaste(text: string): boolean {
  if (!text) return false;
  // 与 ruleNormalize 共用同一道保守闸门：粘贴内容如果已经是真实 markdown
  // 结构（围栏代码块、GFM 表格、引用块……），不该被当成 Excel 脏排版拦截
  // 重排——尤其这条插入路径 (applyInsertion -> setContent) 本就不进浏览器
  // 原生撤销栈（见上方注释），误判代价比 ruleNormalize 自身更高。没有这道
  // 门时，规则 (a)/(b) 只逐行扫，看不出"这几行合起来是一个围栏/表格"，块的
  // 内部恰好长得像 Excel 缩进子项（比如代码里一行缩进的 "a. xxx"）就会被
  // 误判为 true。
  if (isRichMarkdown(text)) return false;
  const lines = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
  for (const line of lines) {
    // (a)：去掉行首缩进后必须整行匹配 BULLET_RE，且命中的 marker 字符属于
    // BULLET_GLYPHS（排除 "-"/"*"/"+"，见上方注释）——与 classifyLine 判定
    // bullet 的标准同源，不是"字形出现在文本任意位置"。
    const stripped = lstripTabSpace(line);
    const bulletMatch = BULLET_RE.exec(stripped);
    if (bulletMatch && BULLET_GLYPHS.includes(bulletMatch[1])) return true;
    if (line[0] !== "\t" && line[0] !== " ") continue; // 未缩进：不构成 (b) 的证据
    if (ORDERED_RE.test(stripped) || ALPHA_RE.test(stripped)) return true;
  }
  return false;
}

// --- P2-f：规整后文本插入——优先走原生撤销栈 --------------------------------
//
// 命中 shouldNormalizePaste 的粘贴会被 preventDefault + 程序化插入规整后的文本。
// 老路径 applyInsertion -> setContent 是对受控 <textarea> 编程式赋值 .value，
// **不**加入 textarea 的原生编辑事务，Cmd/Ctrl+Z 撤不掉这次自动重排。
//
// 改进：先试 document.execCommand("insertText", false, normalized)——它在
// Chromium/WebKit/Gecko 里于光标处插入并替换选区、**加入原生撤销栈**，并触发
// input 事件让 React 受控组件的 onChange 照常更新 state（与正常打字同一条路）。
// execCommand 已废弃、部分环境可能移除或抛错：返回 false 或抛错时回退到旧的
// applyInsertion 路径（不进撤销栈，但粘贴仍生效）——优雅降级，宁可不可撤销也不
// 让这次粘贴丢失。
//
// 把"试 execCommand、失败回退"的决策抽成纯函数便于单测：尝试与回退都以 thunk
// 注入（生产传真实闭包，测试传桩：返回 true / 返回 false / 抛错）。返回值=是否
// 真的走成了 execCommand（true=已进原生撤销栈；false=已回退到 applyInsertion）。
export function insertViaExecCommandOrFallback(tryExecCommand: () => boolean, fallback: () => void): boolean {
  let ok = false;
  try {
    ok = tryExecCommand();
  } catch {
    // execCommand 在部分环境抛错（已废弃/被禁用）——按失败处理，走回退。
    ok = false;
  }
  if (!ok) fallback();
  return ok;
}

// --- 规整格式：before/after 对照状态机 --------------------------------------
//
// 结构镜像 knowhow-optimize-logic.ts 的 CellOptimizeState（「优化表达」的同一
// 个 before/after 对照面板既有实现），多一个 "unchanged" 态：reformat_cell 会
// 返回 changed=false（这一格已经是干净 markdown，规则/LLM 都判定无需改动），
// 这个语义在"优化表达"那边不存在（LLM 改写几乎总会产生字面差异），此时不该
// 展示一个左右一模一样的空对照面板，而是走 REFORMAT_UNCHANGED_MESSAGE 的独立
// 友好提示分支（任务要求：用提示替代空 diff）。
export type CellReformatState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; candidateMd: string; source: string }
  | { status: "unchanged"; source: string }
  // 陈旧态：本次规整候选已对不上眼前该展示的原文，接受会误导地覆盖，故不展示对照
  // 面板、转入这个友好信息态（非 error——不是出错）。两种成因用 reason 区分、各配
  // 一句文案：
  //   · "local"（P1-d）：请求在飞期间用户在**本编辑器**里敲了字（currentContent
  //     偏离发起时快照）——候选基于的已保存原文已过时，用户的字仍在、重试即可。
  //   · "server"（F3）：后端回带的 sourceMd（它实际读来喂给 reformat 的原文）≠ 本
  //     编辑器发起请求那一刻的基线——说明**另一个标签页/进程**在本编辑器打开后又保存
  //     过这一格，候选是拿本地从没见过的服务器内容算出来的、对照面板左侧的「原文」
  //     已是旧值，接受会误导地覆盖。此时重试也没用（基线本身旧了），只能关闭后重开。
  | { status: "stale"; reason: "local" | "server" }
  | { status: "error"; message: string };

export const CELL_REFORMAT_IDLE: CellReformatState = { status: "idle" };

// 本地陈旧（P1-d）的单例常量（同 CELL_REFORMAT_IDLE 的写法）——到达守卫的本地改动
// 分支与接受守卫两处都转入它，附带文案是下面的 REFORMAT_STALE_MESSAGE。
export const CELL_REFORMAT_STALE: CellReformatState = { status: "stale", reason: "local" };

// 服务器陈旧（F3）的单例常量——到达守卫发现 sourceMd 与发起快照不一致时转入它，
// 附带文案是下面的 REFORMAT_SERVER_STALE_MESSAGE（组件按 reason 选文案）。
export const CELL_REFORMAT_SERVER_STALE: CellReformatState = { status: "stale", reason: "server" };

// 「规整建议」对照面板右侧标题——与「优化表达」的 OPTIMIZE_SUGGESTION_LABEL
// （"优化建议"）区分开，避免用户把"规整格式"的结果误当成"优化表达"的结果
// （左侧"原文"沿用 knowhow-optimize-logic.ts 的 OPTIMIZE_ORIGINAL_LABEL 即可，
// 两个面板都是"跟已保存原文对照"，不需要为此再造一个同义常量）。
export const REFORMAT_SUGGESTION_LABEL = "规整建议";

// changed=false 时的友好提示（任务要求：替代空对照面板）。
export const REFORMAT_UNCHANGED_MESSAGE = "已经是规整格式，无需改动";
export const REFORMAT_UNCHANGED_DISMISS_LABEL = "知道了";

// P1-d：编辑器内容在规整请求在飞期间被改动，本次候选已对不上眼前的内容，
// 丢弃并提示重来（用户的字原样留在编辑框，不被覆盖）。「知道了」复用
// REFORMAT_UNCHANGED_DISMISS_LABEL——两者都只是退出这层提示回到编辑视图。
export const REFORMAT_STALE_MESSAGE = "编辑器内容已变化，本次规整结果已失效，请重试";

// F3：服务器上的原文在本编辑器打开后被他人改动（后端回带的 sourceMd ≠ 发起快照）。
// 与本地陈旧不同——重试也没用（对照面板的「原文」基线本身已旧），只能关闭后重开
// 拿到最新原文再规整。文案刻意与 REFORMAT_STALE_MESSAGE 区分，给用户对得上的指引。
export const REFORMAT_SERVER_STALE_MESSAGE = "服务器上的内容已被其他人修改，请关闭后重新打开";

// 禁用原因提示文案——同 knowhow-optimize-logic.ts 的 OPTIMIZE_*_HINT 三兄弟。
export const REFORMAT_LOADING_HINT = "规整中，请稍候";
export const REFORMAT_UNSAVED_HINT = "有未保存的修改，请先保存后再规整格式";
export const REFORMAT_EMPTY_HINT = "格子为空，无需规整格式";

export function beginCellReformat(state: CellReformatState): CellReformatState {
  return state.status === "loading" ? state : { status: "loading" };
}

// 后端结果到达：changed=true -> ready（候选内容供左右对照）；changed=false ->
// unchanged（只带 source，供旁边小标签说明"是规则还是 AI 判定的无需改动"）。
// 同 optimize 侧 resolveCellOptimizeSuggestion，只有"正在请求中"才会被结果
// 打断——状态若已被其它操作带离 loading（理论上不会发生，按钮在 loading 期间
// 被禁用），迟到的结果原样丢弃，不覆盖新状态。
export function resolveCellReformat(
  state: CellReformatState,
  result: { candidateMd: string; source: string; changed: boolean },
): CellReformatState {
  if (state.status !== "loading") return state;
  return result.changed
    ? { status: "ready", candidateMd: result.candidateMd, source: result.source }
    : { status: "unchanged", source: result.source };
}

// 规整请求到达时的陈旧丢弃守卫。reformat_cell 读的是**已保存**的格子内容，且
// 回带它实际读到的原文 sourceMd（并发防护）。请求在飞期间 textarea 并未禁用，且
// 另一个标签页/进程也可能保存这一格。若照常展示对照面板、用户点「接受」，候选会
// 整段覆盖掉现场。故到达时按序比对：
//   · 已不在 loading（状态被后续操作带离，理论上不会发生，按钮 loading 期间禁用）
//     → 原样返回，迟到结果不覆盖新状态（同 resolveCellReformat 的既有语义）；
//   · (P1-d) 当前内容 ≠ 发起时快照 → 本地 stale（用户在本编辑器敲了字，候选覆盖会
//     丢字；用户的字仍在、"重试"即可，故先于服务器判定——本地改动是更贴近用户的成因，
//     且这保持 P1-d 既有契约不变）；
//   · (F3) sourceMd ≠ 发起时快照 → 服务器 stale（他人在本编辑器打开后又保存过这一格，
//     候选是拿本地从没见过的服务器内容算的、对照左侧「原文」已旧，接受会误导覆盖；
//     与批量规整 reformatResultIsStale 的 originalMd!==sourceMd 同一口径）；
//   · 都未变 → 委托 resolveCellReformat 照常处理（changed→ready / 无改动→unchanged）。
// 发起请求那一刻按钮要求「无未保存改动」，故 contentAtRequest 就是当时的已保存基线，
// 拿它同时当「本地快照」和「服务器基线」两个比对基准是自洽的。刻意复用
// resolveCellReformat 而非复制它的 ready/unchanged 分支——扩展既有状态机，不另起一份
// 可能漂移的实现。
//
// 注意：兄弟「优化表达」流程（knowhow-optimize-logic.ts 的
// resolveCellOptimizeSuggestion）有同样的弱点（请求在飞期间内容可变），但那是
// master 上既有行为，不在本 PR 范围内——此处只加固「规整格式」这一条。
export function resolveReformatArrival(
  state: CellReformatState,
  contentAtRequest: string,
  currentContent: string,
  result: { candidateMd: string; source: string; changed: boolean; sourceMd: string },
): CellReformatState {
  if (state.status !== "loading") return state;
  if (currentContent !== contentAtRequest) return CELL_REFORMAT_STALE;
  if (result.sourceMd !== contentAtRequest) return CELL_REFORMAT_SERVER_STALE;
  return resolveCellReformat(state, result);
}

// F（review）：到达解析后是否需要**请父级刷新整表 detail**。仅服务器陈旧（reason
// ==="server"）需要——他人在本编辑器打开后又保存过这一格，父级 detail 快照（savedContent
// 之源）已是旧值；只关掉浮窗只清了 modal、快照仍旧，重开撞同一 server-stale 态、永远
// 循环（这正是 F 修复要堵的洞）。本地陈旧（reason==="local"，用户在本编辑器敲了字）
// **不**需要：服务器基线没变、重试即可，刷新反而可能打断用户。ready/unchanged/idle/
// loading/error 亦不需要。组件据此在到达后回调 onServerStale（见 knowhow-cell-editor.tsx）。
export function reformatArrivalNeedsParentRefresh(state: CellReformatState): boolean {
  return state.status === "stale" && state.reason === "server";
}

export function failCellReformat(state: CellReformatState, message: string): CellReformatState {
  return state.status === "loading" ? { status: "error", message } : state;
}

// 接受候选 / 放弃候选 / 「知道了」关掉 unchanged 提示，三者都回到同一个
// idle——组件侧各自的额外副作用（接受时 setContent 回填 textarea）不属于这个
// 纯状态机，由 knowhow-cell-editor.tsx 的 handler 自己做。
export function dismissCellReformat(): CellReformatState {
  return CELL_REFORMAT_IDLE;
}

export function isCellReformatLoading(state: CellReformatState): boolean {
  return state.status === "loading";
}

// 「规整格式」按钮禁用原因（null=可点击）——判定优先级与
// knowhow-optimize-logic.ts 的 optimizeCellDisabledReason 完全一致（正在
// 请求中 > 有未保存修改 > 内容为空 > 可点击）：reformat_cell 与 optimize_cell
// 同样读的是已保存的格子内容而非编辑框里的草稿，若两者不一致仍允许触发，
// 用户会看到"原文"对不上自己刚打的字，所以有未保存修改时先挡住。两个
// disabledReason 函数分属 Task 8/Task 9 各自的任务文件，未合并成一个共享
// helper——参数形状相同不代表应该耦合成一个跨特性共享的函数。
export function reformatCellDisabledReason(
  content: string,
  savedContent: string,
  state: CellReformatState,
): string | null {
  if (isCellReformatLoading(state)) return REFORMAT_LOADING_HINT;
  if (hasUnsavedChanges(content, savedContent)) return REFORMAT_UNSAVED_HINT;
  if (!content.trim()) return REFORMAT_EMPTY_HINT;
  return null;
}

// --- reformatSourceLabel：后端 source 枚举 -> 友好文案 ----------------------
//
// 后端 reformat_cell 的 source ∈ {"llm", "rule/llm-failed", "rule/no-llm"}
// （backend/app/services/knowhow/api.py 同名函数文档字符串逐字对照）。这三个
// 原始枚举值本身都不该出现在界面上——本仓库的硬规则是后端枚举值不得直出给
// 用户，尤其 "rule/llm-failed" 会把"AI 规整格式失败、已退回规则"这类实现
// 细节暴露给用户，徒增困惑却没有实际帮助；"rule/llm-failed" 与 "rule/no-llm"
// 对用户而言是同一件事——"这次是规则规整的"，AI 到底是没配置还是配置了但
// 失败，不值得也不应该展示。任何不在这三个已知值内的输入（后端未来新增
// 枚举 / 传输异常等防御性场景）一律退化到 REFORMAT_SOURCE_FALLBACK_LABEL，
// 绝不把原始字符串吐给用户。
export const REFORMAT_SOURCE_LLM_LABEL = "已用 AI 规整格式";
export const REFORMAT_SOURCE_RULE_LABEL = "已用规则规整格式";
export const REFORMAT_SOURCE_FALLBACK_LABEL = "已规整格式";

export function reformatSourceLabel(source: string): string {
  if (source === "llm") return REFORMAT_SOURCE_LLM_LABEL;
  if (source === "rule/llm-failed" || source === "rule/no-llm") return REFORMAT_SOURCE_RULE_LABEL;
  return REFORMAT_SOURCE_FALLBACK_LABEL;
}
