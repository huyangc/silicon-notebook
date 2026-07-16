// Knowhow 表 — Task 11「代码附件 UI」的纯逻辑（无 JSX，供 knowhow-code.test.mjs
// 直接 import）。knowhow-code.tsx 含 JSX，Node 原生 TS 类型剥离不支持 .tsx
// （仅 .ts/.mts/.cts 可被 node --test 直接 import），故本文件把三态新鲜度的
// 展示映射、抽屉分节 chip 的渲染门控、行级 code map 缺席补位、保存前校验与
// 复制按钮分支决策这些可测纯逻辑单独抽出，镜像既有 knowhow-panel.tsx <->
// knowhow-panel-logic.ts / knowhow-cell-editor.tsx <->
// knowhow-cell-editor-logic.ts 的拆分方式。knowhow-code.tsx 只调用本文件
// 导出的函数/常量，不重复实现判断逻辑或复制粘贴文案字符串。
//
// 规格⑥-4 原文（设计文档 2026-07-15-knowhow-tables-design.md）："新鲜度读取
// 时推导：附件 hash==格子当前 hash → implemented；不一致 → stale（知识已
// 更新待重审）；无附件 → none"；UI 段："格子/行「代码」徽章（三态）→ 点击
// 浮层查看（等宽、复制、语言标记），用户可编辑保存"。

import { hasUnsavedChanges } from "./knowhow-cell-editor-logic.ts";
import type { CellCodeStatus, KnowhowCellCode } from "./knowhow-model.ts";

// ===========================================================================
// 1. 三态新鲜度 -> 文案/色调/说明句（镜像 knowhow-panel-logic.ts 的
//    PROJECTION_STATUS_LABELS/PROJECTION_STATUS_TONE 既有写法）
// ===========================================================================

// 任务简报原文三态友好文案（规格一致、无黑话）。
export const CODE_STATUS_LABELS: Record<CellCodeStatus, string> = {
  implemented: "已实现",
  stale: "知识已更新",
  none: "未实现",
};

export type CodeStatusTone = "success" | "warning" | "neutral";

// 复用既有 .knowhow-status-badge--{tone} 徽章色板：success=已有的绿（同投影
// 「已同步」）；warning 是本任务新增的琥珀色调（与 .knowhow-role-badge--
// procedure / .kh-procedure-hint 同一套琥珀，视觉上"需要留意但不是错误"）；
// none 不经这个色板渲染徽章（走 kh-code-chip--add 的安静虚线样式），这里仍
// 给出 neutral 只为保持三态映射表的完整性/可测性。
export const CODE_STATUS_TONE: Record<CellCodeStatus, CodeStatusTone> = {
  implemented: "success",
  stale: "warning",
  none: "neutral",
};

// stale 说明句为规格⑥-4/任务简报逐字原文（无句末句号）；implemented/none 是
// 本任务补的对称文案，为保持三句风格一致，同样不加句末句号。
export const CODE_STATUS_EXPLANATIONS: Record<CellCodeStatus, string> = {
  implemented: "这段代码与当前格子内容一致",
  stale: "格子内容已更新，此代码可能过期",
  none: "这一格还没有代码附件",
};

// ===========================================================================
// 2. 抽屉分节 chip 显示门控
// ===========================================================================

// implemented/stale 总是显示（只读成员也能看到"有没有代码"这件事，无需写
// 权限）；none 时只有 canEdit 才显示"添加代码"这一安静入口——纯只读成员看
// 不到（无写权限，点了也无处可去）；none && !canEdit 时不渲染任何东西（任务
// 简报："absent → no chip unless canEdit, in which case a quiet 「添加代码」
// affordance"）。
export function shouldShowCodeChip(status: CellCodeStatus, canEdit: boolean): boolean {
  return status !== "none" || canEdit;
}

// ===========================================================================
// 3. 行级 code map 缺席补 none 占位
// ===========================================================================

const NONE_CODE_VIEW: KnowhowCellCode = { codeText: "", language: "", status: "none", updatedAt: null, updatedBy: null };

// 从 knowhow-model.ts 的 fetchKnowhowRowCodeByColumn 拿到的按列索引 map 里取
// 某一格的展示状态：命中则原样返回；缺席时合成一个 none 占位值——与后端
// build_row_detail"缺席即 none"的既有约定对齐（该端点只把有附件的格子塞进
// code[]，从不为 none 合成条目），组件侧因此永远拿到一个完整 KnowhowCellCode
// 形状去渲染，不需要额外判断"有没有这个 key"。
export function resolveCellCodeView(
  codeByColumn: Record<string, KnowhowCellCode>,
  columnId: string,
): KnowhowCellCode {
  return codeByColumn[columnId] ?? NONE_CODE_VIEW;
}

