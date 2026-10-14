"use client";

/**
 * use-knowhow-row-code.ts
 *
 * Knowhow 行详情抽屉里「本行各列代码状态」的领域 owner：按列索引的代码状态
 * map、是否已成功加载、加载错误，以及 loadRowCode 与代码浮层保存/删除后的两个
 * 本地合并命令。从 knowhow-panel.tsx 原样搬出（S4-d）：state、请求号守卫、
 * loadRowCode、按 openRowId 拉取的 effect 与两个 handler 的代码一字未改。
 *
 * 面板在原 effect 的位置（拉表详情的 effect 之后、notebook 复位 effect 之前）调用
 * 本 hook，并用单一标识符 `knowhowRowCode` 持有返回值，在使用点直接读
 * `knowhowRowCode.view.*`；不解构、不逐字段摊平，也不把返回对象放进依赖数组或整体
 * 下传（owner-view-no-reflatten 守卫钉住）。
 *
 * 回调身份与搬家前相同：`loadRowCode` 仍是 useCallback([])；`handleCodeSaved` /
 * `handleCodeDeleted` 仍是每次渲染新建的普通函数，**不要**给它们加 useCallback
 * （计划第 184 行，knowhow-panel-boundary 守卫钉住）。
 *
 * 展开行一变（含关抽屉变成 null、直接换到另一行），渲染期就把 map 复位成新的 `{}`、
 * loaded/error 复位：这一步负责清掉上一行的代码状态。新行的拉取要到 effect 里才发起，
 * 抽屉第一帧拿到的是本 hook 当时持有的状态；不复位的话，第一帧会带着上一行的
 * `rowCodeLoaded=true` 与代码 map，按上一行的代码状态画出 chip
 * （knowhow-panel-row-code-reset 组件用例钉住）。复位放在渲染期（React 文档「prop 变化
 * 时调整 state」的写法：比较上一次的 openRowId，不同就同步 setState，React 丢弃这次
 * 渲染结果、带着新 state 立刻重渲染，不会把中间态提交出去），而不是 effect 里：S6 之前
 * 在 effect 里复位，关抽屉要多一次提交；现在复位与关抽屉/换行是同一次提交。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { fetchKnowhowRowCodeByColumn, type KnowhowCellCode } from "./knowhow-model.ts";
import { CODE_STATUS_LOAD_ERROR } from "./knowhow-code-logic.ts";

export function useKnowhowRowCode(openRowId: string | null) {
  // 当前展开行的「按列索引代码状态」map——抽屉打开时一次性拉取（见
  // knowhow-model.ts 的 fetchKnowhowRowCodeByColumn 头注释：GET 一次行详情
  // 换全行代码状态，不随网格/抽屉渲染逐格 GET）。rowCodeLoaded 只在成功加载
  // 后才置 true；加载失败时保持 false 且不合成任何 map——宁可"暂时不显示
  // chip"也不能在数据不确定时显示可能误导的"添加代码"（明明有代码却因为这
  // 次请求失败而提示"添加"）。
  const [rowCodeByColumn, setRowCodeByColumn] = useState<Record<string, KnowhowCellCode>>({});
  const [rowCodeLoaded, setRowCodeLoaded] = useState(false);
  const [rowCodeError, setRowCodeError] = useState<string | null>(null);
  // 上一次渲染时的展开行：与本次 openRowId 不同即在渲染期复位上面三项（见文件头注释）。
  const [rowCodeOwnerId, setRowCodeOwnerId] = useState<string | null>(openRowId);
  if (rowCodeOwnerId !== openRowId) {
    setRowCodeOwnerId(openRowId);
    setRowCodeByColumn({});
    setRowCodeLoaded(false);
    setRowCodeError(null);
  }

  // Task 11：行详情抽屉展开的这一行的代码状态——独立于 loadDetail（整表结构/
  // 内容），只在抽屉真正打开时才发起，避免网格视图本身多背一次网络请求。
  const rowCodeRequestRef = useRef(0);
  const loadRowCode = useCallback((rowId: string) => {
    const requestId = ++rowCodeRequestRef.current;
    setRowCodeError(null);
    fetchKnowhowRowCodeByColumn(rowId)
      .then((map) => {
        if (rowCodeRequestRef.current !== requestId) return; // 已切行/关抽屉：丢弃旧行响应
        setRowCodeByColumn(map);
        setRowCodeLoaded(true);
      })
      .catch(() => {
        if (rowCodeRequestRef.current === requestId) setRowCodeError(CODE_STATUS_LOAD_ERROR);
      });
  }, []);

  useEffect(() => {
    if (!openRowId) return; // 关抽屉：状态已在渲染期复位，这里只剩 cleanup 作废旧请求
    loadRowCode(openRowId);
    return () => {
      rowCodeRequestRef.current += 1; // 切行/关抽屉：作废 in-flight 的旧行代码状态
    };
  }, [openRowId, loadRowCode]);

  // 代码保存成功：把最新代码视图合并进行级 map——不必整行重拉（putCellCode
  // 本身已返回更新后的值，见 knowhow-model.ts）。
  function handleCodeSaved(columnId: string, entry: KnowhowCellCode) {
    setRowCodeByColumn((prev) => ({ ...prev, [columnId]: entry }));
  }

  // 代码删除成功：从行级 map 里摘除这一列——摘除后 resolveCellCodeView 会
  // 合成 none 占位，chip 自然回落到「添加代码」（或 canEdit=false 时不显示）。
  function handleCodeDeleted(columnId: string) {
    setRowCodeByColumn((prev) => {
      const next = { ...prev };
      delete next[columnId];
      return next;
    });
  }

  return {
    view: {
      rowCodeByColumn,
      rowCodeLoaded,
      rowCodeError,
    },
    loadRowCode,
    handleCodeSaved,
    handleCodeDeleted,
  };
}
