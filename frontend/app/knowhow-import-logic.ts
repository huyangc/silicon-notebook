// Knowhow 表导入向导 — 纯逻辑（无 JSX，可被 knowhow-import.test.mjs 直接
// import）。knowhow-import.tsx 含 JSX，Node 原生 TS 类型剥离不支持 .tsx
// （仅 .ts/.mts/.cts 可被 node --test 直接 import），故本文件把 payload
// 组装 / concept 校验 / 角色下拉选项 / 默认标题推导 / 文件类型校验 / 错误
// 文案抽取 这些可测纯逻辑单独抽出，镜像 knowhow-panel.tsx <-> -logic.ts
// 的既有拆分方式。knowhow-import.tsx 只调用本文件导出的函数，不重复实现
// 判断逻辑。

import { ROLE_LABELS, type Role, type KnowhowColumnInput, type KnowhowPreviewColumn } from "./knowhow-model.ts";

// --- 步骤①：文件选择 -----------------------------------------------------------

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

// --- 步骤③：提交 -----------------------------------------------------------------

// 后端错误展示：knowhow-model.ts 的 apiFetch()（Task 7 产物，本任务文件
// 白名单不含该文件，不可改）在非 2xx 时固定抛
// `Error(`${status} ${bodyText}`)`，没有做 FastAPI `{"detail": "..."}"`
// 的展开（对照 auth.ts 里已有的同类 unwrap 写法）。本函数尽量抠出纯中文
// detail 文本；解析失败（响应体不是 JSON，如网络层错误）时退回去掉状态码
// 前缀后的原文；两者都拿不到有效文本时才用调用方给的兜底文案——不让用户
// 看到裸状态码/JSON 花括号噪音，同时不吞掉后端真正想说的中文错误。
export function extractErrorMessage(err: unknown, fallback = "操作失败，请重试"): string {
  const raw = err instanceof Error ? err.message : String(err ?? "");
  const withoutStatus = raw.replace(/^\d{3}\s/, "");
  try {
    const parsed = JSON.parse(withoutStatus);
    if (parsed && typeof parsed.detail === "string" && parsed.detail.trim()) {
      return parsed.detail;
    }
  } catch {
    // 响应体不是 JSON（网络错误/非结构化文本等），走下面的原文兜底。
  }
  const trimmed = withoutStatus.trim();
  return trimmed || fallback;
}
