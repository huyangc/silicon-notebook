// Knowhow 表 — 管理与建表向导的纯逻辑（无 JSX，可被 knowhow-manage.test.mjs
// 直接 import）。knowhow-manage.tsx / knowhow-import.tsx 含 JSX，Node 原生 TS
// 类型剥离不支持 .tsx（仅 .ts/.mts/.cts 可被 node --test 直接 import），故把
// 建表向导表头状态机 / 内容类型选项与提示语 / 行标题列选择器文案 / 导入向导
// 猜测预选映射 / 管理动作 payload 组装 这些可测纯逻辑单独抽出（镜像
// knowhow-panel.tsx <-> knowhow-panel-logic.ts、knowhow-import.tsx <->
// knowhow-import-logic.ts 的既有拆分方式）。
//
// 词表口径（规格① 2026-07-15 修订）：列的内容类型下拉只剩三项
// （方法步骤/工具/事物/普通），「行标题」不进列下拉、走表级独立选择器
// 「行标题列：[某列 ▾ / 不设置]」；行标题列 0..1 可选（存量「恰一」校验已
// 放宽为「至多一」，knowhow-import-logic.ts 里的 conceptValidationError/
// canSubmitImport/assembleImportColumns/ROLE_OPTIONS 即被本文件的
// 选择器版函数取代——旧函数保留在原文件（已注明 deprecated）等 Task 3 wire
// 落地后统一清理，本任务文件白名单不含 knowhow-import-logic.ts，不动它）。

import { KIND_LABELS, type ColumnKind, type KnowhowColumnInput, type KnowhowCreateTableInput, type KnowhowTablePatch } from "./knowhow-model.ts";
import { isBlankTitle } from "./knowhow-import-logic.ts";

// --- 内容类型选项与提示语（规格①，文案逐字）------------------------------------

// 三种内容类型各自的一行提示语（规格①括注原文）：向导/管理里的下拉 title 与
// 图例（KindLegend）共用，单一事实来源。
export const KIND_HINTS: Record<ColumnKind, string> = {
  procedure: "写做法/流程的列，自动识别有序步骤",
  entity: "列出的名称自动归并：工具、命令、文档等",
  attribute: "仅作为内容参与检索",
};

export type KindOption = { value: ColumnKind; label: string; hint: string };

// 列内容类型下拉的三个选项：顺序/文案取自 KIND_LABELS（方法步骤/工具/事物/
// 普通），提示语取自 KIND_HINTS。anchor 不在其中——行标题走表级选择器。
export const KIND_OPTIONS: KindOption[] = (Object.keys(KIND_LABELS) as ColumnKind[]).map((kind) => ({
  value: kind,
  label: KIND_LABELS[kind],
  hint: KIND_HINTS[kind],
}));

// --- 行标题列选择器文案（规格①，文案逐字）--------------------------------------

export const ANCHOR_SELECTOR_LABEL = "行标题列";
export const ANCHOR_NONE_LABEL = "不设置";

// 已选行标题列时的提示（规格①：「用作每行的标题；设置后每行作为一个节点进入
// 知识图谱，节点名取自该列」）。
export const ANCHOR_SET_HINT = "用作每行的标题；设置后每行作为一个节点进入知识图谱，节点名取自该列";

// 未选行标题列时的提示（规格①：向导无主题时提示，随「主题→行标题」改名）。
export const ANCHOR_NONE_HINT = "未选行标题列：本表仅参与问答检索，不构建图谱节点";

// 选择器下方提示语：selection 为 null（不设置）时给「仅检索投影」提示，否则给
// 「入图谱」提示。selection 兼容两种形态——建表/导入向导用列下标(number)，
// 管理面板用列 id(string)。
export function anchorHint(selection: number | string | null): string {
  return selection === null ? ANCHOR_NONE_HINT : ANCHOR_SET_HINT;
}

// --- 建表向导：表头状态机 --------------------------------------------------------

export type WizardColumn = { name: string; kind: ColumnKind };

// 向导「定表头」这一步的完整状态：列定义（名+内容类型）+ 行标题列下标
// （null=不设置）。列此时尚无 id，选择器只能按下标定位，增删/换序时由下面的
// 状态转移函数负责让 anchorIndex 跟着列走。
export type WizardHeaderState = { columns: WizardColumn[]; anchorIndex: number | null };

