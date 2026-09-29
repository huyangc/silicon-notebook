// 退出共享笔记本 —— 网络客户端与纯 helper。
//
// 退出共享会**永久删除**成员自己在这本笔记本里的记忆,所以「退出」不再是一次无条件的
// DELETE:
//
//   1. GET  .../membership/exit-disclosure → { memory_count: N }  先问会删多少条;
//   2. N = 0 → DELETE .../membership,不带任何查询参数(与改动前逐字节相同);
//      N > 0 → 用户看过并确认之后 DELETE .../membership?acknowledged_memory_count=N。
//   3. 服务端在「N > 0 而参数缺失或不等」时回 409 `exit_disclosure_required`(附它自己数
//      到的最新条数),什么都不删;调用方据此就地更新确认框。
//
// 请求全部经共享 transport(api-client)。DELETE 用 `performApiRequest` 而不是
// `requestVoid`:409 的正文要读出最新条数,而 `throwHumanizedHttpError` 会把它压平成一句
// 泛化文案。其余失败仍走 `throwHumanizedHttpError`。

import { performApiRequest, requestJson } from "./api-client.ts";
import { throwHumanizedHttpError } from "./errors.ts";
import { memoryListPath } from "./memory-model.ts";
import type { PaginatedMemories } from "./workspace-model.ts";

const TAG = "notebook-exit";
const EXIT_DISCLOSURE_REQUIRED = "exit_disclosure_required";
const MEMORY_LIST_PAGE = 100;

export type ExitDisclosure = { memory_count: number };

const membershipPath = (notebookId: string) =>
  `/notebooks/${encodeURIComponent(notebookId)}/membership`;

const isCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isInteger(value) && value >= 0;

/** 离开这本笔记本会删掉「我自己」多少条记忆。形状不对一律当失败——不能凭一个读不懂的
 *  回答就放人退出。 */
export async function getExitDisclosure(
  notebookId: string,
  signal?: AbortSignal,
): Promise<ExitDisclosure> {
  const disclosure = await requestJson<ExitDisclosure>(
    `${membershipPath(notebookId)}/exit-disclosure`,
    { tag: TAG, signal },
  );
  if (!isCount(disclosure?.memory_count)) throw new Error("malformed exit disclosure");
  return { memory_count: disclosure.memory_count };
}

export type LeaveResult =
  | { left: true }
  /** 服务端拒绝了:确认过的条数与它此刻数到的不一致(或还没确认)。`memoryCount` 是最新值。 */
  | { left: false; memoryCount: number };

/** 409 正文里的最新条数;不是这个形状返回 null(那就是一个普通的 409)。 */
async function disclosureRequiredCount(response: Response): Promise<number | null> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return null;
  }
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail !== "object" || detail === null) return null;
  const { code, memory_count: memoryCount } = detail as Record<string, unknown>;
  return code === EXIT_DISCLOSURE_REQUIRED && isCount(memoryCount) ? memoryCount : null;
}

/**
 * 退出共享。`acknowledgedMemoryCount` 为 0(或不传)时**不带**查询参数——N = 0 的请求
 * 必须与「退出要删记忆」出现之前的那条 DELETE 完全一样。
 */
export async function leaveNotebook(
  notebookId: string,
  acknowledgedMemoryCount = 0,
): Promise<LeaveResult> {
  const query = acknowledgedMemoryCount > 0
    ? `?acknowledged_memory_count=${acknowledgedMemoryCount}`
    : "";
  const response = await performApiRequest(`${membershipPath(notebookId)}${query}`, {
    method: "DELETE",
    tag: TAG,
  });
  if (response.ok) return { left: true };
  if (response.status === 409) {
    const memoryCount = await disclosureRequiredCount(response.clone());
    if (memoryCount !== null) return { left: false, memoryCount };
  }
  return throwHumanizedHttpError(response, TAG);
}

/** `Content-Disposition` 里的文件名(`filename*=UTF-8''…` 优先);取不到返回 null。
 *  只留末段——服务端给的名字不许带路径。 */
export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const clean = (raw: string) => raw.split(/[\\/]/).pop()?.trim() || null;
  const extended = /filename\*\s*=\s*([^']*)'[^']*'([^;]+)/i.exec(header);
  if (extended) {
    try {
      const decoded = clean(decodeURIComponent(extended[2].trim()));
      if (decoded) return decoded;
    } catch {
      // 落到下面的普通 filename。
    }
  }
  const plain = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/i.exec(header);
  return plain ? clean(plain[1] ?? plain[2]) : null;
}

/** 把「我自己」在这本笔记本里的记忆导出成一个 Markdown 文件(下载由调用方触发)。 */
export async function exportOwnMemories(
  notebookId: string,
): Promise<{ blob: Blob; filename: string }> {
  const response = await performApiRequest(
    `/notebooks/${encodeURIComponent(notebookId)}/memories/export`,
    { tag: TAG },
  );
  if (!response.ok) await throwHumanizedHttpError(response, TAG);
  return {
    blob: await response.blob(),
    filename:
      filenameFromDisposition(response.headers.get("Content-Disposition"))
      ?? `memories-${notebookId}.md`,
  };
}

/** 「我自己」在这本笔记本里、可以转移的记忆 id(只有已确认的能转移,同记忆页的口径)。 */
export async function listOwnTransferableMemoryIds(
  notebookId: string,
  signal?: AbortSignal,
): Promise<string[]> {
  const ids: string[] = [];
  let offset = 0;
  for (;;) {
    const page = await requestJson<PaginatedMemories>(
      memoryListPath({
        scope: "notebook",
        notebookId,
        status: "confirmed",
        origin: "all",
        query: "",
        offset,
        limit: MEMORY_LIST_PAGE,
      }),
      { tag: TAG, signal },
    );
    for (const item of page.items) ids.push(item.id);
    offset += page.items.length;
    if (page.items.length === 0 || offset >= page.total_count) return ids;
  }
}
