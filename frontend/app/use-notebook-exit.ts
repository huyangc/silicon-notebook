"use client";

// 「退出共享」的唯一流程(顶栏按钮与笔记本卡片菜单共用)。
//
// 退出会永久删除成员自己在这本笔记本里的记忆,所以按下「退出共享」先问服务端会删多少条:
//   - 0 条:直接退出,请求与改动前完全一样;
//   - 多条:就地展开确认(渲染见 notebook-exit-panel.tsx),用户可以先导出、先转移,
//     最后带着自己看过的条数确认。
// 服务端在条数对不上时回 409 并附最新条数,确认框就地更新、要求重新确认。
//
// 结果如实:每个数字取自服务端响应(见 notebook-exit-copy.ts)。网络中断/超时是「不知道」而
// 不是「失败」——重读笔记本列表与告知条数,再说哪种状态成立。面板已被取消或离开页面之后
// 才到的结果,同样用提示告诉用户,绝不静默丢掉。
//
// 同步闸:`leavingRef` 是「这本笔记本有一次 DELETE(含事后核对)在途」的集合,在按下的
// **同一个事件循环里**就写入(state 更新是异步的,快速双击会在 state 生效前发出第二个请求)。
// 它按笔记本 id 记,不是一把全局锁:取消不会撤销已发出的 DELETE,但也不该让用户在**另一
// 本**笔记本上的「退出共享」被悄悄吞掉。同一本笔记本在 DELETE 还没落地时再按,不会发第二
// 个请求,而是接回那次在途请求(面板显示「正在退出…」,结果落在面板上)。

import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { useCopyResult, type CopyResult } from "./copy-result.ts";
import { saveBlobAsFile } from "./download-file.ts";
import { toUserMessage } from "./errors.ts";
import { transferMemoriesInBatches } from "./memory-transfer.ts";
import {
  EXIT_FAILED_TEXT,
  EXIT_UNVERIFIED_TEXT,
  incompleteConfirmText,
  incompleteFailedText,
  incompleteLateText,
  leftText,
  stillMemberText,
} from "./notebook-exit-copy.ts";
import {
  exportOwnMemories,
  getExitDisclosure,
  leaveNotebook,
  listOwnTransferableMemoryIds,
  readExitState,
  type ExitState,
  type LeaveOutcome,
} from "./notebook-exit-api.ts";
import type { Box } from "./notebook-exit-placement.ts";
import { summarizeTransferResults, type TransferMode } from "./transfer-model.ts";

export { EXIT_UNAVAILABLE_TEXT } from "./notebook-exit-copy.ts";

/** 面板的落点依据:打开它的控件(优先,量它的实时位置)与按下那一刻的位置(控件没了时兜底)。 */
export type ExitAnchor = { element: HTMLElement | null; box: Box };

const boxOf = (rect: DOMRect | Box): Box => ({
  left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
});

/** 以一个控件为落点:面板出现在它的正下方,并且跟着它走。 */
export const anchorOf = (element: HTMLElement): ExitAnchor => ({
  element,
  box: boxOf(element.getBoundingClientRect()),
});

/** 没有控件可跟(控件已不在)时,以一个点为落点。 */
export const anchorAt = (left: number, top: number): ExitAnchor => ({
  element: null,
  box: { left, top, right: left, bottom: top },
});

export type ExitPhase =
  | "checking" // 正在读会删多少条
  | "leaving" // DELETE 在途(含事后核对)
  | "confirm" // 等用户确认(或直接退出失败后的结果)
  | "unavailable"; // 读不到条数/核对不出结果:不盲退

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
  /** DELETE 没有拿到明确结果,正在重读实际状态。 */
  verifying: boolean;
  /** 转移之后/退出未完成之后重新读条数在途。 */
  recounting: boolean;
  /** 已经翻成人话的失败说明(不是后端原文)。 */
  failure: string;
  notice: string;
  /** working = 选择器已被关掉、但一次转移请求还在途。 */
  transfer: "idle" | "loading" | "picking" | "working";
  transferIds: string[];
};

/** 慢请求才把确认框露出来:快路径(条数为 0)不该闪一下面板。 */
const REVEAL_AFTER_MS = 350;

type CheckMode = "first" | "recount" | "quiet";