// 初始状态：一列空名（默认内容类型=普通），未设行标题——用户打开向导即有一行
// 可填，而不是先点一次「加一列」。
export function initialWizardState(): WizardHeaderState {
  return { columns: [{ name: "", kind: "attribute" }], anchorIndex: null };
}

export function addWizardColumn(state: WizardHeaderState): WizardHeaderState {
  return { columns: [...state.columns, { name: "", kind: "attribute" }], anchorIndex: state.anchorIndex };
}

export function updateWizardColumn(
  state: WizardHeaderState,
  index: number,
  patch: Partial<WizardColumn>,
): WizardHeaderState {
  return {
    columns: state.columns.map((column, i) => (i === index ? { ...column, ...patch } : column)),
    anchorIndex: state.anchorIndex,
  };
}

// 删列：行标题列被删则清空选择（回到「不设置」），删除位置在行标题列之前则
// 下标左移一位补偿。
export function removeWizardColumn(state: WizardHeaderState, index: number): WizardHeaderState {
  const columns = state.columns.filter((_, i) => i !== index);
  let anchorIndex = state.anchorIndex;
  if (anchorIndex !== null) {
    if (anchorIndex === index) anchorIndex = null;
    else if (anchorIndex > index) anchorIndex -= 1;
  }
  return { columns, anchorIndex };
}

// 换序（上移 delta=-1 / 下移 delta=+1）：与相邻列交换；行标题列参与交换时
// 下标跟着列走（无论它是被移动的还是被交换的那个）。越界移动原样返回。
export function moveWizardColumn(state: WizardHeaderState, index: number, delta: -1 | 1): WizardHeaderState {
  const target = index + delta;
  if (index < 0 || index >= state.columns.length || target < 0 || target >= state.columns.length) {
    return state;
  }
  const columns = [...state.columns];
  [columns[index], columns[target]] = [columns[target], columns[index]];
  let anchorIndex = state.anchorIndex;
  if (anchorIndex === index) anchorIndex = target;
  else if (anchorIndex === target) anchorIndex = index;
  return { columns, anchorIndex };
}

export function setWizardAnchor(state: WizardHeaderState, anchorIndex: number | null): WizardHeaderState {
  return { columns: state.columns, anchorIndex };
}

// 建表校验：返回可直接展示的中文提示（null=通过）。行标题列不参与校验——
// 0..1 由单选选择器天然保证（规格①「至多一」）。「表标题不能为空」与后端
// create_knowhow_table 的报错文案一致，本地拦截只是省一次注定 400 的请求。
export function createValidationError(title: string, columns: WizardColumn[]): string | null {
  if (isBlankTitle(title)) return "表标题不能为空";
  if (columns.length === 0) return "至少需要一列";
  const names = columns.map((column) => column.name.trim());
  if (names.some((name) => !name)) return "列名不能为空";
  const seen = new Set<string>();
  for (const name of names) {
    if (seen.has(name)) return `列名重复：${name}`;
    seen.add(name);
  }
  return null;
}

export function canSubmitCreate(title: string, columns: WizardColumn[]): boolean {
  return createValidationError(title, columns) === null;
}

// 建表 payload：标题/列名 trim 后原序提交，anchorIndex 原样透传（null=不设行
// 标题）。形状即 createKnowhowTable fetcher 的入参。
export function assembleCreatePayload(title: string, state: WizardHeaderState): KnowhowCreateTableInput {
  return {
    title: title.trim(),
    columns: state.columns.map((column) => ({ name: column.name.trim(), kind: column.kind })),
    anchorIndex: state.anchorIndex,
  };
}

// --- 导入向导：猜测预选映射 ------------------------------------------------------

