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
  // 进行中的提问按 job 认身份。签名里**不带**已进行时长:那个值每 30 秒就变一次,
  // 带上它等于每半分钟给这条待办换一个新身份,关掉过的项会一直复活。job 只有
  // running 一个在途态,终态即整条消失,所以身份不需要再编码状态。
  if (it.type === "ask") return `ask:${it.job_id ?? ""}`;
  return `index:${it.notebook_id}:${it.state ?? ""}`;
}

/** 「已进行多久」——纯函数,时钟由调用方传入(组件测试因此不依赖真实时间)。
 *
 * `askedAt` 解析不出来(空串、旧行、脏值)就返回空串,让调用方整段省略这一截,
 * 而不是显示一个从 1970 年算起的荒谬时长。未来时刻(客户端时钟快于服务端)同样
 * 归零处理 —— 显示「刚刚」而不是负数。
 */
export function askElapsedLabel(askedAt: string, nowMs: number): string {
  const started = Date.parse(askedAt || "");
  if (!Number.isFinite(started)) return "";
  const seconds = Math.floor((nowMs - started) / 1000);
  if (seconds < 60) return "刚刚开始";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `已进行 ${minutes} 分钟`;
  return `已进行 ${Math.floor(minutes / 60)} 小时`;
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
  // 「进行中的提问」刻意**不计入未读徽标**,与其它待办项分岔。它是用户自己几秒前
  // 发起的动作,不是送到他面前的消息:算进未读的话,每问一个问题铃铛就亮一次红点,
  // 徽标从「有东西等你处理」退化成常态噪音。它仍然可见、可关掉、终态即消失。
  // 索引构建中 / 论文补全中那两类仍然计未读——那是用户没发起、可能已经忘了的后台
  // 活,提醒他一次是有价值的;这条分岔正是这个差别。
  const unread =
    visibleItems.filter((it) => it.type !== "ask" && !seenSet.has(itemSig(it))).length +
    done.filter((d) => !seenSet.has(doneSig(d.notebook_id, d.kind))).length;
  return { visibleItems, visibleDone: done, unread };
}
