import { workspaceRequestIsCurrent } from "./workspace-transitions.ts";


export type SourceDeleteRefreshOwner = {
  notebookId: string;
  workspaceEpoch: number;
  generation: number;
};


/**
 * DELETE 可能跨越 A → B → A。响应时只要用户已经回到发起删除的 notebook，当前
 * 页面就必须幂等移除该来源；发起请求时的旧 epoch 不能否决这次收敛。刷新归属则从
 * 响应时的当前 epoch 开始计算，避免随后再切库时把校准结果落进别的工作区；同库
 * generation 另行保证并发删除中只有最后一次成功响应启动的校准能够提交。
 */
export function claimSourceDeleteRefresh(
  deletedFromNotebookId: string,
  activeNotebookId: string | null,
  currentWorkspaceEpoch: number,
  generation: number,
): SourceDeleteRefreshOwner | null {
  if (deletedFromNotebookId !== activeNotebookId) return null;
  return {
    notebookId: deletedFromNotebookId,
    workspaceEpoch: currentWorkspaceEpoch,
    generation,
  };
}


export function ownsSourceDeleteRefresh(
  owner: SourceDeleteRefreshOwner,
  activeNotebookId: string | null,
  currentWorkspaceEpoch: number,
  latestGeneration: number,
): boolean {
  return workspaceRequestIsCurrent(
    false,
    owner.workspaceEpoch,
    currentWorkspaceEpoch,
    owner.notebookId,
    activeNotebookId,
  ) && owner.generation === latestGeneration;
}


export function filterDeletedSourceItems<T extends { id: string }>(
  items: T[],
  deletedIds: ReadonlySet<string> | undefined,
): { items: T[]; removedCount: number } {
  if (!deletedIds || deletedIds.size === 0) return { items, removedCount: 0 };
  const visible = items.filter((item) => !deletedIds.has(item.id));
  return { items: visible, removedCount: items.length - visible.length };
}


export async function readStableSourceSnapshot<T>(
  currentGeneration: () => number,
  readSnapshot: () => Promise<T>,
): Promise<T> {
  while (true) {
    const generationBeforeRead = currentGeneration();
    const snapshot = await readSnapshot();
    if (currentGeneration() === generationBeforeRead) return snapshot;
  }
}
