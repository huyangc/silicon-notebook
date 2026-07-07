// 待确认中心的「已读 / 关掉」纯逻辑 —— 单测于 pending-actions.test.mjs。
// 待办项是后端实时聚合(治理计数是实时 backlog、非存储事件),故已读/关掉状态放客户端
// (localStorage,按用户),不新增后端表。签名把「实时状态」编码进去:治理项含 count,
// 状态变化 → 新签名 → 关掉过的项会重新出现(诚实反映 backlog 变化)。

import type { PendingItem, DoneToast } from "./pending-center";

export function itemSig(it: PendingItem): string {
  if (it.type === "report_outline") return `report:${it.report_id ?? it.notebook_id ?? ""}`;
  if (it.type === "governance") return `gov:${it.notebook_id}:${it.subtype ?? ""}:${it.count ?? 0}`;
  return `index:${it.notebook_id}:${it.state ?? ""}`;
}

export function doneSig(notebookId: string): string {
  return `done:${notebookId}`;
}

// 当前快照里全部项的签名(含 done),用于「开面板标记已读」与剪枝存储。
export function currentSigs(items: PendingItem[], done: DoneToast[]): string[] {
  return [...items.map(itemSig), ...done.map((d) => doneSig(d.notebook_id))];
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
    done.filter((d) => !seenSet.has(doneSig(d.notebook_id))).length;
  return { visibleItems, visibleDone: done, unread };
}
