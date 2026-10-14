"use client";

/**
 * use-knowhow-table-list.ts
 *
 * Knowhow 面板第一层「表列表」的领域 owner：表清单、加载态与错误、健康筛选
 * （跟随 `initialHealthFilter` prop）与筛选后的可见表，以及 `loadTables` 命令。
 * 从 knowhow-panel.tsx 原样搬出（S4-d）：state、useMemo、两个 effect 与
 * loadTables 的代码一字未改。呈现在 knowhow-table-list.tsx。
 *
 * 面板用单一标识符 `knowhowTableList` 持有返回值，在使用点直接读
 * `knowhowTableList.view.*`、调 `knowhowTableList.loadTables()`：不解构、不逐字段
 * 摊平，也不把整个返回对象放进依赖数组或整体作为 prop 下传（它每次渲染都是新
 * 对象）。owner-view-no-reflatten 守卫钉住这几条。
 *
 * 回调身份与搬家前相同：`setHealthFilter` 是 useState 原生 setter，面板原样传给
 * 列表的 onHealthFilterChange；`loadTables` 仍是 useCallback([notebookId])，列表
 * 的 onRetry 与面板各写命令完成后的刷新共用它。
 *
 * effect 顺序：面板在函数体最前面调用本 hook，健康筛选同步与拉清单这两个 effect
 * 因此排到面板 panelMountedRef 那个 effect 之前。后者只设 ref，不 setState、
 * 不发请求，这一换位不可观测。
 *
 * 旧响应守卫（S6）：loadTables 被面板各写命令在 await 之后调用，命令闭包里拿的可能是
 * 上一个笔记本的 loadTables；清单请求也可能连发、乱序落地。所以：调用时若闭包的笔记本
 * 已不是当前笔记本，直接不发请求；每次请求领单调递增的号，只有最新一次的结果（清单、
 * 错误、loading 收尾）能落进 state。当前笔记本由拉清单 effect 在发请求前登记。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  fetchKnowhowTables,
  filterKnowhowTables,
  type KnowhowHealthFilter,
  type KnowhowTableSummary,
} from "./knowhow-model.ts";

export function useKnowhowTableList({
  notebookId,
  initialHealthFilter,
}: {
  notebookId: string;
  initialHealthFilter?: KnowhowHealthFilter;
}) {
  const [tables, setTables] = useState<KnowhowTableSummary[] | null>(null);
  const [tablesLoading, setTablesLoading] = useState(false);
  const [tablesError, setTablesError] = useState<string | null>(null);
  const [healthFilter, setHealthFilter] = useState<KnowhowHealthFilter>(initialHealthFilter ?? "all");
  const visibleTables = useMemo(
    () => filterKnowhowTables(tables ?? [], healthFilter),
    [tables, healthFilter],
  );

  useEffect(() => {
    setHealthFilter(initialHealthFilter ?? "all");
  }, [initialHealthFilter]);

  const currentNotebookRef = useRef(notebookId);
  const tablesRequestRef = useRef(0);
  const loadTables = useCallback(() => {
    if (currentNotebookRef.current !== notebookId) return; // 上一个笔记本的命令落地：不再刷它的清单
    const requestId = ++tablesRequestRef.current;
    setTablesLoading(true);
    setTablesError(null);
    fetchKnowhowTables(notebookId)
      .then((list) => {
        if (tablesRequestRef.current === requestId) setTables(list);
      })
      .catch(() => {
        if (tablesRequestRef.current === requestId) setTablesError("加载 knowhow 表失败，请重试");
      })
      .finally(() => {
        if (tablesRequestRef.current === requestId) setTablesLoading(false);
      });
  }, [notebookId]);

  useEffect(() => {
    currentNotebookRef.current = notebookId;
    loadTables();
    // notebookId 只用来登记当前笔记本，它一变 loadTables 必然跟着变，依赖不必另列
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadTables]);

  return {
    view: {
      tables,
      tablesLoading,
      tablesError,
      healthFilter,
      visibleTables,
    },
    setHealthFilter,
    loadTables,
  };
}
