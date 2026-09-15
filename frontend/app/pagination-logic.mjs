export function pageMeta({ page, pageSize, total }) {
  const lastPage = Math.max(0, Math.ceil(total / pageSize) - 1);
  const from = total === 0 ? 0 : page * pageSize + 1;
  const to = Math.min(total, (page + 1) * pageSize);
  return { lastPage, canPrev: page > 0, canNext: page < lastPage, from, to };
}
export const clampPage = (p, lastPage) => Math.max(0, Math.min(lastPage, p));

// 前端分页:整份清单已经取回,只切出当前一页来渲染。请求的页码按当前长度夹紧——
// 最后一页上唯一一行被移除后,视图退回上一页,而不是停在一张空页上。
export function slicePage(items, page, pageSize) {
  const current = clampPage(page, pageMeta({ page, pageSize, total: items.length }).lastPage);
  return { page: current, items: items.slice(current * pageSize, (current + 1) * pageSize) };
}
