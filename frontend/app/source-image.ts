/**
 * 一张附图的取图地址。每个消费方都把它交给带鉴权的 `fetchInternalAssetBlob`
 * (`AuthedImage`、放大预览),而那条路会把以 `/` 开头的入参当作**相对 API_BASE
 * 的路径**再解析一次(`api-client.resolveApiUrl`)。所以:
 *
 * · 绝对的 `apiBase`(`https://host/api`)→ 返回绝对地址,原样通过;
 * · 相对的 `apiBase`(`/api`,同源反代部署)→ 只返回 API_BASE 之内的那一段路径。
 *   把 `/api` 拼在前面会被再解析成 `/api/api/notebooks/…`,每一张图都 404。
 */
export function sourceImageAssetUrl(apiBase: string, notebookId: string, assetId: string): string {
  if (!assetId || !notebookId) return "";
  const path = `/notebooks/${notebookId}/assets/${assetId}`;
  return /^[a-z][a-z0-9+.-]*:\/\//i.test(apiBase) ? `${apiBase}${path}` : path;
}

/**
 * 一张附图该经**哪个** notebook 的资产端点去读。取图归属只有这一条规则、只实现
 * 这一次；所有取图点(引用卡「本段附图」、正文内联附图、清单条目图片、放大预览)
 * 都调它，不各写一遍三元式——两份规则必然漂移，而漂移的那一半就是权限面。
 *
 * ① 有 active notebook 就**恒用 active**，逐字保持既有口径：后端按资产自己声明的
 *    所属库在 active 的有效参与集内解析，跨库图片因此也能取到。绝不拿条目自己的
 *    `notebook_id` 去直连另一个库——挂载进来的参考库用户未必是成员，那是前端替
 *    用户猜权限，只能经 active 代理。
 * ② 没有 active(全局问答没有「当前笔记本」)才退到条目自己的 `notebook_id`。这不是
 *    ①的例外而是另一种情形：全局模式下每条引用/锚点/清单条目的所属库，都是本轮
 *    范围里**用户自己有读权**、由 `can_read_many` 准入过的库。拿它自己的 id 去取
 *    它自己的图不新增任何权限面——`GET /notebooks/{id}/assets/{asset_id}` 每次请求
 *    仍会跑 `require_notebook_read` 并复核资产所属库在该 notebook 的有效参与集内
 *    (参与集首项恒为自身)，服务端才是判权的那一方。
 * ③ 两者皆空 → 空串。调用方据此整块不渲染，与「无附图」等价，绝不拼一个取不到的
 *    URL 去换一张「图片加载失败」。
 */
export function assetNotebookId(
  activeNotebookId: string | null | undefined,
  itemNotebookId: string | null | undefined,
): string {
  return activeNotebookId || itemNotebookId || "";
}
