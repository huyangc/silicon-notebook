// Knowhow 表 / Memory 跨 notebook 传输(复制/移动)— 共享纯 helper + view/API 类型。
// 网络客户端在 knowhow-transfer.ts / memory-transfer.ts,各自 import 本模块的类型与
// body 构造器,自己都不重新声明字段名——wire 契约的唯一真源在这里,靠
// transfer-model.test.mjs 锁死。

import type { NotebookSummary } from "./workspace-model.ts";

export type TransferMode = "copy" | "move";

// 后端 memory_service.transfer() 每条结果的 status(见 backend/app/services/
// memory_service.py):copy 模式成功恒 "copied";move 模式源清理成功 "moved",
// 源清理失败但副本已提交 "copied_source_not_removed"(ok=false 但 new_id 非空,
// 不能等同于「什么都没发生」的 "failed")；memory_id 本身查不到/状态不对/
// 目标校验失败 → "failed"(new_id=null)。knowhow 表传输是单条 200/409,不走这
// 个 per-item 结果数组。
export type TransferStatus = "copied" | "moved" | "failed" | "copied_source_not_removed";

// POST /api/memories/transfer 响应里 results 数组的单项。注意:没有
// `source_deleted` 字段——早前草稿设想过这个字段,但后端从未产出它;
// "源是否被删掉了"要从 `status` 判断(moved=删了,copied_source_not_removed=
// 没删但副本已存在)。不变量(调用方可依赖,后端保证):
//   ok === true  ⟺ error === null
//   status === "failed" ⟺ new_id === null
export type TransferResult = {
  source_id: string;
  new_id: string | null;
  ok: boolean;
  error: string | null;
  status: TransferStatus;
};

/** 目标笔记本候选:排除源自身 + 排除只读(reader)库(只读没有写入权限,不能接收传输)。 */
export const destinationNotebooks = (
  all: readonly NotebookSummary[],
  sourceId: string
): NotebookSummary[] => all.filter((n) => n.id !== sourceId && n.access !== "reader");

/** POST /notebooks/{id}/knowhow/{table_id}/transfer 的请求体。字段名是 wire 契约,勿改。 */
export const knowhowTransferBody = (targetNotebookId: string, mode: TransferMode) => ({
  target_notebook_id: targetNotebookId,
  mode,
});

/** POST /memories/transfer 的请求体。字段名是 wire 契约,勿改。数组做防御性拷贝,
 *  不别名调用方后续可能原地变更的数组。 */
export const memoryTransferBody = (
  memoryIds: readonly string[],
  targetNotebookId: string,
  mode: TransferMode,
  extractKg: boolean
) => ({
  memory_ids: [...memoryIds],
  target_notebook_id: targetNotebookId,
  mode,
  extract_kg: extractKg,
});

// --- AMENDMENT 1: 批量传输的「所选是否同源笔记本」判定(C4 消费)---------------
// DestinationPicker 的 sourceNotebookId 是单数——它只用来在目标候选列表里排除
// 源自身(见上面 destinationNotebooks)。全局 Memory 视图的多选可以跨 notebook；
// 一旦选中项跨了源笔记本,"唯一源"这个前提就不成立了——调用方必须在打开选择器
// 之前用这个纯函数拦截(提示用户改成单一笔记本内多选),不能悄悄拿第一条的
// notebook 当源硬着头皮继续:那会导致"移动"从错误的笔记本删除源、"复制"漏排
// 除真正的源笔记本。单条传输天然只有一个来源,不需要经过这个检查。

/** 一批条目是否共享同一个 notebook_id；是则返回该 id,跨源或空输入返回 null。 */
export const singleSourceNotebookId = (
  items: readonly { notebook_id: string }[]
): string | null => {
  if (items.length === 0) return null;
  const first = items[0].notebook_id;
  return items.every((item) => item.notebook_id === first) ? first : null;
};

