export function pageMeta(a: { page: number; pageSize: number; total: number }):
  { lastPage: number; canPrev: boolean; canNext: boolean; from: number; to: number };
export function clampPage(p: number, lastPage: number): number;
