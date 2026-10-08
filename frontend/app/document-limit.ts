// 每笔记本「用户可见文档」数量上限的前端判定。
//
// 上限的真源是后端的 effective document_limit(owner 的有效配额),经
// GET /notebooks/{id} 详情下发到 NotebookSummary.document_limit。列表投影
// (listNotebooks)里这个字段是 0 哨兵,所以这里把 0 / 缺失一律当「未知」——
// 不显示指示、也不门控,交给后端 409 兜底。
//
// 管理员豁免:加源写路径是 owner-only(成员表只有只读角色),当前用户恒等于
// owner,故按当前用户角色判定 admin 即等价于「owner 是 admin」,正确。

import { PROMOTION_SOURCE_TYPE } from "./source-management.ts";

export type DocumentCapacity = {
  /** 是否展示「文档 X / 上限」指示并参与门控。管理员或上限未知时为 false。 */
  show: boolean;
  /** 是否已达上限(可见文档数 ≥ 上限)。show 为 false 时恒为 false。 */
  atCapacity: boolean;
  /** 有效上限(仅 show 为 true 时有意义)。 */
  limit: number;
  /** 当前可见文档数。 */
  count: number;
};

export function resolveDocumentCapacity(input: {
  isAdmin: boolean;
  documentLimit: number | null | undefined;
  documentCount: number;
}): DocumentCapacity {
  const limit = input.documentLimit ?? 0;
  const count = Math.max(0, input.documentCount);
  const show = !input.isAdmin && Number.isFinite(limit) && limit > 0;
  return {
    show,
    atCapacity: show && count >= limit,
    limit,
    count,
  };
}

/** Return the user-facing reason a staged file upload cannot start.
 *
 * The backend owns the final quota decision, but the upload dialog already has
 * the same effective limit and the notebook's unfiltered visible-document
 * count.  Include the staged batch in that decision so a 19/20 notebook cannot
 * present an enabled "upload 2 files" button that is guaranteed to be rejected.
 */
export function documentUploadBlockReason(
  capacity: DocumentCapacity,
  stagedCount: number,
): string | null {
  if (!capacity.show || stagedCount <= 0) return null;
  const remaining = Math.max(0, capacity.limit - capacity.count);
  if (remaining === 0) {
    return "已达该笔记本的文档数量上限，无法继续上传。请先删除部分文档，或联系管理员调整上限。";
  }
  if (stagedCount > remaining) {
    const excess = stagedCount - remaining;
    return `当前仅可再上传 ${remaining} 个文档，已选择 ${stagedCount} 个。请移除 ${excess} 个文件后再上传。`;
  }
  return null;
}

/** 一页来源响应里的「上传文档数」（文档数量上限的计数口径）：后端在未带搜索词的
 *  页上下发 `visible_document_count`（不含收录来源，它们列出、计入总数，但不占名额）；
 *  旧后端不下发时退回总数。本地仍在删除中、被从这一页滤掉的来源与总数同样扣除——
 *  但滤掉的收录来源本就不在这个计数里，不再扣。 */
export function pageDocumentCount(
  page: {
    total_count: number;
    visible_document_count?: number | null;
    items: readonly { id: string; type?: string }[];
  },
  keptIds: ReadonlySet<string>,
): number {
  const counted = typeof page.visible_document_count === "number";
  const base = counted ? page.visible_document_count as number : page.total_count;
  const removed = page.items.filter(
    (item) => !keptIds.has(item.id) && (!counted || item.type !== PROMOTION_SOURCE_TYPE),
  ).length;
  return Math.max(0, base - removed);
}
