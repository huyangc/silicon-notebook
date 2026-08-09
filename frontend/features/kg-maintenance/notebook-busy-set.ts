/**
 * 按笔记本作用域的忙碌位 —— 后台长任务的共享纯函数。
 *
 * 这三个函数原本长在 kg-relink-status.ts 里（「补上关联」后台化时落的）。「重新合并」
 * 后台化需要**逐字相同**的语义，所以抽到这里共用而不是复制一份：复制出来的第二份不会
 * 有单测钉住「只加自己那一格」「只清自己那一格」这两条边角，而它们正是这套形态存在的
 * 全部理由。
 *
 * 为什么是**集合**而不是裸布尔、也不是只能记一个库的裸字符串：这类任务按笔记本单飞，
 * 用户完全可以点完 A 就切到 B 再点一次。单值形态下后一次认领会覆盖前一次——A 的追踪
 * 被冲掉，回到 A 不再显示忙碌、完成也不再刷新，再点一次还会被服务端 409 误导成「没在
 * 跑」。集合让两次认领都活着，互不干扰。
 */

/** 忙碌位是否作用于**当前打开的**笔记本（按钮 disabled 与轮询 effect 共用这一条判据）。 */
export function busyForNotebook(
  busyNotebookIds: ReadonlySet<string>,
  currentNotebookId: string | null,
): boolean {
  return currentNotebookId !== null && busyNotebookIds.has(currentNotebookId);
}

/** 认领某个笔记本的忙碌位：只加自己那一格，其余库的认领原样保留。 */
export function claimNotebookSlot(
  previous: ReadonlySet<string>,
  notebookId: string,
): Set<string> {
  if (previous.has(notebookId)) return previous as Set<string>;
  const next = new Set(previous);
  next.add(notebookId);
  return next;
}

/**
 * 结算某个笔记本的忙碌位：只清自己那一格。
 *
 * 用户完全可以点完 A 就切到 B 再点一次——A 那条迟到的终态（或失败回执）绝不能把 B 刚
 * 置上的忙碌位一并抹掉，否则 B 的按钮立刻可点、轮询却已经被这一下清掉了。Set 语义天生
 * 满足这一条：删除自己的键从不触碰其余键，不需要像单值形态那样先比较「是不是我」。
 */
export function releaseNotebookClaim(
  previous: ReadonlySet<string>,
  settled: string,
): Set<string> {
  if (!previous.has(settled)) return previous as Set<string>;
  const next = new Set(previous);
  next.delete(settled);
  return next;
}
