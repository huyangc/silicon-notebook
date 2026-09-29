"use client";

// 「退出共享」的唯一流程(顶栏按钮与笔记本卡片菜单共用)。
//
// 退出会永久删除成员自己在这本笔记本里的记忆,所以按下「退出共享」先问服务端会删多少条:
//   - 0 条:直接退出,请求与改动前完全一样;
//   - 多条:就地展开确认(渲染见 notebook-exit-panel.tsx),用户可以先导出、先转移,
//     最后带着自己看过的条数确认。
// 服务端在条数对不上时回 409 并附最新条数,确认框就地更新、要求重新确认。
//
// 一个 ref 当同步闸:`busyRef` 在按下的**同一个事件循环里**就置位(state 更新是异步的,
// 快速双击会在 state 生效前发出第二个请求)。闸只覆盖「读条数」与「DELETE」两类在途请求;
// 「取消」永远可用——取消不会撤销已发出的 DELETE(它落地后照样刷新列表并出提示),只是
// 让确认框收起。

import { useCallback, useEffect, useRef, useState } from "react";

import { useCopyResult, type CopyResult } from "./copy-result.ts";
import { saveBlobAsFile } from "./download-file.ts";
import { toUserMessage } from "./errors.ts";
import { transferMemories } from "./memory-transfer.ts";
import {
  exportOwnMemories,
  getExitDisclosure,
  leaveNotebook,
  listOwnTransferableMemoryIds,
} from "./notebook-exit-api.ts";
import { summarizeTransferResults, type TransferMode } from "./transfer-model.ts";

/** 确认框在视口里的落点(fixed 坐标):按钮正下方,或卡片菜单原来所在的位置。 */
export type ExitAnchor = { left: number; top: number };

/** 按钮正下方的落点。 */
export const anchorBelow = (element: HTMLElement): ExitAnchor => {
  const rect = element.getBoundingClientRect();
  return { left: rect.left, top: rect.bottom + 8 };
};

export type ExitPhase =
  | "checking" // 正在读会删多少条
  | "leaving" // 条数为 0,直接退出的 DELETE 在途
  | "confirm" // 有记忆要删:等用户确认
  | "unavailable"; // 读不到条数:不盲退

export type ExitFlow = {
  notebookId: string;
  anchor: ExitAnchor;
  phase: ExitPhase;
  /** 确认框上显示的、离开将删除的条数。 */
  count: number;
  /** 服务端数到的条数与用户确认过的不一致(409):提示重新确认。 */
  changed: boolean;
  /** DELETE 在途。 */
  leaving: boolean;
  /** 转移后重新读条数在途。 */
  recounting: boolean;
  /** 已经翻成人话的失败说明(不是后端原文)。 */
  failure: string;
  notice: string;
  transfer: "idle" | "loading" | "picking";
  transferIds: string[];
};

/** 慢请求才把确认框露出来:快路径(条数为 0)不该闪一下面板。 */
const REVEAL_AFTER_MS = 350;

export const EXIT_UNAVAILABLE_TEXT = "暂时无法确认将删除多少条记忆，请稍后重试。";

