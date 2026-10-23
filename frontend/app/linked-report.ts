// 问答「就此追问」的关联报告：纯函数与稳定协议值 —— 单测于 tests/unit/linked-report.test.mjs。
//
// 状态（标签、判词、就地说明）只归 use-ask-session.ts 所有；这里只放它和呈现层共用的
// 推导：从历史/在途任务恢复粘性标签、把一轮回执说成一句话、把报告生成时冻结的检索范围
// 换成勾选状态。没有 I/O，不读 React。

import {
  defaultBaseScopeSelection,
  defaultSourceScopeSelection,
  selectedBaseIds,
  type BaseScopePayload,
  type BaseScopeSelection,
  type SourceScopePayload,
  type SourceScopeSelection,
} from "./source-scope.ts";
import type { ReportFollowupScopeT } from "./report-model.ts";
import type {
  ConversationDetail,
  LinkedReportReceipt,
  LinkedReportRef,
} from "./workspace-model.ts";

/**
 * 输入框上方那枚「关联报告」标签指向的报告。`title` 是 `reports.question` 的整串快照
 * （不截断；界面只做单行省略并把全文放进 `title` 属性）。从读取出错的回执或已删除报告的
 * 在途任务恢复时可能是空串，显示时用不含题目的兜底文案。
 */
export type LinkedReportTag = Readonly<{ id: string; title: string }>;

/** 就地预检的判词：`readable` 为假时 `reason` 是可原样展示的中文原因。 */
export type LinkedReportVerdict = Readonly<{ readable: boolean; reason: string }>;

/** 标签、回答方式或两维范围变了之后，等这么久没有再变才发一次就地预检。 */
export const LINKED_REPORT_CHECK_DEBOUNCE_MS = 300;

/**
 * 标签旁那句就地说明（范围过滤、报告已不存在）自己的清除计时。8 秒：这几句比按钮上的
 * 「已复制」长，要读得完；又不会挂到用户已经在问下一个问题的时候。
 */
export const LINKED_REPORT_NOTE_MS = 8000;

/** 就地预检得知报告已不存在（404）时，标签被移除，旁边留这句话。 */
export const LINKED_REPORT_GONE_NOTE = "关联的报告已不存在，已移除";

/**
 * 「就此追问」没能按报告生成时的范围勾选、退回全选时的说明。成因不止一种（冻结的选择
 * 已无可用部分，或无法按原样恢复），只说现状、不猜原因。
 */
export const FOLLOWUP_SCOPE_FELL_BACK_NOTE = "未能按报告生成时的检索范围勾选，已改为全选";

function tagFrom(reportId: unknown, title: unknown): LinkedReportTag | null {
  if (typeof reportId !== "string" || !reportId) return null;
  return { id: reportId, title: typeof title === "string" ? title : "" };
}

/** 一个 `LinkedReportRef`（会话摘要/详情/在途任务上的报告指针）转成标签。 */
export function linkedReportFromRef(ref: LinkedReportRef | null | undefined): LinkedReportTag | null {
  return ref ? tagFrom(ref.report_id, ref.title) : null;
}

/** 这一轮挂的报告（取自它的回执）；没挂报告的轮次为 null。 */
export function linkedReportFromTurn(
  turn: { response: { linked_report?: LinkedReportReceipt | null } } | null | undefined,
): LinkedReportTag | null {
  const receipt = turn?.response.linked_report;
  return receipt ? tagFrom(receipt.report_id, receipt.title) : null;
}

/** 在途任务挂的报告；没挂时为 null。题目是报告当前的题目，已删除时为空串。 */
export function linkedReportFromActiveJob(
  job: ConversationDetail["active_job"] | null | undefined,
): LinkedReportTag | null {
  return linkedReportFromRef(job?.linked_report);
}

/**
 * 重开一个会话时粘性标签恢复成什么：有在途任务就是**它**挂的报告（它没挂就是没有——
 * 那一问提交时用户已经移除了标签，移除在那一刻落成了事实）；否则是最后一轮回执里的
 * 报告。移除了标签但还没提问就重开，标签会回来：与回答方式、检索档位从最后一轮恢复
 * 同一口径。
 */
export function restoredLinkedReport(
  detail: Pick<ConversationDetail, "turns" | "active_job" | "linked_report">,
): LinkedReportTag | null {
  if (detail.active_job) return linkedReportFromActiveJob(detail.active_job);
  const last = detail.turns[detail.turns.length - 1];
  return linkedReportFromTurn(last) ?? linkedReportFromRef(detail.linked_report);
}

/**
 * 就地预检的身份：报告、这次会提交的回答方式、两维范围。任何一项变了，旧判词就不再
 * 描述这次提交（视图据此把它藏起来），也要重新预检。
 */
export function linkedReportCheckKey(
  reportId: string,
  mode: string,
  sourceScope: SourceScopePayload,
  baseScope: BaseScopePayload,
): string {
  return JSON.stringify([
    reportId,
    mode,
    sourceScope.mode,
    sourceScope.source_ids,
    baseScope.mode,
    baseScope.notebook_ids,
  ]);
}