// --- AMENDMENT 2: 409 source_cleanup_failed 结构化解析 ----------------------
// knowhow transfer 在「复制已提交、源清理失败」时返回 409,body 形如:
//   { detail: { code: "source_cleanup_failed", new_table_id: "...", message: "..." } }
// (见 backend/app/api/routes.py transfer_knowhow_table 的 SourceCleanupFailed 分支)。
// 通用错误路径会把整个 body 拍扁成一行字符串,丢失 new_table_id——这条信息 C3
// 必须要有(提示"副本已在目标存在,别再盲目重试",并能直接带用户跳过去)。
// 判定逻辑单独抽成纯函数(这里),网络客户端只管把 status/body 转手过来问它、
// 拿到非 null 就抛结构化错误——不在 fetch 包装器里重复写字段判断。

export type CleanupFailure = { newTableId: string; message: string };

const FALLBACK_CLEANUP_MESSAGE = "已复制到目标，但源表未删除，请勿重复重试";

/**
 * 判定一个 HTTP 响应是否是 knowhow transfer 特有的
 * `409 {detail: {code: "source_cleanup_failed", new_table_id, message}}`。
 * 命中 → {newTableId, message};其余任何情况(非 409、非该 code、detail 是普通
 * 字符串、new_table_id 缺失或非字符串、body 根本不是对象……)一律 null,调用方
 * 落回通用错误路径。纯函数,不摸网络——status/body 由调用方(res.status /
 * 已解析的 JSON)转手过来。
 */
export const parseCleanupFailure = (status: number, body: unknown): CleanupFailure | null => {
  if (status !== 409) return null;
  if (typeof body !== "object" || body === null) return null;
  const detail = (body as Record<string, unknown>).detail;
  if (typeof detail !== "object" || detail === null) return null;
  const d = detail as Record<string, unknown>;
  if (d.code !== "source_cleanup_failed") return null;
  if (typeof d.new_table_id !== "string" || !d.new_table_id) return null;
  const message = typeof d.message === "string" && d.message ? d.message : FALLBACK_CLEANUP_MESSAGE;
  return { newTableId: d.new_table_id, message };
};

// --- AMENDMENT 3: 传输结果汇总(C4 消费)-------------------------------------

export type TransferResultsSummary = {
  total: number;
  succeeded: number;
  failed: number;
  // status === "copied_source_not_removed" 的子集:副本已经在目标 notebook 落地,
  // 但源没删掉——不是普通失败,不能引导用户「重试」(会在目标堆出更多重复副本),
  // 需要单独一条「副本已存在,请手动清理源」的提示。
  copiedSourceNotRemoved: TransferResult[];
};

/** 汇总一批 memory transfer 结果:成功/失败计数 + 需要"副本已存在,别再重试"提示的子集。 */
export const summarizeTransferResults = (
  results: readonly TransferResult[]
): TransferResultsSummary => {
  const succeeded = results.filter((r) => r.ok).length;
  const copiedSourceNotRemoved = results.filter((r) => r.status === "copied_source_not_removed");
  return {
    total: results.length,
    succeeded,
    failed: results.length - succeeded,
    copiedSourceNotRemoved,
  };
};

// --- AMENDMENT 4（P2-B，round 6 评审）：批量传输必须预先剔除非 confirmed 项 ---
// 单条卡片操作区的「复制/移动到…」入口天生只在 status==="confirmed" 的卡片上
// 渲染（memory-panel.tsx 里那条 memory.status === "confirmed" 门），但批量选择
// 走 checkbox，选中态与状态无关——用户可以选中 candidate/rejected/deprecated
// 条目一起点「复制/移动到…」。后端 memory_service.transfer() 对这些条目会逐条
// 报 status="failed"（"只能传输 confirmed 状态的 memory"），不是数据丢失，但
// 都是可预见的、本可以在前端拦下来的失败。调用方在打开 DestinationPicker 之
// 前用这个纯函数先筛一遍所选条目，把注定失败的条目挡在请求之外。

/** 只保留 status === "confirmed" 的条目，其余（candidate/rejected/deprecated）
 *  剔除，相对顺序不变。 */
export const confirmedOnly = <T extends { status: string }>(items: readonly T[]): T[] =>
  items.filter((item) => item.status === "confirmed");