// T3 flips this: importKnowhowPreview 目前（后端 Task 3 落地前）线上仍返回
// legacy guessed_role 词表（concept/identify/root_cause/fix/tool/plain，
// grid_parser.guess_roles 过渡 shim 的输出），knowhow-model 的 mapper 原样
// 透传到 guessedRole 字段；Task 3 把 preview 端点改为直接返回三值
// guessed_kind + anchor_suggestion 后，本函数的 legacy 分支自然不再命中，可
// 收缩为纯 passthrough。legacy 'concept' 是「行标题猜测」而非内容类型——
// 内容类型兜底为普通，行标题建议由 deriveImportSelection 单独提取。
export function kindFromGuess(guess: string): ColumnKind {
  switch (guess) {
    case "procedure":
    case "entity":
    case "attribute":
      return guess;
    case "identify":
    case "root_cause":
    case "fix":
      return "procedure";
    case "tool":
      return "entity";
    default:
      return "attribute";
  }
}

// 导入预览 → 向导初始选择：每列内容类型（经 kindFromGuess 归一）+ 行标题列
// 预选下标。行标题预选优先取显式 anchorSuggestion（Task 3 起后端直接给）；
// 缺席时回退扫描 legacy 'concept' 猜测；越界建议一律丢弃（选择器不指向不存
// 在的列）。两边都没有 → null（默认不设置，规格①取消首列兜底）。
export function deriveImportSelection(preview: {
  columns: { name: string; guessedRole: string }[];
  anchorSuggestion?: number | null;
}): { kinds: ColumnKind[]; anchorIndex: number | null } {
  const kinds = preview.columns.map((column) => kindFromGuess(column.guessedRole));
  let anchorIndex: number | null = preview.anchorSuggestion ?? null;
  if (anchorIndex === null) {
    const legacy = preview.columns.findIndex((column) => column.guessedRole === "concept");
    anchorIndex = legacy >= 0 ? legacy : null;
  }
  if (anchorIndex !== null && (anchorIndex < 0 || anchorIndex >= preview.columns.length)) {
    anchorIndex = null;
  }
  return { kinds, anchorIndex };
}

// 导入提交的列定义（选择器版，取代 knowhow-import-logic.assembleImportColumns）：
// 按文件列序逐位组装 `{name, kind}`，kinds 意外比列名短时兜底普通。与建表的
// assembleCreatePayload 同一 wire。
//
// 行标题列**不在这里**：它只经 importKnowhow 的独立 `anchor_index` 传达。
// 曾经这个函数叫 assembleImportColumnsWithAnchor，把选中的行标题列编码成
// `role:'anchor'` 塞进列定义——那是个不存在的后端契约。后端
// `_columns_with_anchor` 只读 `column["kind"]`（缺失时静默默认 'attribute'，
// 不报错），VALID_KINDS 又明确排除 'anchor'，于是每列内容类型 + 行标题列被
// 一起无声丢弃，整张表落库成无 anchor 的记录型表（forward-fill 随之跳过，
// anchor 分组显示完全不生效），而前后端测试全绿。
export function assembleImportColumnKinds(
  names: string[],
  kinds: ColumnKind[],
): { name: string; kind: ColumnKind }[] {
  return names.map((name, index) => ({ name, kind: kinds[index] ?? "attribute" }));
}

// --- 管理面板：payload 与确认文案 ------------------------------------------------

// 破坏性操作确认层文案（管理面板删列/删行共用；表级删除文案在
// knowhow-panel.tsx 既有确认层里，不迁移）。
export const COLUMN_DELETE_CONFIRM = "删除该列？列下所有格子与代码附件将一并删除";
export const ROW_DELETE_CONFIRM = "删除该行？行内所有格子与代码附件将一并删除";

// 表信息保存 patch：只装「确实变化」的字段（PATCH 语义：不提供=不触碰）。
// 标题 trim 后为空不入 patch（表标题不可清空——置空由禁用保存按钮兜底，
// 这里再挡一道）；描述 trim 后与当前不同才入 patch（空串=清除描述，合法）。
// 都没变化返回空对象，调用方据此禁用保存/跳过请求。
export function tableMetaPatch(
  current: { title: string; description: string },
  draftTitle: string,
  draftDescription: string,
): KnowhowTablePatch {
  const patch: KnowhowTablePatch = {};
  const title = draftTitle.trim();
  if (title && title !== current.title) patch.title = title;
  const description = draftDescription.trim();
  if (description !== current.description) patch.description = description;
  return patch;
}

export function hasMetaChanges(patch: KnowhowTablePatch): boolean {
  return Object.keys(patch).length > 0;
}
