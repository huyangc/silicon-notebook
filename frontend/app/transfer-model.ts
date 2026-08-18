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
//   error_code 只在 error 非空时才可能非空,绝不替代 error(人话文案照旧走
//     error/status;error_code 只是给前端挑分支用的机器令牌)——round 8 P2-A。
export type TransferResult = {
  source_id: string;
  new_id: string | null;
  ok: boolean;
  error: string | null;
  // 机器可判的拒绝原因(backend/app/services/memory_service.py 的
  // `_TransferRejected`)。目前后端只产出一个值:"promotion_proposed"——该
  // Memory 正在公共知识库审批队列中,move 被拒绝,重试永远不会成功,前端得
  // 给一条区别于"请稍后重试"的可操作提示。其余每一种失败(status 不是
  // "failed"的成功/警告结果、或"failed"里除这一种以外的任何原因)一律
  // null——不是"未分类"的占位值,是"这条失败没有专属机器码,只有 error 里
  // 的人话/诊断文案"。留 string(不是窄字面量联合):前端只在乎"是不是
  // promotion_proposed",不需要为后端未来新增的其它 code 同步改这里的类型。
  error_code: string | null;
  status: TransferStatus;
};

/**
 * 目标笔记本候选:排除源自身 + 排除不能接收传输的库。
 *
 * `allowManaged`(群组知识共享 P2-T2 评审 P2-4)决定「组管理员可管理的共享库」算不算
 * 合法目标——它取决于**这次传的是什么**,因为两条 transfer 的目标鉴权不同:
 * - **knowhow 表** transfer 的目标走 `notebook_capability_allowed("knowhow:write")`
 *   = **admin**,组管理员能接收 → knowhow-panel 传 `allowManaged=true`;
 * - **Memory** transfer 的目标走 `user_can_access_notebook` = **owner-only**,组管理员
 *   接收会 404 → memory-panel 传 `allowManaged=false`(默认),不把这些库列进候选,
 *   免得给出一个提交必失败的目标。
 * `can_manage_content` 只有在 `allowManaged` 时才放行(且仍要求它为真);否则一律按
 * `access !== "reader"`(owner-only)的旧口径,逐字复现本参数出现之前的行为。
 */
export const destinationNotebooks = (
  all: readonly NotebookSummary[],
  sourceId: string,
  allowManaged: boolean = false,
): NotebookSummary[] =>
  all.filter(
    (n) =>
      n.id !== sourceId &&
      (n.access !== "reader" || (allowManaged && Boolean(n.can_manage_content))),
  );

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

// --- AMENDMENT 2: 409 source_cleanup_failed/source_changed_kept 结构化解析 --
// knowhow transfer 在"复制已提交、源清理未完成"时返回 409,body 形如:
//   { detail: { code, new_table_id: "...", message: "..." } }
// (见 backend/app/api/routes.py transfer_knowhow_table 的 SourceCleanupFailed
// 分支)。通用错误路径会把整个 body 拍扁成一行字符串,丢失 new_table_id——这条
// 信息 C3 必须要有(提示"副本已在目标存在,别再盲目重试",并能直接带用户跳过
// 去)。判定逻辑单独抽成纯函数(这里),网络客户端只管把 status/body 转手过来
// 问它、拿到非 null 就抛结构化错误——不在 fetch 包装器里重复写字段判断。
//
// round 10 P1-A：code 现在有两个截然不同的值,对应后端 SourceCleanupFailed.
// reason 的两种成因——"source_cleanup_failed"(清理操作本身抛异常,源原封
// 未动,手动删源安全)和新增的"source_changed_kept"(指纹复核未命中,源被
// 有意保留以保护一份并发编辑,绝不能引导删除)。两者的 message 都是后端按各
// 自成因写的人话文案,这里只管原样透传——不需要在这一层区分"具体是哪个
// code":knowhow-panel.tsx 的调用点只是把 message 显示出来,后端已经把"这
// 种情况该怎么办"写进了 message 本身。
export type CleanupFailure = { newTableId: string; message: string };

