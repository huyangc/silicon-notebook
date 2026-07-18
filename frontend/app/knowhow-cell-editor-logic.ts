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
// storage 为 null（SSR：没有 window）或读取抛错（隐私模式/配额）时一律当「没有
// 草稿」——读不到就没有可恢复的东西，也就没有「恢复会整段覆盖掉刚落笔的图片」那条
// 风险；这里只如实回答「读到了什么」，放不放行上传由调用方的门禁决定。
export interface DraftReadStorage {
  getItem(key: string): string | null;
}
export function readCellDraft(storage: DraftReadStorage | null, key: string): string | null {
  if (!storage) return null;
  try {
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

// 编辑器是否处在「有异步在飞」的忙态：保存中 / 图片上传中 / 优化表达请求中。
// 用于门控**发起类**入口（保存、切兄弟格）——但刻意不门控**离开类**入口
// （关闭/Esc/背景/取消）：离开的安全性由下面的 resolveSaveCompletion 陈旧回调
// 守卫来保证，若连离开都禁掉，请求卡住时用户会被关在弹窗里出不来。
export function isEditorBusy(saving: boolean, uploading: boolean, optimizing: boolean): boolean {
  return saving || uploading || optimizing;
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
// 在服务端提交之后的那个窄窗口里，会留下一条没有任何东西引用的资产行，而本仓库的
// sweep_orphan_assets 目前还没有生产调用方（接它是后端侧的单独改动）。这不影响
// 用户看到的内容，故文案只说「放弃」，不再向用户承诺「会尽量留着」这种在存储不可
// 用时兑现不了的话。
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
