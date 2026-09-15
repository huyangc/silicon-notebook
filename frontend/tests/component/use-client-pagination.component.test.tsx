import { act, renderHook } from "@testing-library/react";
import { expect, test } from "vitest";

import { useClientPagination, type PaginationResetKey } from "../../app/use-client-pagination.ts";

const rows = (count: number) => Array.from({ length: count }, (_, index) => index);

type Props = { list: number[]; key?: PaginationResetKey };

function renderPagination(items: number[], resetKey?: PaginationResetKey) {
  return renderHook(
    ({ list, key }: Props) => useClientPagination(list, 20, key),
    { initialProps: { list: items, key: resetKey } as Props },
  );
}

test("清单缩短时页码夹紧并写回：再变长也不会跳回早已离开的页", () => {
  const { result, rerender } = renderPagination(rows(45));
  act(() => result.current.setPage(2));
  expect(result.current.pageItems[0]).toBe(40);

  // 第 3 页的行都没了:视图退回第 2 页。
  rerender({ list: rows(40) });
  expect(result.current.page).toBe(1);
  expect(result.current.pageItems[0]).toBe(20);

  // 清单又长回 45 行:仍停在用户实际看到的第 2 页,而不是被夹紧前请求过的第 3 页。
  rerender({ list: rows(45) });
  expect(result.current.page).toBe(1);
  expect(result.current.pageItems[0]).toBe(20);
});

test("resetKey 变化回到第一页，同一个 key 重渲染保持当前页", () => {
  const { result, rerender } = renderPagination(rows(45), "group-a");
  act(() => result.current.setPage(1));

  rerender({ list: rows(45), key: "group-a" });
  expect(result.current.page).toBe(1);

  rerender({ list: rows(45), key: "group-b" });
  expect(result.current.page).toBe(0);
  expect(result.current.pageItems).toEqual(rows(20));
  expect(result.current.total).toBe(45);
});
