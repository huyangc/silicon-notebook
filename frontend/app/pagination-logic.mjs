export function pageMeta({ page, pageSize, total }) {
  const lastPage = Math.max(0, Math.ceil(total / pageSize) - 1);
  const from = total === 0 ? 0 : page * pageSize + 1;
  const to = Math.min(total, (page + 1) * pageSize);
  return { lastPage, canPrev: page > 0, canNext: page < lastPage, from, to };
}
export const clampPage = (p, lastPage) => Math.max(0, Math.min(lastPage, p));
