// Knowhow 表导入向导 — 纯逻辑（无 JSX，可被 knowhow-import.test.mjs 直接
// import）。knowhow-import.tsx 含 JSX，Node 原生 TS 类型剥离不支持 .tsx
// （仅 .ts/.mts/.cts 可被 node --test 直接 import），故本文件把 payload
// 组装 / concept 校验 / 角色下拉选项 / 默认标题推导 / 文件类型校验 / 错误
// 文案抽取 这些可测纯逻辑单独抽出，镜像 knowhow-panel.tsx <-> -logic.ts
// 的既有拆分方式。knowhow-import.tsx 只调用本文件导出的函数，不重复实现
// 判断逻辑。

import { toUserMessage } from "./errors.ts";
import {
  ROLE_LABELS,
  type KnowhowColumnInput,
  type KnowhowImportOrientation,
  type KnowhowPreviewColumn,
  type Role,
} from "./knowhow-model.ts";

// --- 步骤①：文件选择 -----------------------------------------------------------

export const DEFAULT_IMPORT_ORIENTATION: KnowhowImportOrientation = "columns";

export const IMPORT_ORIENTATION_OPTIONS: {
  value: KnowhowImportOrientation;
  label: string;
  description: string;
}[] = [
  {
    value: "columns",
    label: "属性按列",
    description: "第一行是属性名，每一行是一条记录",
  },
  {
    value: "rows",
    label: "属性按行",
    description: "第一列是属性名，每一列是一条记录",
  },
];

// 支持的导入文件扩展名，顺序即 <input accept> 与提示文案的顺序。与后端
// grid_parser.parse_grid 支持的后缀集合一致（.xlsm 也被后端接受，但向导
// 只暴露规格明确列出的三种，减少用户认知负担；选中 .xlsm 时后端仍会正常
// 处理，只是不出现在 accept/提示文案里）。
export const IMPORT_ACCEPT_EXTENSIONS = [".xlsx", ".csv", ".md"];
export const IMPORT_ACCEPT = IMPORT_ACCEPT_EXTENSIONS.join(",");