export function useNotebookExit(handlers: {
  onToast: (message: string) => void;
  onError: (error: unknown) => void;
}) {
  const [flow, setFlow] = useState<ExitFlow | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [slow, setSlow] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [transferExtractKg, setTransferExtractKg] = useState(true);
  const exportResult = useCopyResult();

  const flowRef = useRef<ExitFlow | null>(null);
  const busyRef = useRef<string | null>(null);
  const exportingRef = useRef(false);
  const epochRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  const afterLeaveRef = useRef<() => Promise<void>>(async () => {});
  const revealTimerRef = useRef<number | null>(null);
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  const publish = useCallback((next: ExitFlow | null) => {
    flowRef.current = next;
    setFlow(next);
  }, []);

  const update = useCallback((patch: Partial<ExitFlow>) => {
    if (flowRef.current) publish({ ...flowRef.current, ...patch });
  }, [publish]);

  const markBusy = useCallback((id: string | null) => {
    busyRef.current = id;
    setBusyId(id);
    if (revealTimerRef.current !== null) window.clearTimeout(revealTimerRef.current);
    revealTimerRef.current = null;
    setSlow(false);
    if (id) revealTimerRef.current = window.setTimeout(() => setSlow(true), REVEAL_AFTER_MS);
  }, []);

  useEffect(() => () => {
    if (revealTimerRef.current !== null) window.clearTimeout(revealTimerRef.current);
    controllerRef.current?.abort();
  }, []);

  const sendLeave = useCallback(async (id: string, epoch: number, acknowledged: number) => {
    markBusy(id);
    const current = flowRef.current;
    if (current && epochRef.current === epoch) {
      update({ leaving: true, failure: "", phase: current.phase === "checking" ? "leaving" : current.phase });
    }
    try {
      const result = await leaveNotebook(id, acknowledged);
      markBusy(null);
      const alive = epochRef.current === epoch;
      if (!result.left) {
        // 用户在等待期间点了取消:这次退出没有发生,也就没有什么要告诉他的。
        if (alive) update({ phase: "confirm", leaving: false, count: result.memoryCount, changed: true });
        return;
      }
      if (alive) publish(null);
      try {
        await afterLeaveRef.current();
      } catch (error) {
        handlersRef.current.onError(error);
        return;
      }
      handlersRef.current.onToast(
        acknowledged > 0 ? `已退出共享，已删除 ${acknowledged} 条记忆` : "已退出共享",
      );
    } catch (error) {
      markBusy(null);
      const inline = epochRef.current === epoch && flowRef.current?.phase === "confirm";
      if (inline) {
        update({ leaving: false, failure: toUserMessage(error, "退出没有成功，请重试") });
        return;
      }
      // 直接退出(没有确认框可落)或确认框已被取消:沿用一直以来的全局错误通道。
      if (epochRef.current === epoch) publish(null);
      handlersRef.current.onError(error);
    }
  }, [markBusy, publish, update]);

  const runCheck = useCallback(async (id: string, epoch: number, recount: boolean) => {
    markBusy(id);
    const controller = new AbortController();
    controllerRef.current = controller;
    let count: number | null = null;
    try {
      count = (await getExitDisclosure(id, controller.signal)).memory_count;
    } catch {
      count = null;
    }
    if (controllerRef.current === controller) controllerRef.current = null;
    if (epochRef.current !== epoch) return; // 已取消/被替换:取消那一刻已经放开了闸
    if (count === null) {
      markBusy(null);
      update({ phase: "unavailable", recounting: false });
      return;
    }
    if (count === 0 && !recount) {
      await sendLeave(id, epoch, 0);
      return;
    }
    markBusy(null);
    update({ phase: "confirm", count, recounting: false });
  }, [markBusy, sendLeave, update]);

  /** 按下「退出共享」。`afterLeave` 由入口给:各入口退出后对账的方式不同。 */
  const start = useCallback((notebookId: string, anchor: ExitAnchor, afterLeave: () => Promise<void>) => {
    if (busyRef.current) return;
    if (flowRef.current?.notebookId === notebookId) return;
    afterLeaveRef.current = afterLeave;
    const epoch = ++epochRef.current;
    publish({
      notebookId, anchor, phase: "checking", count: 0, changed: false, leaving: false,
      recounting: false, failure: "", notice: "", transfer: "idle", transferIds: [],
    });
    void runCheck(notebookId, epoch, false);
  }, [publish, runCheck]);

  /** 取消:收起确认框,什么都不发。永远可用;已在飞的 DELETE 不撤销。 */
  const cancel = useCallback(() => {
    epochRef.current += 1;
    if (controllerRef.current) {
      controllerRef.current.abort();
      controllerRef.current = null;
      markBusy(null);
    }
    if (flowRef.current) publish(null);
  }, [markBusy, publish]);

  const confirm = useCallback(() => {
    const current = flowRef.current;
    if (!current || current.phase !== "confirm" || busyRef.current) return;
    if (current.recounting || current.transfer !== "idle") return;
    void sendLeave(current.notebookId, epochRef.current, current.count);
  }, [sendLeave]);

  const retry = useCallback(() => {
    const current = flowRef.current;
    if (!current || current.phase !== "unavailable" || busyRef.current) return;
    update({ phase: "checking", failure: "" });
    void runCheck(current.notebookId, epochRef.current, false);
  }, [runCheck, update]);

  const exportMemories = useCallback(async () => {
    const current = flowRef.current;
    if (!current || exportingRef.current || current.leaving) return;
    const key = `export:${current.notebookId}`;
    exportingRef.current = true;
    setExporting(true);
    try {
      const { blob, filename } = await exportOwnMemories(current.notebookId);
      saveBlobAsFile(blob, filename);
      exportResult.report(key, true);
    } catch {
      exportResult.report(key, false);
    } finally {
      exportingRef.current = false;
      setExporting(false);
    }
  }, [exportResult]);

  const openTransfer = useCallback(async () => {
    const current = flowRef.current;
    if (!current || current.phase !== "confirm" || current.transfer !== "idle" || current.leaving) return;
    const epoch = epochRef.current;
    update({ transfer: "loading", failure: "", notice: "" });
    let ids: string[];
    try {
      ids = await listOwnTransferableMemoryIds(current.notebookId);
    } catch (error) {
      if (epochRef.current === epoch) {
        update({ transfer: "idle", failure: toUserMessage(error, "暂时无法读取你的记忆，请稍后重试。") });
      }
      return;
    }
    if (epochRef.current !== epoch) return;
    if (ids.length === 0) {
      update({ transfer: "idle", notice: "没有可以转移的记忆（只有「已确认」的记忆可以转移）。" });
      return;
    }
    setTransferExtractKg(true);
    update({ transfer: "picking", transferIds: ids });
  }, [update]);

  const closeTransfer = useCallback(() => update({ transfer: "idle", transferIds: [] }), [update]);

  /** 交给既有的目标笔记本选择器(DestinationPicker)。resolve = 已完成,选择器由此卸载。 */
  const submitTransfer = useCallback(async (
    targetNotebookId: string,
    mode: TransferMode,
    targetNotebookName: string,
  ) => {
    const current = flowRef.current;
    if (!current) return;
    const epoch = epochRef.current;
    const { results } = await transferMemories(
      current.transferIds, targetNotebookId, mode, transferExtractKg,
    );
    if (epochRef.current !== epoch) return;
    const summary = summarizeTransferResults(results);
    // 移动时「副本已建但源没清掉」不算失败:退出会删掉这条源,副本已经在目标里了。
    const done = summary.succeeded + summary.copiedSourceNotRemoved.length;
    const failed = summary.failed - summary.copiedSourceNotRemoved.length;
    const verb = mode === "move" ? "移动" : "复制";
    const notice = failed > 0
      ? (done > 0
        ? `已${verb} ${done} 条记忆到「${targetNotebookName}」，另有 ${failed} 条没有成功。`
        : `${failed} 条记忆没能${verb}，请稍后重试。`)
      : (done > 1
        ? `已${verb} ${done} 条记忆到「${targetNotebookName}」`
        : `已${verb}到「${targetNotebookName}」`);
    // 转移之后条数变了:就地重读,而不是让用户拿着旧数字去确认。
    update({ transfer: "idle", transferIds: [], notice, failure: "", recounting: true });
    void runCheck(current.notebookId, epoch, true);
  }, [runCheck, transferExtractKg, update]);

  return {
    flow,
    /** 有「读条数 / DELETE」在途的笔记本 id(顶栏按钮据此显示「退出中…」)。 */
    busyId,
    /** 在途请求已经拖过了 REVEAL_AFTER_MS:此时才把面板露出来。 */
    slow,
    exporting,
    exportResult: (flow ? exportResult.resultFor(`export:${flow.notebookId}`) : "idle") as CopyResult,
    transferExtractKg,
    setTransferExtractKg,
    start,
    cancel,
    confirm,
    retry,
    exportMemories,
    openTransfer,
    closeTransfer,
    submitTransfer,
  };
}

export type NotebookExit = ReturnType<typeof useNotebookExit>;