// ===========================================================================
// 4. 保存前客户端校验 + language 归一化 + 编辑态脏检测
// ===========================================================================

export const CODE_EMPTY_ERROR = "代码内容不能为空";

// 镜像后端 put_cell_code 的 "代码内容不能为空" 校验（app/services/knowhow/
// api.py 同错误文案）——客户端先挡一道，用户不必发起一次注定失败的请求才
// 看到报错（null=可保存）。
export function codeSaveDisabledReason(codeText: string): string | null {
  return codeText.trim() ? null : CODE_EMPTY_ERROR;
}

export function normalizeLanguageInput(input: string): string {
  return input.trim();
}

// 编辑态是否已偏离已保存值——决定 Esc/背景点击要不要弹"确认放弃"（镜像
// knowhow-cell-editor-logic.ts 的 hasUnsavedChanges 判断代码正文，language
// 额外按归一化后的值比较，避免"只多打了几个空格"就被判定为脏）。
export function codeEditorIsDirty(
  codeText: string,
  language: string,
  saved: { codeText: string; language: string },
): boolean {
  return (
    hasUnsavedChanges(codeText, saved.codeText) ||
    normalizeLanguageInput(language) !== normalizeLanguageInput(saved.language)
  );
}

// ===========================================================================
// 4b. 查看态「最近更新」溯源后缀（收尾修复：updated_by 展示）
// ===========================================================================

// 查看态头部「最近更新：{时间}」之后的来源后缀（含前导分隔符，直接拼接）：
// updatedBy 非空时返回 " · 来自 {updatedBy}"，否则空串。行级端点带
// updated_by（Agent 名/用户名），单格 GET/PUT 端点刻意不带（wire 契约，见
// knowhow-model.ts KnowhowCellCode 的类型注释）——后者场景下这里拿到 null，
// 只显示时间、不合成假来源。
export function codeProvenanceSuffix(updatedBy: string | null | undefined): string {
  const name = (updatedBy ?? "").trim();
  return name ? ` · 来自 ${name}` : "";
}

// ===========================================================================
// 5. 复制按钮 clipboard 分支决策
// ===========================================================================

export type CopyStrategy = "clipboard-api" | "execCommand-fallback";

// 纯判断：有没有 navigator.clipboard.writeText 决定走哪条路径——实际 DOM
// 副作用（navigator.clipboard.writeText 本身 / document.execCommand('copy')
// 的隐藏 textarea 兜底）留在组件里执行，由浏览器 QA 验证；这里只测"给定环境
// 探测结果，该走哪条分支"这个决策本身。
export function resolveCopyStrategy(hasClipboardApi: boolean): CopyStrategy {
  return hasClipboardApi ? "clipboard-api" : "execCommand-fallback";
}

// ===========================================================================
// 6. UI 文案常量（组件侧只引用，不内联硬编码字符串——同 knowhow-cell-editor-
//    logic.ts 的既有写法，保证「byte-exact vs 规格/任务简报」可以在这里用
//    简单的相等断言锁住）
// ===========================================================================

export const ADD_CODE_LABEL = "添加代码";
export const COPY_CODE_LABEL = "复制";
export const COPIED_CODE_LABEL = "已复制";
export const EDIT_CODE_LABEL = "编辑";
export const DELETE_CODE_TITLE = "删除代码";
export const DELETE_CODE_CONFIRM_TEXT = "删除这份代码附件？";
export const SAVE_CODE_LABEL = "保存";
export const CANCEL_CODE_LABEL = "取消";
export const CODE_TEXTAREA_PLACEHOLDER = "粘贴或编写代码…";
export const LANGUAGE_INPUT_PLACEHOLDER = "语言标记（可选，如 python / tcl）";
export const NO_LANGUAGE_TAG_TEXT = "未标注语言";
export const CODE_SAVE_ERROR_FALLBACK = "保存失败，请重试";
export const CODE_DELETE_ERROR_FALLBACK = "删除失败，请重试";
export const CODE_CLOSE_GUARD_MESSAGE = "有未保存的代码修改，确定要关闭吗？";
export const CODE_CLOSE_GUARD_CONTINUE_LABEL = "继续编辑";
export const CODE_CLOSE_GUARD_DISCARD_LABEL = "放弃并关闭";
export const CODE_STATUS_LOAD_ERROR = "代码状态加载失败";