const FALLBACK_CLEANUP_MESSAGE = "已复制到目标，但源表未删除，请勿重复重试";

const CLEANUP_FAILURE_CODES = new Set(["source_cleanup_failed", "source_changed_kept"]);

/**
 * 判定一个 HTTP 响应是否是 knowhow transfer 特有的
 * `409 {detail: {code, new_table_id, message}}`(code 为
 * "source_cleanup_failed" 或 "source_changed_kept" 之一)。命中 →
 * {newTableId, message};其余任何情况(非 409、code 不是这两者之一、detail
 * 是普通字符串、new_table_id 缺失或非字符串、body 根本不是对象……)一律
 * null,调用方落回通用错误路径。纯函数,不摸网络——status/body 由调用方
 * (res.status / 已解析的 JSON)转手过来。
 */
export const parseCleanupFailure = (status: number, body: unknown): CleanupFailure | null => {
  if (status !== 409) return null;
  if (typeof body !== "object" || body === null) return null;
  const detail = (body as Record<string, unknown>).detail;
  if (typeof detail !== "object" || detail === null) return null;
  const d = detail as Record<string, unknown>;
  if (typeof d.code !== "string" || !CLEANUP_FAILURE_CODES.has(d.code)) return null;
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
  // 但源没删掉——不是普通失败,不能引导用户笼统地「重试」(会在目标堆出更多重复
  // 副本)。这是个超集,下面的 sourceChanged 是它按 error_code 进一步细分出的
  // 子集(round 10 P1-B):两种成因需要截然不同的指引,不能共用一条消息。
  copiedSourceNotRemoved: TransferResult[];
  // copiedSourceNotRemoved 的子集:error_code === "source_changed"(round 10
  // P1-B)——源不是清理失败,而是在复制完成后被并发编辑/状态变更/提交了审批
  // 提案,是被有意保留下来保护这份改动的。绝不能引导用户删除源(会连带丢失
  // 那份改动);正确的下一步是核对目标里的旧副本、删除它,或重新发起一次
  // 搬迁。copiedSourceNotRemoved 减去这个子集,剩下的就是真正的清理失败
  // (error_code === "cleanup_error")——那种情况下源确实原封未动,弃用/删除
  // 都安全,调用方直接用 copiedSourceNotRemoved.length - sourceChanged.length
  // 算,不需要再单独一个字段(同 plainFailedCount 的既有算法风格)。
  sourceChanged: TransferResult[];
  // status === "failed" 且 error_code === "promotion_proposed" 的子集(round 8
  // P2-A):这条 Memory 正在公共知识库审批队列里,move 被拒绝——同样不是能靠
  // "重试"解决的普通失败(在提案被审批中心处理完之前,重试永远还是这个结果),
  // 需要单独一条「去待确认中心处理提案」的可操作提示,不能和其它失败一起被
  // 压进"N 条失败,请稍后重试"。
  promotionBlocked: TransferResult[];
};

/** 汇总一批 memory transfer 结果:成功/失败计数 + 「副本已存在,别再重试」/
 *  「正在审批中,别再重试」两个不可重试子集(理由/文案在各自渲染点不同,
 *  分开两个字段而不是合并成一个"不可重试"桶)。 */
export const summarizeTransferResults = (
  results: readonly TransferResult[]
): TransferResultsSummary => {
  const succeeded = results.filter((r) => r.ok).length;
  const copiedSourceNotRemoved = results.filter((r) => r.status === "copied_source_not_removed");
  const sourceChanged = copiedSourceNotRemoved.filter((r) => r.error_code === "source_changed");
  const promotionBlocked = results.filter(
    (r) => r.status === "failed" && r.error_code === "promotion_proposed"
  );
  return {
    total: results.length,
    succeeded,
    failed: results.length - succeeded,
    copiedSourceNotRemoved,
    sourceChanged,
    promotionBlocked,
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
