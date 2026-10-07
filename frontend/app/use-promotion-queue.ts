"use client";

/**
 * use-promotion-queue.ts
 *
 * 「内容审核」弹窗（root modal slot `promotion-queue`，Track F 治理）的领域 owner。
 * 从 page.tsx 原样搬出：状态（候选列表 / busy / 单飞 operationRef）、开窗与批准/拒绝
 * 两条命令，以及关闭时的载荷清理。呈现在 `promotion-queue-modal.tsx`。
 *
 * 与协调器的接触面是 `Pick<RootModalCoordinator, …>` 的**窄结构类型**：这个 owner 只
 * 需要「发冻结票据 / 发布 / 判票据还新不新 / 取当前票据 / 判自己还持有」这五个命令，
 * 不该看到 requestClose、view、workspace 生命周期。page.tsx 直接把 rootModals 传进来
 * 即可（结构类型自动收窄）。
 *
 * ⚠ 两条不许动的契约（root-modal-boundary 守卫钉住）：
 *   1. 开窗走 issue → await → publish 的**冻结票据**，不是「先开窗再拿数据」；请求
 *      期间用户切了用户/笔记本，publish 会拒绝，陈旧数据不会落进一个新窗口。
 *   2. `clearQueue` 是 close sink（呈现层）调用的，只清可见载荷——**绝不**把
 *      `promoOperationRef` 置空或把 busy 复位：关闭弹窗不等于那条 HTTP 请求结束，
 *      提前放开单飞闸会让同一候选被批准两次。
 */

import { useRef, useState } from "react";

import { toUserMessage } from "./errors";

import {
  approvePromotion,
  fetchPromotionQueue,
  rejectPromotion,
  type PromotionCandidate,
} from "./promotion-queue";
import type { RootModalCoordinator } from "./use-root-modal-coordinator";

export type PromotionQueueModals = Pick<
  RootModalCoordinator,
  "issue" | "publish" | "leaseIsCurrent" | "owns" | "activeLease" | "captureActorOwner"
>;

export type PromotionQueueEffects = {
  notify: (message: string) => void;
  // 批准会把内容并进公共知识库，笔记本列表里的计数/层级随之变化，所以决策落定后
  // 要顺带刷新集合视图（原 page.tsx 的 `loadNotebookCollection()`）。
  refreshCollection: () => Promise<void>;
};

// owner-hidden 回退值必须是稳定引用（hook-view-stable-empty-guard）：候选列表还没
// 拉到时给出的空数组不能是每次渲染新建的 `[]`。
const NO_CANDIDATES: readonly PromotionCandidate[] = Object.freeze([]);
/**
 * 一次决策失败落在那一条上的结果。`closed`:服务端重取的队列里这一条已经不在或已关闭
 * (例如批准时被自动关闭)——此时原因顶替按钮,不让人对着一个再也批不了的申请反复点;
 * 否则(暂时性失败:5xx、断网、重取失败)原因显示在按钮旁边,按钮保留,可以直接重试。
 */
export type PromotionNotice = { reason: string; closed: boolean };

const NO_NOTICES: Readonly<Record<string, PromotionNotice>> = Object.freeze({});
const OPEN_STATUSES = new Set(["proposed", "under_review"]);

/**
 * 决策失败后的队列:以服务端重取的为准,但被点的那一条若已不在队列里(例如服务端在
 * 拒绝批准时把它关闭了),仍按原位置留在界面上,好让失败原因落在它自己身上,而不是
 * 只在页面顶部闪一下——用户点的是这一条,结果就该出现在这一条。关窗或重开即消失。
 */
function keepClickedCandidate(
  previous: readonly PromotionCandidate[] | null,
  refreshed: PromotionCandidate[],
  candidateId: string,
): PromotionCandidate[] {
  if (refreshed.some((cand) => cand.id === candidateId)) return refreshed;
  const index = previous ? previous.findIndex((cand) => cand.id === candidateId) : -1;
  if (!previous || index < 0) return refreshed;
  const next = refreshed.slice();
  next.splice(Math.min(index, next.length), 0, previous[index]);
  return next;
}

