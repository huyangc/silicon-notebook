"use client";

import { useMemo, useState } from "react";

import { slicePage } from "./pagination-logic.mjs";

/**
 * 前端分页:接口一次返回整份清单(契约上刻意不分页的那几类),界面只渲染当前一页,
 * 配合 `<Pagination>` 使用。
 *
 * - 页码按清单当前长度夹紧(`slicePage`):翻到最后一页再删掉那一行,视图自动退回上一页。
 *   夹紧后的页码同时写回状态——否则清单再变长时,视图会跳回那个早已离开的旧页码。
 * - `resetKey` 变化(换了一个群组、改了筛选词)时回到第一页——同一个组件实例被复用给
 *   另一份清单时,停在上一份清单的第 N 页没有意义。重置在渲染期完成,不会先闪一帧旧页。
 */
export function useClientPagination<T>(
  items: readonly T[],
  pageSize: number,
  resetKey?: unknown,
): {
  page: number;
  pageSize: number;
  total: number;
  pageItems: T[];
  setPage: (page: number) => void;
} {
  const [requested, setRequested] = useState(0);
  const [seenKey, setSeenKey] = useState(resetKey);
  let page = requested;
  if (!Object.is(seenKey, resetKey)) {
    setSeenKey(resetKey);
    setRequested(0);
    page = 0;
  }
  const view = useMemo(() => slicePage(items, page, pageSize), [items, page, pageSize]);
  if (view.page !== page) setRequested(view.page);
  return {
    page: view.page,
    pageSize,
    total: items.length,
    pageItems: view.items,
    setPage: setRequested,
  };
}
