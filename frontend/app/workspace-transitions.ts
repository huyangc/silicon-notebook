export function ownsWorkspaceRun(
  expectedRun: number,
  currentRun: number,
  expectedWorkspace: number,
  currentWorkspace: number,
  expectedNotebook: string,
  currentNotebook: string | null,
): boolean {
  return (
    expectedRun === currentRun
    && expectedWorkspace === currentWorkspace
    && expectedNotebook === currentNotebook
  );
}


export function workspaceRequestIsCurrent(
  cancelled: boolean,
  expectedWorkspace: number,
  currentWorkspace: number,
  expectedNotebook: string,
  currentNotebook: string | null,
): boolean {
  return (
    !cancelled
    && expectedWorkspace === currentWorkspace
    && expectedNotebook === currentNotebook
  );
}


export function historyModeForTransition(
  currentNotebookId: string | null,
  nextNotebookId: string,
): "push" | "replace" {
  return currentNotebookId === nextNotebookId ? "replace" : "push";
}


export async function restoreLatestConversation<T>(
  sessions: readonly { id: string }[],
  apply: (id: string) => Promise<T>,
): Promise<T | null> {
  if (!sessions[0]) return null;
  try {
    return await apply(sessions[0].id);
  } catch {
    return null;
  }
}


export async function openMemoryDeepLink(
  notebookId: string,
  open: (notebookId: string) => Promise<void>,
  fallback: () => void,
): Promise<boolean> {
  try {
    await open(notebookId);
    return true;
  } catch {
    fallback();
    return false;
  }
}


export const NOTEBOOK_PRIVATE_MEMORY_DELETE_WARNING =
  "所有成员各自绑定到此笔记本的私有记忆也会按生命周期一并删除。";


export function workspaceCapabilities(access: string | undefined, role: string) {
  const canWrite = access !== "reader";
  return {
    canWriteNotebook: canWrite,
    canGovernKnowledge: canWrite,
    canManageReports: canWrite,
    canManageSchemas: role === "admin",
  };
}


export function doneItemDestination(kind: string | undefined): "sources" | "kg" {
  return kind === "paper_meta_done" ? "sources" : "kg";
}
