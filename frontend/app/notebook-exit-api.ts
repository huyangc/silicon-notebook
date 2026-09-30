// 退出共享笔记本 —— 网络客户端与纯 helper。
//
// 退出共享会**永久删除**成员自己在这本笔记本里的记忆,所以「退出」不再是一次无条件的
// DELETE:
//
//   1. GET  .../membership/exit-disclosure → { memory_count: N }  先问会删多少条;
//   2. N = 0 → DELETE .../membership,不带任何查询参数(与改动前逐字节相同);
//      N > 0 → 用户看过并确认之后 DELETE .../membership?acknowledged_memory_count=N。
//   3. 服务端在「确认数缺失或与它此刻数到的不等」时回 409 `exit_disclosure_required`(附它
//      自己数到的最新条数),什么都不删;调用方据此就地更新确认框;
//   4. 成功是 204(没有删)或 200 `{deleted_memory_count}`;删了一部分却没退成是 409 / 503
//      `exit_incomplete`(带 deleted_memory_count 与 memory_count)。客户端展示的每个数字
//      都取自这些响应,不自己算。
//
// 请求全部经共享 transport(api-client)。DELETE 用 `performApiRequest` 而不是
// `requestVoid`:409/503 的正文要读出数字,而 `throwHumanizedHttpError` 会把它压平成一句
// 泛化文案。其余失败仍走 `throwHumanizedHttpError`。

import { performApiRequest, requestJson } from "./api-client.ts";
import { throwHumanizedHttpError } from "./errors.ts";
import { isGroupGranted } from "./group-api.ts";
import { memoryListPath } from "./memory-model.ts";
import type { NotebookSummary, PaginatedMemories } from "./workspace-model.ts";

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

/** 一次 DELETE 的结果。每个数字都来自服务端的响应,客户端不自己算。 */
export type LeaveOutcome =
  /** 204 或 200:成员关系已结束。`deleted` 是服务端数到的、这次请求删掉的条数(204 没删,为 0)。 */
  | { kind: "left"; deleted: number }
  /** 409 exit_disclosure_required:确认过的条数与服务端此刻数到的不一致。什么都没删,仍是成员。 */
  | { kind: "disclosure"; memoryCount: number }
  /** 409 / 503 exit_incomplete:已删 `deleted` 条,没有退出成功(仍是成员),还剩 `remaining` 条。 */
  | { kind: "incomplete"; deleted: number; remaining: number; status: 409 | 503 }
  /** 网络失败、超时、读不懂的 5xx:不知道服务端做到了哪一步,不能说「失败」。 */
  | { kind: "unknown" };

const EXIT_INCOMPLETE = "exit_incomplete";

async function structuredDetail(response: Response): Promise<Record<string, unknown> | null> {
  try {
    const detail = ((await response.json()) as { detail?: unknown } | null)?.detail;
    return typeof detail === "object" && detail !== null ? (detail as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/**
 * 退出共享。`acknowledgedMemoryCount` 为 0(或不传)时**不带**查询参数——N = 0 的请求
 * 必须与「退出要删记忆」出现之前的那条 DELETE 完全一样。
 *
 * 契约里有明确含义的响应(204 / 200 / 409 / 503 exit_incomplete)返回 `LeaveOutcome`;
 * 传输失败和读不懂的 5xx 返回 `unknown`(调用方去核对实际状态);其余(401/403/404…)
 * 抛出已翻成人话的错误。
 */
export async function leaveNotebook(
  notebookId: string,
  acknowledgedMemoryCount = 0,
): Promise<LeaveOutcome> {
  const query = acknowledgedMemoryCount > 0
    ? `?acknowledged_memory_count=${acknowledgedMemoryCount}`
    : "";
  let response: Response;
  try {
    response = await performApiRequest(`${membershipPath(notebookId)}${query}`, {
      method: "DELETE",
      tag: TAG,
    });
  } catch {
    return { kind: "unknown" };
  }
  if (response.status === 204) return { kind: "left", deleted: 0 };
  if (response.ok) {
    const body = (await response.json().catch(() => null)) as { deleted_memory_count?: unknown } | null;
    return { kind: "left", deleted: isCount(body?.deleted_memory_count) ? body.deleted_memory_count : 0 };
  }
  if (response.status === 409 || response.status === 503) {
    const detail = await structuredDetail(response.clone());
    if (detail?.code === EXIT_DISCLOSURE_REQUIRED && response.status === 409 && isCount(detail.memory_count)) {
      return { kind: "disclosure", memoryCount: detail.memory_count };
    }
    if (detail?.code === EXIT_INCOMPLETE && isCount(detail.deleted_memory_count) && isCount(detail.memory_count)) {
      return {
        kind: "incomplete",
        deleted: detail.deleted_memory_count,
        remaining: detail.memory_count,
        status: response.status,
      };
    }
  }
  if (response.status >= 500) return { kind: "unknown" };
  return throwHumanizedHttpError(response, TAG);
}

/** 退出之后服务端实际处于哪种状态(用于结果不明时核对)。 */
export type ExitState =
  | { left: true }
  | { left: false; remaining: number };

/**
 * 重读笔记本列表与告知条数,判断此刻是「已退出」还是「仍是成员、还剩几条记忆」。
 * 列表里没有这本库,或者它只剩群组授权(退出之后靠授权继续读)都算已退出。
 * 任何一步读不到就抛出——调用方据此说「无法确认」,不猜。
 */
export async function readExitState(notebookId: string): Promise<ExitState> {
  const notebooks = await requestJson<NotebookSummary[]>("/notebooks", { tag: TAG });
  const entry = notebooks.find((notebook) => notebook.id === notebookId);
  if (!entry || isGroupGranted(entry)) return { left: true };
  return { left: false, remaining: (await getExitDisclosure(notebookId)).memory_count };
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
