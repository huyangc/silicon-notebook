/**
 * source-reparse-request.ts
 *
 * 一次「重新解析」请求(`POST /notebooks/{id}/sources/reparse`)从发出到落地的全部副作用——体检的
 * 批量修复与图谱分析问题清单的「重新解析…」共用这一条路径(page.tsx 把真实的请求、提示、刷新
 * 接进来)。
 *
 * 归属(docs/architecture.md 的来源库归属边界):请求在路上时用户可能切了笔记本或账号。回来的
 * 结果属于被放弃的那个工作区——不在新工作区弹提示、不报错、不刷新体检与来源列表、不开修复轮询
 * 窗口,只回报 "abandoned"。`stillOwned` 由调用方给:发起时捕获的来源库 owner 是否仍是当前的
 * (笔记本 + 账号;来源库的 submitFollowedReparse 另外核对写权限)。
 */

/**
 * - started:至少排上一篇;
 * - none:一篇都没排上(样本过时、来源已删、后端按类型排除),是合法结果;
 * - failed:请求失败(已经报错);
 * - abandoned:等待期间笔记本、账号或写权限变了,这次结果不归当前工作区,什么都没显示、没刷新。
 */
export type ReparseOutcome = "started" | "none" | "failed" | "abandoned";

export type ReparseRequestEffects = {
  reparse: (notebookId: string, sourceIds: string[]) => Promise<{ scheduled: readonly string[] }>;
  toast: (text: string) => void;
  /** 重拉体检与来源列表当前页,把陈旧的计数与状态标签拨正。 */
  refresh: () => void;
  /** 开一段有界的体检轮询窗口(修复是后台作业,计数不会立即下降)。 */
  armRepairPoll: () => void;
  reportError: (error: unknown) => void;
};

export async function requestReparse(
  notebookId: string,
  sourceIds: string[],
  stillOwned: () => boolean,
  effects: ReparseRequestEffects,
): Promise<ReparseOutcome> {
  if (sourceIds.length === 0) return "none";
  let scheduled: readonly string[];
  try {
    scheduled = (await effects.reparse(notebookId, sourceIds)).scheduled;
  } catch (error) {
    if (!stillOwned()) return "abandoned";
    effects.reportError(error);
    return "failed";
  }
  if (!stillOwned()) return "abandoned";
  // 一篇都没排上是**合法**结果:样本是体检那一刻的快照,期间来源可能已被删除,或后端按类型
  // 排除(memory/knowhow 合成源)。此时既不能说「已开始重新解析 0 篇」,也不能 arm 轮询/置忙碌
  // 位——后台根本没活在跑,按钮会白锁到窗口结束(codex 第 2 轮 P2)。只重拉体检与来源列表。
  if (scheduled.length === 0) {
    effects.toast("这些来源已不在或无需重新解析；已刷新状态");
    effects.refresh();
    return "none";
  }
  effects.toast(`已开始重新解析 ${scheduled.length} 篇来源；完成后会自动更新`);
  effects.armRepairPoll();
  // 让来源列表的状态标签也及时反映(分析中/已就绪)。
  effects.refresh();
  return "started";
}