export function usePromotionQueue({
  modals,
  effects,
}: {
  modals: PromotionQueueModals;
  effects: PromotionQueueEffects;
}) {
  const [promoQueue, setPromoQueue] = useState<PromotionCandidate[] | null>(null);
  const [promoBusy, setPromoBusy] = useState(false);
  // 按候选 id 记的「这次点击的结果」(失败原因):呈现层画在那一条的按钮位置上。
  const [promoNotices, setPromoNotices] = useState<Record<string, PromotionNotice> | null>(null);
  const promoOperationRef = useRef<object | null>(null);

  async function openPromoQueue() {
    if (promoOperationRef.current) return;
    const modalLease = modals.issue("promotion-queue", modals.captureActorOwner());
    if (!modalLease) return;
    try {
      const queue = await fetchPromotionQueue();
      if (modals.publish(modalLease)) {
        setPromoQueue(queue);
        setPromoNotices(null);
      }
    } catch (error) {
      if (modals.leaseIsCurrent(modalLease)) throw error;
    }
  }

  async function decidePromotion(candidateId: string, decision: "approve" | "reject", reason = "") {
    const modalLease = modals.activeLease("promotion-queue");
    if (!modalLease || promoOperationRef.current) return;
    const operation = {};
    promoOperationRef.current = operation;
    setPromoBusy(true);
    try {
      // Only the decision itself can fail "on this item". What follows a
      // decision that went through (refetch, collection refresh) is not the
      // item's result: a hiccup there must not paint a failure reason onto an
      // item that was just approved/rejected (quality re-review P3-3).
      try {
        if (decision === "approve") {
          const result = await approvePromotion(candidateId);
          if (!modals.owns(modalLease)) return;
          const merged = result.merged_into ? `（与 ${result.merged_into.slice(0, 8)} 合并）` : "";
          effects.notify(`已批准收录${merged}，内容已加入公共知识库`);
        } else {
          await rejectPromotion(candidateId, reason);
          if (!modals.owns(modalLease)) return;
          effects.notify("贡献未采纳，个人内容保持不变");
        }
      } catch (error) {
        if (!modals.owns(modalLease)) return;
        // 决策失败:原因落在被点的那一条上,并按服务端重取队列。重取结果里这一条已经
        // 不在或已关闭(例如批准时被自动关闭)→ 原因顶替按钮;仍待审核(暂时性失败)或
        // 重取本身失败 → 原因放在按钮旁边,按钮保留,可以直接重试。
        const message = toUserMessage(error);
        let refreshed: PromotionCandidate[] | null = null;
        try {
          refreshed = await fetchPromotionQueue();
        } catch {
          refreshed = null;
        }
        if (!modals.owns(modalLease)) return;
        let closed = false;
        if (refreshed) {
          const queue = refreshed;
          const listed = queue.find((cand) => cand.id === candidateId);
          closed = !listed || !OPEN_STATUSES.has(listed.status);
          setPromoQueue((previous) => keepClickedCandidate(previous, queue, candidateId));
        }
        setPromoNotices((previous) => ({ ...(previous ?? {}), [candidateId]: { reason: message, closed } }));
        return;
      }
      if (!modals.owns(modalLease)) return;
      // A retry that went through clears the earlier failure on that item.
      setPromoNotices((previous) => {
        if (!previous || !(candidateId in previous)) return previous;
        const next = { ...previous };
        delete next[candidateId];
        return next;
      });
      try {
        // Refresh queue, then any loaded notebook collection / knowledge list.
        const queue = await fetchPromotionQueue();
        if (!modals.owns(modalLease)) return;
        setPromoQueue(queue);
        await effects.refreshCollection();
      } catch (error) {
        // The decision succeeded and was announced; a failed refresh is a
        // page-level problem, reported like before (never on the item).
        if (modals.owns(modalLease)) throw error;
      }
    } finally {
      if (promoOperationRef.current === operation) {
        promoOperationRef.current = null;
        setPromoBusy(false);
      }
    }
  }

  // 呈现层的 close sink 调用。只丢可见载荷，见文件头契约 2。
  function clearQueue() {
    setPromoQueue(null);
    setPromoNotices(null);
  }

  return {
    view: {
      // Ternary shape (not `??`), matching use-ask-session.ts's NO_TURNS
      // precedent: hook-view-stable-empty-guard only inspects
      // ConditionalExpression branches, not a bare `??` fallback.
      candidates: promoQueue ? promoQueue : NO_CANDIDATES,
      notices: promoNotices ? promoNotices : NO_NOTICES,
      busy: promoBusy,
    },
    openPromoQueue,
    decidePromotion,
    clearQueue,
  };
}
