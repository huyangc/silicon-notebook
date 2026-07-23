// 每笔记本「用户可见文档」数量上限的前端判定。
//
// 上限的真源是后端的 effective document_limit(owner 的有效配额),经
// GET /notebooks/{id} 详情下发到 NotebookSummary.document_limit。列表投影
// (listNotebooks)里这个字段是 0 哨兵,所以这里把 0 / 缺失一律当「未知」——
// 不显示指示、也不门控,交给后端 409 兜底。
//
// 管理员豁免:加源写路径是 owner-only(成员表只有只读角色),当前用户恒等于
// owner,故按当前用户角色判定 admin 即等价于「owner 是 admin」,正确。

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