export function useNotebookExit(handlers: {
  onToast: (message: string) => void;
  onError: (error: unknown) => void;
}) {
  const [flow, setFlow] = useState<ExitFlow | null>(null);
  const [slow, setSlow] = useState(false);
  const [transferExtractKg, setTransferExtractKg] = useState(true);
  const [, bump] = useReducer((n: number) => n + 1, 0);
  const exportResult = useCopyResult();

  const flowRef = useRef<ExitFlow | null>(null);
  const leavingRef = useRef<Set<string>>(new Set());
  // 正在导出的笔记本(按笔记本记,和导出结果 `export:<id>` 同一口径):另一本笔记本的退出
  // 面板不会因为这本的下载还没完而显示「正在导出…」、按钮被禁用。
  const exportingRef = useRef<Set<string>>(new Set());
  // 在途转移所属流程的 epoch(没有则为 null)。按流程认领:取消后重开的新流程(epoch 已变)
  // 不会被旧流程的转移置成「正在转移…」,旧转移落定也不会清掉新流程自己的在途标记。
  const transferEpochRef = useRef<number | null>(null);
  const epochRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  const afterLeaveRef = useRef<Map<string, () => Promise<void>>>(new Map());
  // runCheck 在条数为 0 时直接接着发 DELETE;sendLeave 又要在结果里回头调 runCheck,用 ref 断开循环。
  const sendLeaveRef = useRef<(id: string, acknowledged: number) => Promise<void>>(async () => {});
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  const publish = useCallback((next: ExitFlow | null) => {
    flowRef.current = next;
    setFlow(next);
  }, []);

  const update = useCallback((patch: Partial<ExitFlow>) => {
    if (flowRef.current) publish({ ...flowRef.current, ...patch });
  }, [publish]);

  /** 只在面板此刻还属于这本笔记本时才改它(结果晚到时面板可能已换成别的笔记本)。 */
  const updateFor = useCallback((notebookId: string, patch: Partial<ExitFlow>) => {
    if (flowRef.current?.notebookId === notebookId) update(patch);
  }, [update]);

  const busyPhase = flow !== null && (flow.phase === "checking" || flow.phase === "leaving");
  useEffect(() => {
    setSlow(false);
    if (!busyPhase) return undefined;
    const timer = window.setTimeout(() => setSlow(true), REVEAL_AFTER_MS);
    return () => window.clearTimeout(timer);
  }, [busyPhase, flow?.notebookId]);

  useEffect(() => () => { controllerRef.current?.abort(); }, []);

  /** 面板关闭之后把焦点还给打开它的控件;控件已不在(卡片随退出消失)时落到页面主体。 */
  const restoreFocus = useCallback((anchor: ExitAnchor) => {
    window.setTimeout(() => {
      const active = document.activeElement;
      if (active && active !== document.body) return; // 用户已经把焦点带去别处
      const element = anchor.element;
      if (element && element.isConnected && !element.closest("[inert]")
        && !(element as HTMLButtonElement).disabled) {
        element.focus();
        return;
      }
      const main = document.querySelector<HTMLElement>("main");
      if (main) {
        if (!main.hasAttribute("tabindex")) main.setAttribute("tabindex", "-1");
        main.focus();
      }
    }, 0);
  }, []);

  const runCheck = useCallback(async (id: string, epoch: number, mode: CheckMode) => {
    const controller = new AbortController();
    controllerRef.current = controller;
    let count: number | null = null;
    try {
      count = (await getExitDisclosure(id, controller.signal)).memory_count;
    } catch {
      count = null;
    }
    if (controllerRef.current === controller) controllerRef.current = null;
    if (epochRef.current !== epoch) return; // 已取消/被替换:不再往下走,更不能发 DELETE
    if (count === null) {
      // 静默重读失败时保留服务端刚给的数字与说明;只有首次/转移后的读取才退到「不盲退」。
      update(mode === "quiet" ? { recounting: false } : { phase: "unavailable", recounting: false, failure: "" });
      return;
    }
    if (count === 0 && mode === "first") {
      // 直接退出:条数为 0,不惊动面板(慢了才露出)。
      await sendLeaveRef.current(id, 0);
      return;
    }
    update({ phase: "confirm", count, recounting: false });
  }, [update]);

  const afterLeft = useCallback(async (id: string, deleted: number | null) => {
    const anchor = flowRef.current?.notebookId === id ? flowRef.current.anchor : null;
    if (anchor) publish(null);
    try {
      await (afterLeaveRef.current.get(id) ?? (async () => {}))();
    } catch (error) {
      handlersRef.current.onError(error);
    }
    // 刷新列表失败也要告诉用户退出已经发生(以及删了几条):这条提示是永久删除的唯一确认。
    handlersRef.current.onToast(leftText(deleted ?? 0));
    if (anchor) restoreFocus(anchor);
  }, [publish, restoreFocus]);

  const settle = useCallback(async (
    id: string,
    outcome: LeaveOutcome | { kind: "failed"; cause: unknown },
    state: ExitState | null,
  ) => {
    const attached = flowRef.current?.notebookId === id;
    const toast = handlersRef.current.onToast;
    const recount = () => { void runCheck(id, epochRef.current, "quiet"); };
    const reopen = (patch: Partial<ExitFlow>) => updateFor(id, {
      phase: "confirm", leaving: false, verifying: false, recounting: false, ...patch,
    });
    switch (outcome.kind) {
      case "left":
        await afterLeft(id, outcome.deleted);
        return;
      case "disclosure":
        // 什么都没删、仍是成员。面板还在就换成服务端的新数字;面板已关(用户取消了)无事可报。
        reopen({ count: outcome.memoryCount, changed: true, failure: "", notice: "" });
        return;
      case "incomplete":
        if (!attached) {
          toast(outcome.status === 409
            ? incompleteLateText(outcome.deleted, outcome.remaining)
            : incompleteFailedText(outcome.deleted, outcome.remaining));
          return;
        }
        reopen(outcome.status === 409
          ? { count: outcome.remaining, changed: false, failure: "", notice: incompleteConfirmText(outcome.deleted, outcome.remaining) }
          : { count: outcome.remaining, changed: false, notice: "", failure: incompleteFailedText(outcome.deleted, outcome.remaining) });
        // 契约:未完成之后从告知重新开始——静默重读一次,以服务端此刻的条数为准。
        updateFor(id, { recounting: true });
        recount();
        return;
      case "failed":
        if (!attached) {
          handlersRef.current.onError(outcome.cause);
          return;
        }
        reopen({ failure: toUserMessage(outcome.cause, EXIT_FAILED_TEXT), notice: "", changed: false });
        return;
      default:
        break;
    }
    // unknown:传输中断/超时。核对之后才说话。
    if (state?.left) {
      await afterLeft(id, null);
      return;
    }
    if (state) {
      const text = stillMemberText(state.remaining);
      if (!attached) toast(text);
      else reopen({ count: state.remaining, changed: false, failure: "", notice: text });
      return;
    }
    if (!attached) toast(EXIT_UNVERIFIED_TEXT);
    else updateFor(id, { phase: "unavailable", leaving: false, verifying: false, failure: EXIT_UNVERIFIED_TEXT });
  }, [afterLeft, runCheck, updateFor]);

  const sendLeave = useCallback(async (id: string, acknowledged: number) => {
    leavingRef.current.add(id);
    bump();
    const current = flowRef.current;
    if (current?.notebookId === id) {
      update({
        leaving: true, failure: "", notice: "", changed: false,
        phase: current.phase === "checking" ? "leaving" : current.phase,
      });
    }
    let outcome: LeaveOutcome | { kind: "failed"; cause: unknown };
    try {
      outcome = await leaveNotebook(id, acknowledged);
    } catch (error) {
      outcome = { kind: "failed", cause: error };
    }
    let state: ExitState | null = null;
    if (outcome.kind === "unknown") {
      updateFor(id, { verifying: true });
      try {
        state = await readExitState(id);
      } catch {
        state = null;
      }
    }
    leavingRef.current.delete(id);
    bump();
    await settle(id, outcome, state);
  }, [settle, update, updateFor]);

  sendLeaveRef.current = sendLeave;

  /** 按下「退出共享」。`afterLeave` 由入口给:各入口退出后对账的方式不同。 */
  const start = useCallback((notebookId: string, anchor: ExitAnchor, afterLeave: () => Promise<void>) => {
    if (flowRef.current?.notebookId === notebookId) return; // 双击:这本已经在流程里
    afterLeaveRef.current.set(notebookId, afterLeave);
    const epoch = ++epochRef.current;
    controllerRef.current?.abort(); // 上一本笔记本的读取作废;它已发出的 DELETE 不受影响
    controllerRef.current = null;
    // 这本笔记本上一次 DELETE 还没落地(用户取消过):接回它,不发第二个请求。
    const attached = leavingRef.current.has(notebookId);
    publish({
      notebookId, anchor, phase: attached ? "leaving" : "checking", count: 0, changed: false,
      leaving: attached, verifying: false, recounting: false, failure: "", notice: "",
      transfer: "idle", transferIds: [],
    });
    if (!attached) void runCheck(notebookId, epoch, "first");
  }, [publish, runCheck]);

  /** 收起面板,不动焦点(离开页面时用)。已在飞的请求不撤销,结果照常以提示告知。 */
  const dismiss = useCallback(() => {
    epochRef.current += 1;
    controllerRef.current?.abort();
    controllerRef.current = null;
    if (flowRef.current) publish(null);
  }, [publish]);

  /** 取消:收起确认框并把焦点还回去,什么都不发。永远可用。 */
  const cancel = useCallback(() => {
    const anchor = flowRef.current?.anchor;
    dismiss();
    if (anchor) restoreFocus(anchor);
  }, [dismiss, restoreFocus]);

  const confirm = useCallback(() => {
    const current = flowRef.current;
    if (!current || current.phase !== "confirm") return;
    if (leavingRef.current.has(current.notebookId)) return;
    if (current.recounting || current.transfer !== "idle") return;
    void sendLeave(current.notebookId, current.count);
  }, [sendLeave]);

  const retry = useCallback(() => {
    const current = flowRef.current;
    if (!current || current.phase !== "unavailable" || leavingRef.current.has(current.notebookId)) return;
    update({ phase: "checking", failure: "" });
    void runCheck(current.notebookId, epochRef.current, "first");
  }, [runCheck, update]);

  const exportMemories = useCallback(async () => {
    const current = flowRef.current;
    if (!current || exportingRef.current.has(current.notebookId) || current.leaving) return;
    const id = current.notebookId;
    const key = `export:${id}`;
    exportingRef.current.add(id);
    bump();
    try {
      const { blob, filename } = await exportOwnMemories(id);
      saveBlobAsFile(blob, filename);
      exportResult.report(key, true);
    } catch {
      exportResult.report(key, false);
    } finally {
      exportingRef.current.delete(id);
      bump();
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

  /** 关掉选择器。**这个流程**的转移请求还在途时保留「转移中」状态:结果落地后照常汇报并
   *  重读条数。别的(已取消的)流程的转移不算——它落地时只发提示,不会来解开这个面板。 */
  const closeTransfer = useCallback(() => {
    if (transferEpochRef.current !== null && transferEpochRef.current === epochRef.current) {
      update({ transfer: "working" });
    } else {
      update({ transfer: "idle", transferIds: [] });
    }
  }, [update]);

  /** 交给既有的目标笔记本选择器(DestinationPicker)。resolve = 已完成,选择器由此卸载。 */
  const submitTransfer = useCallback(async (
    targetNotebookId: string,
    mode: TransferMode,
    targetNotebookName: string,
  ) => {
    const current = flowRef.current;
    if (!current) return;
    const epoch = epochRef.current;
    const id = current.notebookId;
    transferEpochRef.current = epoch;
    try {
      // 服务端一次最多 200 个 id:分批提交,某一批失败不影响后面的批次。
      const batches = await transferMemoriesInBatches(
        current.transferIds, targetNotebookId, mode, transferExtractKg,
      );
      if (batches.results.length === 0 && batches.firstFailure !== null) {
        if (flowRef.current?.notebookId === id && flowRef.current.transfer === "working" && epochRef.current === epoch) {
          // 选择器已被关掉,没人接这个错误:落在面板上。
          update({ transfer: "idle", transferIds: [], failure: toUserMessage(batches.firstFailure, "转移没有成功，请稍后重试。") });
          return;
        }
        throw batches.firstFailure; // 选择器还开着:它就地显示错误,用户可以换个目标重试
      }
      const summary = summarizeTransferResults(batches.results);
      // 移动时「副本已建但源没清掉」不算失败:退出会删掉这条源,副本已经在目标里了。
      const done = summary.succeeded + summary.copiedSourceNotRemoved.length;
      const failed = summary.failed - summary.copiedSourceNotRemoved.length + batches.failed;
      const verb = mode === "move" ? "移动" : "复制";
      const notice = failed > 0
        ? (done > 0
          ? `已${verb} ${done} 条记忆到「${targetNotebookName}」，另有 ${failed} 条没有成功。`
          : `${failed} 条记忆没能${verb}，请稍后重试。`)
        : (done > 1
          ? `已${verb} ${done} 条记忆到「${targetNotebookName}」`
          : `已${verb}到「${targetNotebookName}」`);
      if (epochRef.current !== epoch) {
        handlersRef.current.onToast(notice); // 面板已被取消/换掉:转移确实发生了,照样告知
        return;
      }
      // 转移之后条数变了:就地重读,而不是让用户拿着旧数字去确认。
      update({ transfer: "idle", transferIds: [], notice, failure: "", recounting: true });
      void runCheck(id, epoch, "recount");
    } finally {
      if (transferEpochRef.current === epoch) transferEpochRef.current = null;
    }
  }, [runCheck, transferExtractKg, update]);

  return {
    flow,
    /** 这本笔记本有「读条数 / DELETE」在途(顶栏按钮据此显示「退出中…」)。 */
    isBusy: (notebookId: string) =>
      leavingRef.current.has(notebookId)
      || (flow?.notebookId === notebookId && flow.phase === "checking"),
    /** 在途请求已经拖过了 REVEAL_AFTER_MS:此时才把面板露出来。 */
    slow,
    exporting: flow !== null && exportingRef.current.has(flow.notebookId),
    exportResult: (flow ? exportResult.resultFor(`export:${flow.notebookId}`) : "idle") as CopyResult,
    transferExtractKg,
    setTransferExtractKg,
    start,
    cancel,
    dismiss,
    confirm,
    retry,
    exportMemories,
    openTransfer,
    closeTransfer,
    submitTransfer,
  };
}

export type NotebookExit = ReturnType<typeof useNotebookExit>;
