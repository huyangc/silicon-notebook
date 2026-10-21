// 删除整本笔记本的一天窗口 —— 网络客户端(纯逻辑,不依赖 React)。
//
// 主人确认删除后,服务端把笔记本放进一天的删除窗口(`pending_delete`):它立即对所有人
// 不可用,`delete_after` 起永久删除。窗口期内只开着几个入口,全在这里:
//
//   * GET  /me/notebook-deletions                 与我有关的、正在删除窗口里的笔记本;
//   * POST /notebooks/{id}/cancel-delete          主人撤销删除;
//   * GET  /notebooks/{id}/memories/export        导出我在这里的记忆(窗口期内也可以);
//   * POST /notebooks/{id}/memories/transfer      把我已确认的记忆转移到我自己的笔记本;
//   * GET  /notebooks/{id}                        读者打开它得到 404 + 说明码。
//
// 这些端点的失败各有含义(截止已过、库已不存在、超出导出上限……),所以一律用
// `performApiRequest` 读出 `detail.code` 再分流,不走会把正文压平成一句泛化文案的
// `requestJson`;其余失败照旧经 `throwHumanizedHttpError` 翻成人话抛出。所有时刻都取自
// 服务端的 `delete_after`,客户端不自己加一天。

import { performApiRequest, requestJson } from "./api-client.ts";
import { throwHumanizedHttpError } from "./errors.ts";
import { filenameFromDisposition } from "./notebook-exit-api.ts";
import type { TransferResult } from "./transfer-model.ts";
import type { NotebookSummary } from "./workspace-model.ts";

const TAG = "notebook-deletion";

/** 一本正在删除窗口里的笔记本(从调用者的角度看)。 */
export type NotebookDeletionNotice = {
  notebook_id: string;
  name: string;
  /** 永久删除的时刻(UTC ISO)。 */
  delete_after: string;
  is_owner: boolean;
  /** 我在这里的记忆条数(各种状态,即导出的内容)。 */
  memory_count: number;
  /** 其中「转移到我的笔记本」会移走的条数(已确认、不在晋升审批中)。 */
  transferable_count: number;
};

const isCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isInteger(value) && value >= 0;

/** 形状不对的条目一律丢掉——读不懂的数据不能变成一个能点的按钮。 */
export function parseDeletionNotice(value: unknown): NotebookDeletionNotice | null {
  if (typeof value !== "object" || value === null) return null;
  const v = value as Record<string, unknown>;
  if (typeof v.notebook_id !== "string" || !v.notebook_id) return null;
  if (typeof v.delete_after !== "string" || !Number.isFinite(Date.parse(v.delete_after))) return null;
  if (typeof v.is_owner !== "boolean") return null;
  if (!isCount(v.memory_count) || !isCount(v.transferable_count)) return null;
  return {
    notebook_id: v.notebook_id,
    name: typeof v.name === "string" ? v.name : "",
    delete_after: v.delete_after,
    is_owner: v.is_owner,
    memory_count: v.memory_count,
    transferable_count: v.transferable_count,
  };
}

export async function listNotebookDeletions(signal?: AbortSignal): Promise<NotebookDeletionNotice[]> {
  const body = await requestJson<{ items?: unknown }>("/me/notebook-deletions", { tag: TAG, signal });
  if (!Array.isArray(body?.items)) throw new Error("malformed notebook deletions");
  return body.items.map(parseDeletionNotice).filter((n): n is NotebookDeletionNotice => n !== null);
}