/**
 * 在途轮次与会话徽标上的那句「关联报告：<题目>」。题目为空（读取出错的回执、报告已删除的
 * 在途任务）时只说「关联报告」——不编一个题目，也不猜它为什么不在。
 */
export function linkedReportLabel(title: string): string {
  return title ? `关联报告：${title}` : "关联报告";
}

/**
 * 来源栏「关联报告」分组里那枚标签上的字：分组标题已经说了「关联报告」，标签只放题目。
 * 题目为空时说「（无题目）」——同样不编题目、不猜原因。
 */
export function linkedReportTagText(title: string): string {
  return title || "（无题目）";
}

/** 回答下方回执里的报告名：「关联报告《题目》」；题目为空时只说「关联报告」。 */
export function linkedReportReceiptName(title: string): string {
  return title ? `关联报告《${title}》` : "关联报告";
}

function count(value: number | undefined): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

/**
 * 一轮回执的展示句（不含报告题目：题目由调用方放在前面，读取出错的回执题目为空串）。
 *
 * - 跳过：后端写好的原因原样展示（「本轮不读取这份报告：…」）；缺失时用中性兜底。
 * - 读入：读了执行摘要（是否截断）与哪几节，再加复原引用证据的计数。只说读了什么，
 *   不推测没读的那几节为什么没读（回执里分不出「超出篇幅」与「没有单独正文」）。
 */
export function linkedReportReceiptText(receipt: LinkedReportReceipt): string {
  if (receipt.status !== "read") {
    return receipt.reason || "本轮没有读取这份报告";
  }
  const sections = receipt.sections ?? [];
  const total = sections.length;
  const read = sections.filter((section) => section.read).map((section) => section.index);
  const truncated = Boolean(receipt.summary_truncated);
  let text: string;
  if (!truncated && read.length === total) {
    text = "已读取报告全文";
  } else {
    const opening = "已读取" + (truncated ? "执行摘要（已截断）" : "执行摘要");
    if (total === 0) text = opening;
    else if (read.length === total) text = opening + `与全部 ${total} 节`;
    else if (read.length > 0) text = opening + "与第 " + read.join("、") + ` 节（共 ${total} 节）`;
    else text = opening + `与各节标题（共 ${total} 节，各节正文未读入）`;
  }
  const restored = count(receipt.evidence_restored);
  const gone = count(receipt.evidence_gone);
  const overBudget = count(receipt.evidence_over_budget);
  if (!restored && !gone && !overBudget) return text;
  let note = `；复原引用证据 ${restored} 条`;
  if (gone) note += `，${gone} 条已失效`;
  if (overBudget) note += `，${overBudget} 条超出篇幅未读入`;
  return text + note;
}

function sourceSelectionOf(scope: SourceScopePayload): SourceScopeSelection {
  return { allSelected: scope.mode === "exclude", ids: new Set(scope.source_ids) };
}

/**
 * 「就此追问」切换检索范围要用的两维勾选状态，以及标签旁的说明（没什么要说时为空串）。
 *
 * 参考库那一维再按**此刻**挂载的库过滤一遍：服务端判断挂载与这里的挂载列表可能前后
 * 相差一次卸载，被滤掉的库与服务端报来的一起计数。过滤之后两维都没有可选的，或服务端
 * 已经退回全选（`fell_back_to_all`），两维都回到全选并说明——空范围会把问答框整个锁住。
 */
export function followupScopeSelections(
  scope: ReportFollowupScopeT,
  mountedBaseIds: readonly string[],
): { source: SourceScopeSelection; base: BaseScopeSelection; note: string } {
  const mounted = new Set(mountedBaseIds);
  const source = sourceSelectionOf(scope.source_scope);
  let clientDroppedLibraries = 0;
  let base: BaseScopeSelection;
  if (scope.base_scope.mode === "exclude") {
    base = {
      allSelected: true,
      ids: new Set(scope.base_scope.notebook_ids.filter((id) => mounted.has(id))),
    };
  } else {
    const kept = scope.base_scope.notebook_ids.filter((id) => mounted.has(id));
    clientDroppedLibraries = new Set(scope.base_scope.notebook_ids).size - new Set(kept).size;
    base = { allSelected: false, ids: new Set(kept) };
  }
  const localEmpty = !source.allSelected && source.ids.size === 0;
  const baseEmpty = selectedBaseIds(base, mountedBaseIds).length === 0;
  if (scope.fell_back_to_all || (localEmpty && baseEmpty)) {
    return {
      source: defaultSourceScopeSelection(),
      base: defaultBaseScopeSelection(),
      note: FOLLOWUP_SCOPE_FELL_BACK_NOTE,
    };
  }
  const droppedSources = count(scope.dropped_sources);
  const droppedLibraries = count(scope.dropped_libraries) + clientDroppedLibraries;
  const dropped: string[] = [];
  if (droppedSources) dropped.push(`${droppedSources} 份资料`);
  if (droppedLibraries) dropped.push(`${droppedLibraries} 个参考库`);
  const note = dropped.length
    ? "已按报告生成时的检索范围勾选；其中 " + dropped.join("、") + "现在无法勾选，已略去"
    : "";
  return { source, base, note };
}