// 本地扩展名快速校验：只做"看起来对不对"的即时反馈，避免对明显不支持的
// 文件类型发起一次注定 400 的预览请求（效率优先：能本地拦的不打后端）。
// 真正的结构校验（表头/编码/内容是否可解析）完全交给后端 parse_grid，这里
// 不重复解析文件内容本身。
export function isSupportedImportFile(filename: string): boolean {
  const lower = (filename ?? "").toLowerCase();
  return IMPORT_ACCEPT_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

// 默认表标题 = 文件名去掉最后一个扩展名。文件名没有"真扩展名"（不含点，
// 或点在开头——如 ".gitignore" 这类隐藏文件命名）时原样返回整个文件名，
// 不误删用户文件名里唯一的前导点。
export function deriveDefaultTitle(filename: string): string {
  const name = filename ?? "";
  const dotIndex = name.lastIndexOf(".");
  if (dotIndex <= 0) return name;
  return name.slice(0, dotIndex);
}

// --- 步骤②：预览 + 角色映射 -----------------------------------------------------

export type RoleOption = { value: Role; label: string };

// 角色下拉选项：顺序与 ROLE_LABELS 一致（deprecated：ROLE_LABELS 2026-07-15
// 起收窄为四值 CellKind——行标题(anchor)/方法步骤/工具/事物/普通，见
// knowhow-model.ts 的定义与顶部注释）。单一事实来源——词表变化只需改
// knowhow-model.ts 一处，本文件自动同步。Task 5 会把行标题的选择迁移到独立
// 的表级 anchorColumnId 选择器，届时本下拉可收窄为只剩 KIND_LABELS 三项。
export const ROLE_OPTIONS: RoleOption[] = (Object.keys(ROLE_LABELS) as Role[]).map((role) => ({
  value: role,
  label: ROLE_LABELS[role],
}));

// 提交 payload 组装：columns_json 必须与文件实际列序严格对齐——后端按下标
// 而非列名匹配列 id（services/knowhow/api.py::import_table 用
// column_ids[i] 按位置取列，而非按名字查找），所以这里不能重排、只能逐位
// 对齐 columns（预览返回的文件列序）与 roles（向导内的角色选择状态）。
// roles[index] 缺失（数组意外比 columns 短）时兜底退回该列的猜测角色，
// 不让 undefined 悄悄写进 payload。
export function assembleImportColumns(columns: KnowhowPreviewColumn[], roles: Role[]): KnowhowColumnInput[] {
  return columns.map((column, index) => ({
    name: column.name,
    role: roles[index] ?? column.guessedRole,
  }));
}

// 行标题(anchor)列计数（供 conceptValidationError 复用，也可单独用于展示
// "已选 N 列"）。注：角色词表 2026-07-15 收窄，原 "concept" 值已改名为
// "anchor"（行标题），函数名/校验规则(仍是"恰好一列")暂保留——Task 5 引入
// 独立的表级行标题列选择器后会把"恰好一列"放宽为"至多一列"并整体重做这段
// 校验（规格①："存量「恰一」校验放宽为「至多一」"），本次只做最小改名以保
// 持编译通过、不预先改校验语义。
export function countConceptRoles(roles: Role[]): number {
  return roles.filter((role) => role === "anchor").length;
}

// 行标题(anchor)校验：必须恰好一列为「行标题」角色，否则返回可直接展示给
// 用户的中文提示（null 表示校验通过）。0 列/大于 1 列文案不同，帮助用户判断
// 该新增还是该改掉多余的行标题列。
export function conceptValidationError(roles: Role[]): string | null {
  const count = countConceptRoles(roles);
  if (count === 1) return null;
  if (count === 0) return "请将恰好一列设为「行标题」角色（当前没有行标题列）";
  return `请将恰好一列设为「行标题」角色（当前有 ${count} 列被设为行标题）`;
}

// 表标题为空/纯空白视为未填写。
export function isBlankTitle(title: string): boolean {
  return (title ?? "").trim().length === 0;
}

// 提交按钮启用判定：标题非空 且 概念列恰好一列。
export function canSubmitImport(title: string, roles: Role[]): boolean {
  return !isBlankTitle(title) && conceptValidationError(roles) === null;
}

// --- 步骤②：改选行标题列 → 重取预览的「在飞失效」守卫（P1 修复）----------------
//
// 用户在映射步骤改选行标题列会发起一次「按新锚定列重取预览」的请求。这些请求
// 并发/乱序，且——更隐蔽——会在用户「返回选择步骤 / 另选一个文件」之后才迟到
// 返回。用一个单调递增的请求序号来失效：每次「发起新重取 / 离开映射上下文
// （切文件、返回选择步骤）」都把当前序号 +1；响应落地前用本函数校验「响应
// 序号仍等于当前序号且组件仍挂载」——任何更早发起的在飞响应都会在此失配被丢弃。
//
// 不失效的后果（正是本守卫要挡的 P1）：文件 A 改锚定的在飞重取（responseSeq=N）
// 在用户切到文件 B 后迟到返回，若此时 currentSeq 仍是 N，就会把文件 B 的预览
// 覆盖成文件 A 的行——列数相等时用户甚至能就此把 B 按 A 的列结构导入。切文件 /
// 返回选择步骤时把 currentSeq 递增，这个响应的 responseSeq 便不再等于
// currentSeq，本函数返回 false、响应被丢弃。
export function shouldApplyAnchorPreview(mounted: boolean, responseSeq: number, currentSeq: number): boolean {
  return mounted && responseSeq === currentSeq;
}

// --- 步骤②③：提交按钮的置灰原因（null=可提交）（P2 修复）----------------------
//
// 集中在一处，让「按钮 disabled」与「hover 提示」永远对得上同一个原因（UI 约定
// 「置灰必有对得上的原因」——同 knowhow-optimize-logic.ts 的
// optimizeCellDisabledReason 写法）。submitting 态由按钮自身文案（「导入中…」）
// 表达、不在此列，由调用方另行 OR 进 disabled。优先级：标题未填 > 预览重取
// 未就绪 > 重取失败。
//
// 关键（P2）：改选行标题列后预览正在重取（anchorPreviewLoading）或重取失败
// （anchorPreviewError）时必须挡住提交——否则用户会拿着「旧锚定列规整的预览」
// 或「刷新失败、根本没更新的预览」就提交，而 commit 按新锚定列走，展示≠落库。
// 重取失败时用户重新选一次行标题列即可重试（handleAnchorChange 会清掉 error）。
export const SUBMIT_BLANK_TITLE_HINT = "请先填写表标题";
export const SUBMIT_PREVIEW_REFRESHING_HINT = "预览更新中，请稍候";
export const SUBMIT_PREVIEW_ERROR_HINT = "预览更新失败，请重新选择行标题列后再试";

export function importSubmitDisabledReason(
  title: string,
  anchorPreviewLoading: boolean,
  anchorPreviewError: string | null,
): string | null {
  if (isBlankTitle(title)) return SUBMIT_BLANK_TITLE_HINT;
  if (anchorPreviewLoading) return SUBMIT_PREVIEW_REFRESHING_HINT;
  if (anchorPreviewError) return SUBMIT_PREVIEW_ERROR_HINT;
  return null;
}

// --- 步骤③：提交 -----------------------------------------------------------------

// 后端错误展示（knowhow 各面板的错误条共用这一个入口，~20 个调用点）。
//
// 历史上它自己解析 `${status} ${bodyText}`、自己 unwrap FastAPI 的 detail，
// 因为当时 knowhow-model.ts 的 apiFetch() 就是那么裸抛的。现在 apiFetch()
// 已经走 throwHumanizedHttpError()，那套解析全是死代码；更糟的是它的兜底
// 「拿不到 detail 就展示原文」会把 `TypeError: Failed to fetch` 这类英文
// 技术串直接写进错误条——正是错误人话层要消灭的东西。
//
// 现在它就是 toUserMessage() 的别名：保留这个名字只是为了不动 ~20 个调用点，
// 语义、兜底规则、诊断落 console 全部与全局唯一入口一致。
export function extractErrorMessage(err: unknown, fallback = "操作失败，请重试"): string {
  return toUserMessage(err, fallback);
}

/** 预览表格里一个格子的合并显示信息：`rowSpan===0` 表示被上方同列的合并
 * 格覆盖、渲染时跳过；`text` 是该段首行原文（比较用 trim，展示保留原样）。 */
export type PreviewCell = { text: string; rowSpan: number };

/** 预览表格的合并显示，做到「预览即所得」——与导入后主网格 G2
 * （knowhow-grouping-logic.computeGridSpans）看到的形状一致。
 *
 * 两步，顺序与后端落库路径同构：
 * ① 行标题列 forward-fill：空格继承上一个非空值，镜像后端的
 *    `forward_fill_column`（api.import_table 落库前做的事）。「分组只写一次」
 *    的概念列在文件里是「首行有值、兄弟行留空」，不 fill 预览就会显示成一
 *    串「—」，看着像数据丢了。leading-blank（首行就空、上方无值可继承）保
 *    持空，与后端语义一致。
 * ② 每列相邻同值（trim 后）的连续段合并成一个 rowSpan 起始格，段内其余行
 *    该列 rowSpan=0。未选行标题列（anchorIndex===null）时跳过①、仍做②。
 *
 * 不修改传入的 rows。
 */
export function computePreviewSpans(rows: string[][], anchorIndex: number | null): PreviewCell[][] {
  const width = rows[0]?.length ?? 0;
  if (rows.length === 0 || width === 0) return [];

  const filled = rows.map((row) => [...row]);
  if (anchorIndex !== null && anchorIndex >= 0 && anchorIndex < width) {
    let last = "";
    for (const row of filled) {
      const cell = row[anchorIndex] ?? "";
      if (cell.trim()) last = cell;
      else if (last) row[anchorIndex] = last;
    }
  }

  return filled.map((row, i) =>
    Array.from({ length: width }, (_, c) => {
      const text = row[c] ?? "";
      const key = text.trim();
      if (i > 0 && (filled[i - 1][c] ?? "").trim() === key) {
        return { text, rowSpan: 0 };
      }
      let span = 1;
      for (let j = i + 1; j < filled.length; j++) {
        if ((filled[j][c] ?? "").trim() === key) span++;
        else break;
      }
      return { text, rowSpan: span };
    }),
  );
}
