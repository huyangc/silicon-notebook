// 「删除知识图谱」终态之后,KG 图谱领域之外还读着旧图谱事实的几块由 page 重拉:
// 来源列表的已分析/待分析徽标、Knowledge 浏览器里那些已被删掉的知识条目(留着会让
// 通过/驳回撞 404)、看板开着时「索引与构建」那几行。
//
// 抽成这个纯编排函数而不是写在 page.tsx 的 effects 字面量里,是为了能不挂整页就把
// 「守卫」钉住:回调到达时用户可能已经切走,或者重拉在途时切走——两种情况下都不能把
// A 的结果写到 B 的界面上,也不能替 B 作废它的 Knowledge 缓存。page.tsx 不得具名导出,
// 所以它住在单独的模块里。

export type KgDeleteDependentsPort<IndexStatus> = {
  /** 此刻真正打开的笔记本(page 的 activeNotebookIdRef),不是渲染闭包里的 state。 */
  activeNotebookId: () => string | null;
  reloadSources: (notebookId: string, guard: () => boolean) => Promise<void>;
  /** 与删除来源同一条作废路径:行、类型计数、分页、查重结果、上下文一起清。 */
  invalidateKnowledge: () => void;
  knowledgeBrowserOpen: () => boolean;
  /** Knowledge 浏览器开着时重新进入(重拉类型与当前列表)。 */
  reenterKnowledge: () => Promise<void>;
  indexPanelOpen: () => boolean;
  fetchIndexStatus: (notebookId: string) => Promise<IndexStatus>;
  applyIndexStatus: (status: IndexStatus) => void;
};

export async function refreshKgDeleteDependents<IndexStatus>(
  notebookId: string,
  guard: () => boolean,
  port: KgDeleteDependentsPort<IndexStatus>,
): Promise<void> {
  const stillCurrent = () => guard() && port.activeNotebookId() === notebookId;
  if (!stillCurrent()) return;
  port.invalidateKnowledge();
  const tasks: Array<Promise<void>> = [
    port.reloadSources(notebookId, stillCurrent).catch(() => {}),
  ];
  if (port.knowledgeBrowserOpen()) {
    tasks.push(port.reenterKnowledge().catch(() => {}));
  }
  if (port.indexPanelOpen()) {
    tasks.push(port.fetchIndexStatus(notebookId).then((status) => {
      if (stillCurrent()) port.applyIndexStatus(status);
    }).catch(() => {}));
  }
  await Promise.all(tasks);
}
