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


export function notebookIsActive(
  expectedNotebook: string,
  currentNotebook: string | null,
): boolean {
  return expectedNotebook === currentNotebook;
}


export function sessionListRequestIsCurrent(
  expectedRequest: number,
  currentRequest: number,
  expectedNotebook: string,
  currentNotebook: string | null,
): boolean {
  return (
    expectedRequest === currentRequest
    && notebookIsActive(expectedNotebook, currentNotebook)
  );
}


export type NotebookRequest<T> = {
  notebookId: string;
  requestId: number;
  promise: Promise<T>;
};


export async function followLatestNotebookRequest<T>(
  initial: NotebookRequest<T>,
  latest: () => NotebookRequest<T> | null,
  isNotebookActive: () => boolean,
): Promise<{ requestId: number; generationId: number; value: T } | null> {
  let current = initial;
  let fallback: { requestId: number; value: T } | null = null;
  while (true) {
    let value: T;
    try {
      value = await current.promise;
    } catch (error) {
      const candidate = latest();
      if (
        candidate
        && candidate.notebookId === current.notebookId
        && candidate.requestId > current.requestId
      ) {
        current = candidate;
        continue;
      }
      if (fallback && isNotebookActive()) {
        return {
          ...fallback,
          // The fallback resolved the generation whose request failed. It is
          // safe to publish only while that generation is still current.
          generationId: current.requestId,
        };
      }
      throw error;
    }
    if (!isNotebookActive()) return null;

    const candidate = latest();
    if (
      candidate
      && candidate.notebookId === current.notebookId
      && candidate.requestId > current.requestId
    ) {
      fallback = { requestId: current.requestId, value };
      current = candidate;
      continue;
    }
    return {
      requestId: current.requestId,
      generationId: current.requestId,
      value,
    };
  }
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


/**
 * 工作区的能力位 —— 「哪些入口画出来」的唯一判据。
 *
 * `canManageContent` 是 `NotebookSummary.can_manage_content`（群组知识共享 P2）:
 * 组管理员打开被共享进本组的库时 `access` 仍是 `"reader"`（权限档没有新增枚举值，
 * 裁决 P2-3），但他确实有内容管理权。所以内容管理那几位不能再只看 `access`。
 *
 * ⚠ 后端才是权威（`require_notebook_capability` / `notebook_capability_allowed`）。
 * 这里画多了按钮只会让用户点进一个必然 404 的动作，所以缺省一律取**收**的那一侧:
 * 参数省略 = false = 逐字复现本参数出现之前的行为（旧后端不发这个字段）。
 */
export function workspaceCapabilities(
  access: string | undefined,
  role: string,
  canManageContent: boolean = false,
) {
  const canWrite = access !== "reader" || canManageContent;
  return {
    canWriteNotebook: canWrite,
    canGovernKnowledge: canWrite,
    // 深度报告对**只读成员也开放**（群组知识共享 P1）。它不是 `canWrite` 的一部分：
    // 报告按创建者行级隔离——成员只看得见、也只改得动**自己建的**那些，owner 也
    // 一样（不引入「owner 看全部」这条新披露）。所以列表里出现的每一份报告都是
    // 当前用户自己的，可操作性恒成立，没有需要按 access 收起来的动作。后端 9 个
    // 写端点各自挂 `require_notebook_read` + 体内 `reports.created_by == 当前用户`，
    // 这里放开的是**入口**，不是判定。
    //
    // 只放开报告面：来源写、图谱构建、知识治理仍跟着 `canWrite` 走。
    // P2 之后 `canWrite` 本身放宽了（组管理员为真），但这一位**恒 true 不变**——
    // 它本来就已经对每一位只读成员开着，没有可放宽的余地。
    canManageReports: true,
    // 图谱类型的有效配置属于当前笔记本：owner 与组管理员可以维护本库的覆盖和自建
    // 类型；纯只读成员不行。全局基线仍只允许系统管理员变更。
    canManageNotebookSchemas: canWrite,
    canManageGlobalSchemas: role === "admin",
  };
}


/**
 * 笔记本列表「角色」列的文案。
 *
 * 此前那一列**整列写死 "Owner"**,于是只读共享进来的库也被标成所有者——与挂载选择器
 * 把别人的库标成「我的笔记本」是同一类事实错误的标签,同批修掉。判据按行取;
 * `override` 只给「群组」分区用(那一批的 `access` 同样是 reader,但「群组成员」比
 * 「只读成员」更准)。
 */
export function notebookRoleText(
  notebook: { access?: string },
  override?: string,
): string {
  if (override) return override;
  return (notebook.access ?? "owner") === "reader" ? "只读成员" : "Owner";
}


export function doneItemDestination(kind: string | undefined): "sources" | "kg" {
  return kind === "paper_meta_done" ? "sources" : "kg";
}
