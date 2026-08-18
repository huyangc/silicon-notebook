// 待确认中心的「已读 / 关掉」纯逻辑 —— 单测于 pending-actions.test.mjs。
// 待办项是后端实时聚合(治理计数是实时 backlog、非存储事件),故已读/关掉状态放客户端
// (localStorage,按用户),不新增后端表。签名把「实时状态」编码进去:治理项含 count,
// 状态变化 → 新签名 → 关掉过的项会重新出现(诚实反映 backlog 变化)。

import type { PendingItem, DoneToast } from "./pending-center";


export function doneMessage(
  event: string,
  message: {
    notebook_name?: string;
    stored?: number;
    not_paper?: number;
  },
): string | null {
  if (event === "index_done") {
    return `「${message.notebook_name || ""}」索引构建完成,点击查看`;
  }
  if (event !== "paper_meta_done") return null;
  const notebook = message.notebook_name || "该笔记本";
  const stored = message.stored ?? 0;
  const notPaper = message.not_paper ?? 0;
  if (stored > 0 && notPaper > 0) {
    return `「${notebook}」论文信息补全完成,已补全 ${stored} 篇,另有 ${notPaper} 篇非论文,点击查看`;
  }
  if (stored === 0 && notPaper > 0) {
    return `「${notebook}」论文信息已核对完成,${notPaper} 篇均非论文、无需补全,点击查看`;
  }
  return `「${notebook}」论文信息补全完成,已补全 ${stored} 篇,点击查看`;
}

export function itemSig(it: PendingItem): string {
  if (it.type === "report_outline") return `report:${it.report_id ?? it.notebook_id ?? ""}`;
  if (it.type === "governance") return `gov:${it.notebook_id}:${it.subtype ?? ""}:${it.count ?? 0}`;
  // 共享申请按组分组,签名带 count:待审批数变化 → 新签名 → 关掉过的项重新出现
  // (诚实反映积压变化,与治理项同一手法)。它没有 notebook 维度。
  if (it.type === "share_request") return `share_req:${it.group_id ?? ""}:${it.count ?? 0}`;
  if (it.type === "paper_meta") return `paper_meta:${it.notebook_id}:${it.state ?? ""}`;
  return `index:${it.notebook_id}:${it.state ?? ""}`;
}

// kind 可选,省略时与旧签名(仅 notebook_id)保持一致(向后兼容)。传入 kind 供两种完成
// 事件(index_done / paper_meta_done)在同一 notebook 上共存时各自拥有独立签名——否则
// 两者会撞签名,导致「已读/剪枝」把其中一个误当另一个的状态处理。
export function doneSig(notebookId: string, kind?: string): string {
  return kind ? `done:${notebookId}:${kind}` : `done:${notebookId}`;
}

// 当前快照里全部项的签名(含 done),用于「开面板标记已读」与剪枝存储。
export function currentSigs(items: PendingItem[], done: DoneToast[]): string[] {
  return [...items.map(itemSig), ...done.map((d) => doneSig(d.notebook_id, d.kind))];
}

// 把存储的签名集合剪到「当前仍存在」的子集:限制大小 + 让已消失/已变化的项复活。
export function pruneSigs(stored: readonly string[], present: readonly string[]): string[] {
  const set = new Set(present);
  return stored.filter((s) => set.has(s));
}

export type PendingView = {
  visibleItems: PendingItem[]; // 关掉(dismissed)的快照项被隐藏
  visibleDone: DoneToast[];    // done 由 hook 状态直接增删,这里透传
  unread: number;              // 徽标:可见且未读(未 seen)的数量
};

export function pendingView(
  items: PendingItem[],
  done: DoneToast[],
  seen: readonly string[],
  dismissed: readonly string[],
): PendingView {
  const seenSet = new Set(seen);
  const dismSet = new Set(dismissed);
  const visibleItems = items.filter((it) => !dismSet.has(itemSig(it)));
  const unread =
    visibleItems.filter((it) => !seenSet.has(itemSig(it))).length +
    done.filter((d) => !seenSet.has(doneSig(d.notebook_id, d.kind))).length;
  return { visibleItems, visibleDone: done, unread };
}
