// 删除整本笔记本的一天窗口 —— 用户文案(纯函数)。
//
// 规矩:时刻一律取服务端的 `delete_after`,按浏览器本地时区写成绝对时间;数字一律取自
// 服务端(告知投影里的 memory_count / transferable_count、转移的逐条结果),客户端不自己
// 算。确认删除的弹窗仍不显示成员人数与记忆条数(不向主人泄露谁在这里存了多少记忆)。
// 转移是**移动**:转走的记忆离开这本笔记本,主人之后撤销删除也不会回来——凡是说「撤销后
// 恢复原样」的地方都要把这一点写明。

/** 永久删除的时刻,本地时区绝对时间:「2026年10月23日 08:00」。解析不了返回空串。 */
export function formatDeleteAt(iso: string): string {
  const at = new Date(iso);
  if (!Number.isFinite(at.getTime())) return "";
  return `${at.getFullYear()}年${at.getMonth() + 1}月${at.getDate()}日 `
    + `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
}

/** 截止是否已过(本地时钟;服务端是权威,过了就只显示「正在永久删除」并重新拉取)。 */
export function deletionDue(iso: string, nowMs: number): boolean {
  const at = Date.parse(iso);
  return Number.isFinite(at) && at <= nowMs;
}

const named = (name: string) => `“${name || "未命名笔记本"}”`;

// ------------------------------------------------------------------ 确认删除

export const DELETE_DIALOG_TITLE = "删除笔记本";

/** 主人确认弹窗的正文。 */
export function deleteConfirmText(name: string): string {
  return `确定删除${named(name)}吗？确认后，这本笔记本立即对所有人不可用：`
    + "你和其他能读它的人都打不开、搜不到，问答、Agent 和分享链接也用不到它。"
    + "它会再保留一天，之后永久删除其中的一切：来源、深度报告、会话，以及每个人各自保存在这里的记忆。"
    + "这一天里，在这里有记忆的人会收到通知，可以导出自己的记忆，或把它们转移到自己的笔记本；"
    + "你也可以随时撤销删除。撤销后其余一切恢复原样，但已经被转移走的记忆不会回到这本笔记本。";
}

/** 有笔记本把它挂为参考库时的提醒。 */
export function deleteMountText(count: number): string {
  return `${count} 个笔记本正在把它作为参考库。确认后它们立即用不到这条参考库；撤销删除可以恢复，一天后永久失去。`;
}

/** 确认成功后的提示；操作入口由短暂提示和通知面板提供。 */
export function deleteScheduledText(name: string, deleteAfter: string | null): string {
  // 回答里没有可读的截止时刻也不编造；通知详情会独立重新拉取。
  const when = deleteAfter ? `${formatDeleteAt(deleteAfter)}起永久删除` : "一天后永久删除";
  return `已安排删除${named(name)}。它现在对所有人都不可用，${when}。`;
}

// ------------------------------------------------------------- 列表区块 / 说明框

export const DELETIONS_BLOCK_TITLE = "即将永久删除的笔记本";
export const DELETIONS_UNAVAILABLE_TEXT = "暂时读不到即将删除的笔记本，请稍后重试。";
export const DELETION_DUE_TEXT = "删除时间已到，正在永久删除。";
export const NOTICE_DIALOG_TITLE = "这本笔记本已被删除";

/** 区块一行(也是说明框正文)的那句话。 */
export function deletionNoticeText(notice: {
  name: string; delete_after: string; is_owner: boolean; memory_count: number;
}): string {
  const at = formatDeleteAt(notice.delete_after);
  if (notice.is_owner) {
    return `${named(notice.name)}已安排删除，现在对所有人都不可用。${at}起永久删除，在此之前可以撤销。`;
  }
  const memory = notice.memory_count > 0
    ? `，包括你在这里的 ${notice.memory_count} 条记忆；在此之前可以导出或转移到你的笔记本。`
    : "。";
  return `所有者删除了${named(notice.name)}，它现在打不开了。${at}起会永久删除其中的一切${memory}`;
}

/** 有记忆不能转移(候选、正在晋升审批的)时,按钮旁的那句。 */
export function untransferableText(count: number): string {
  return `${count} 条记忆无法转移，请先导出。`;
}

// ------------------------------------------------------------------- 撤销

export const CANCEL_DONE_TEXT = "已撤销删除，笔记本已恢复；已经被转移走的记忆不会回来。";
export const CANCEL_DUE_TEXT = "删除时间已到，已无法撤销。";
export const CANCEL_GONE_TEXT = "这本笔记本已不存在。";
export const CANCEL_FAILED_TEXT = "撤销没有成功，请重试。";

// ------------------------------------------------------------------- 导出

export const EXPORT_CLOSED_TEXT = "删除时间已到，记忆已无法导出。";
export const EXPORT_NOTHING_TEXT = "这里已经没有你的记忆可以导出。";
export const EXPORT_FAILED_TEXT = "导出失败，请重试。";

/** 「导出我的记忆」按钮的文案(带条数)。 */
export const exportLabel = (count: number): string => `导出我的记忆（${count} 条）`;

// ------------------------------------------------------------------- 转移

export const TRANSFER_LABEL = "转移到我的笔记本";
/** 转移之前必须看到的那句:这是移动,不会回来。 */
export const TRANSFER_WARNING_TEXT =
  "转移后这些记忆会离开这本笔记本，即使所有者撤销删除也不会回来。";
export const TRANSFER_NO_TARGET_TEXT = "你还没有自己的其他笔记本可以接收这些记忆，请先导出。";
export const TRANSFER_CLOSED_TEXT = "删除时间已到，记忆已无法转移。";
export const TRANSFER_GONE_TEXT = "这本笔记本或目标笔记本已经读不到了。";
export const TRANSFER_FAILED_TEXT = "转移没有成功，请重试。";
export const TRANSFER_BUSY_TEXT = "另一次转移正在进行，请等它结束后再试。";
/** 转移途中主人撤销了删除(409 notebook_not_pending_delete):库回来了,没转的留在原处。 */
export const TRANSFER_RESTORED_TEXT = "所有者已撤销删除，这本笔记本已恢复，剩下的记忆留在原处。";

/** 分页转移进行中的进度:`moved` 只数真正转走的,失败的另计;`total` = 已处理 + 剩余。 */
export function transferProgressText(moved: number, failed: number, total: number): string {
  const head = `已转移 ${moved} / ${total} 条`;
  return failed > 0 ? `${head}，${failed} 条没有转移成功` : head;
}

/** 转移结束或中途停下时的如实交代:转走几条、几条没转成(还在这本笔记本里)、几条没处理;
 *  停下有原因时原因在前。数字全取自服务端逐条结果与剩余条数。 */
export function transferSummaryText(
  moved: number, failed: number, unprocessed: number, target: string, reason = "",
): string {
  const parts = [moved > 0 ? `已把 ${moved} 条记忆转移到${named(target)}` : "没有转移任何记忆"];
  if (failed > 0) parts.push(`${failed} 条没有转移成功（仍在这本笔记本里，请先导出）`);
  if (unprocessed > 0) parts.push(`${unprocessed} 条未处理`);
  return `${reason}${parts.join("，")}。`;
}

/** 全部处理完的结果(没有未处理的)。 */
export function transferDoneText(moved: number, failed: number, target: string): string {
  return transferSummaryText(moved, failed, 0, target);
}

// ------------------------------------------------------------------- 铃铛

export function bellDeletionLabel(name: string, deleteAfter: string): string {
  return `${named(name)}将于 ${formatDeleteAt(deleteAfter)} 永久删除`;
}

export function bellDeletionHint(isOwner: boolean, memoryCount: number): string {
  if (isOwner) return "可以撤销删除";
  return memoryCount > 0 ? `可以先导出你的 ${memoryCount} 条记忆` : "";
}

// ----------------------------------------------------------------- 退出群组

export const LEAVE_PENDING_TAG = "即将删除";
export function leavePendingDeleteText(deleteAfter: string): string {
  return `所有者已删除这本笔记本，${formatDeleteAt(deleteAfter)}起永久删除；退出群组后就无法再导出，请先导出。`;
}