async function detailOf(response: Response): Promise<Record<string, unknown> | null> {
  try {
    const detail = ((await response.clone().json()) as { detail?: unknown } | null)?.detail;
    return typeof detail === "object" && detail !== null ? (detail as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** 主人确认删除:返回服务端定下的永久删除时刻。 */
export async function requestNotebookDeletion(notebookId: string): Promise<{ deleteAfter: string }> {
  const response = await performApiRequest(`/notebooks/${encodeURIComponent(notebookId)}`, {
    method: "DELETE",
    tag: TAG,
  });
  if (response.ok) {
    const body = (await response.json().catch(() => null)) as { delete_after?: unknown } | null;
    if (typeof body?.delete_after !== "string") throw new Error("malformed delete response");
    return { deleteAfter: body.delete_after };
  }
  return throwHumanizedHttpError(response, TAG);
}

export type CancelOutcome =
  | { kind: "cancelled"; notebook: NotebookSummary }
  /** 409 notebook_delete_due:截止已过,已无法撤销。 */
  | { kind: "due" }
  /** 404:这本笔记本已不存在(已永久删除)。 */
  | { kind: "gone" };

export async function cancelNotebookDeletion(notebookId: string): Promise<CancelOutcome> {
  const response = await performApiRequest(
    `/notebooks/${encodeURIComponent(notebookId)}/cancel-delete`,
    { method: "POST", tag: TAG },
  );
  if (response.ok) return { kind: "cancelled", notebook: (await response.json()) as NotebookSummary };
  if (response.status === 409 && (await detailOf(response))?.code === "notebook_delete_due") {
    return { kind: "due" };
  }
  if (response.status === 404) return { kind: "gone" };
  return throwHumanizedHttpError(response, TAG);
}

export type ExportOutcome =
  | { kind: "exported"; blob: Blob; filename: string }
  /** 404 delete_window_closed:删除时间已到,记忆已无法导出。 */
  | { kind: "closed" }
  /** 413 export_too_large。 */
  | { kind: "too_large" }
  /** 404 nothing_to_export:这里没有我的记忆了(比如刚转移走)。 */
  | { kind: "nothing" }
  | { kind: "failed" };

export async function exportDeletedNotebookMemories(notebookId: string): Promise<ExportOutcome> {
  let response: Response;
  try {
    response = await performApiRequest(
      `/notebooks/${encodeURIComponent(notebookId)}/memories/export`,
      { tag: TAG },
    );
  } catch {
    return { kind: "failed" };
  }
  if (response.ok) {
    return {
      kind: "exported",
      blob: await response.blob(),
      filename: filenameFromDisposition(response.headers.get("Content-Disposition"))
        ?? `memories-${notebookId}.md`,
    };
  }
  const code = (await detailOf(response))?.code;
  if (response.status === 404 && code === "delete_window_closed") return { kind: "closed" };
  if (response.status === 404 && code === "nothing_to_export") return { kind: "nothing" };
  if (response.status === 413) return { kind: "too_large" };
  return { kind: "failed" };
}

export type TransferOutcome =
  | { kind: "transferred"; results: TransferResult[] }
  /** 404 delete_window_closed(还没开始就已关闭)。 */
  | { kind: "closed" }
  /** 404:这本笔记本或目标笔记本已经读不到。 */
  | { kind: "gone" }
  | { kind: "failed" };

/** 把我在 `notebookId` 里已确认的记忆转移(移动)到 `targetNotebookId`。 */
export async function transferOutOfDeletedNotebook(
  notebookId: string,
  targetNotebookId: string,
): Promise<TransferOutcome> {
  let response: Response;
  try {
    response = await performApiRequest(
      `/notebooks/${encodeURIComponent(notebookId)}/memories/transfer`,
      {
        method: "POST",
        tag: TAG,
        body: JSON.stringify({ target_notebook_id: targetNotebookId }),
      },
    );
  } catch {
    return { kind: "failed" };
  }
  if (response.ok) {
    const body = (await response.json().catch(() => null)) as { results?: unknown } | null;
    if (!Array.isArray(body?.results)) return { kind: "failed" };
    return { kind: "transferred", results: body.results as TransferResult[] };
  }
  if (response.status === 404) {
    return (await detailOf(response))?.code === "delete_window_closed"
      ? { kind: "closed" }
      : { kind: "gone" };
  }
  return { kind: "failed" };
}

/** 打开一本正在删除窗口里的笔记本:服务端 404 带说明码。携带说明,由页面换成说明框。 */
export class NotebookDeletedError extends Error {
  readonly notice: NotebookDeletionNotice;

  constructor(notice: NotebookDeletionNotice) {
    super("这本笔记本已被删除");
    this.name = "NotebookDeletedError";
    this.notice = notice;
  }
}

/** `GET /notebooks/{id}`:正常返回摘要;窗口期内的 404 说明码抛 `NotebookDeletedError`;
 *  其余失败照旧翻成人话抛出。 */
export async function getNotebookOrDeletionNotice(notebookId: string): Promise<NotebookSummary> {
  const response = await performApiRequest(`/notebooks/${notebookId}`, {
    tag: "api",
    unauthorized: "clear-and-reload",
  });
  if (response.ok) return (await response.json()) as NotebookSummary;
  if (response.status === 404) {
    const detail = await detailOf(response);
    if (detail?.code === "notebook_pending_delete") {
      const notice = parseDeletionNotice(detail);
      if (notice) throw new NotebookDeletedError(notice);
    }
  }
  return throwHumanizedHttpError(response, "api");
}
