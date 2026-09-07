"use client";

/**
 * use-edge-review-queue.ts
 *
 * 「关系审核队列」弹窗（root modal slot `edge-review`，Track E）的领域 owner。与
 * `use-promotion-queue.ts` 同形，差别只有两处：owner 是 workspace（队列属于某个笔记本，
 * 不是某个用户），以及 view 多带一个 `total`——`total` 是端点报的**真实**队列长度，与被
 * `limit` 截断的当前页 `items` 是两个数（R3 T-A3），弹窗抬头要靠它说「共 N 条」。
 *
 * 呈现在 `edge-review-modal.tsx`。协调器接触面同样是 `Pick<RootModalCoordinator, …>` 的
 * 窄结构类型，`clearQueue` 同样只清可见载荷、绝不释放在飞的单飞闸（见姊妹文件的契约说明）。
 */

import { useRef, useState } from "react";

import {
  fetchEdgeReviewQueue,
  reviewRelation,
  type EdgeReviewItem,
} from "./edge-review-queue";
import type { RootModalCoordinator } from "./use-root-modal-coordinator";

export type EdgeReviewModals = Pick<
  RootModalCoordinator,
  "issue" | "publish" | "leaseIsCurrent" | "owns" | "activeLease" | "captureWorkspaceOwner"
>;

export type EdgeReviewEffects = {
  notify: (message: string) => void;
};

const NO_EDGES: readonly EdgeReviewItem[] = Object.freeze([]);

export function useEdgeReviewQueue({
  modals,
  effects,
}: {
  modals: EdgeReviewModals;
  effects: EdgeReviewEffects;
}) {
  const [edgeQueue, setEdgeQueue] = useState<EdgeReviewItem[] | null>(null);
  // R3 T-A3: the endpoint's true queue size, independent of the `limit`-bounded
  // `edgeQueue` page above — shown in the modal header as "共 N 条".
  const [edgeQueueTotal, setEdgeQueueTotal] = useState<number | null>(null);
  const [edgeBusy, setEdgeBusy] = useState(false);
  const edgeOperationRef = useRef<object | null>(null);

  async function openEdgeReviewQueue(notebookId: string | null) {
    if (!notebookId || edgeOperationRef.current) return;
    const modalLease = modals.issue("edge-review", modals.captureWorkspaceOwner());
    if (!modalLease || modalLease.owner.kind !== "workspace" || modalLease.owner.notebookId !== notebookId) return;
    try {
      const { items, total } = await fetchEdgeReviewQueue(notebookId);
      if (modals.publish(modalLease)) {
        setEdgeQueue(items);
        setEdgeQueueTotal(total);
      }
    } catch (error) {
      if (modals.leaseIsCurrent(modalLease)) throw error;
    }
  }

  async function decideEdge(relId: string, status: "verified" | "rejected") {
    const modalLease = modals.activeLease("edge-review");
    if (!modalLease || modalLease.owner.kind !== "workspace" || edgeOperationRef.current) return;
    const operation = {};
    edgeOperationRef.current = operation;
    setEdgeBusy(true);
    try {
      await reviewRelation(modalLease.owner.notebookId, relId, status);
      if (!modals.owns(modalLease)) return;
      effects.notify(status === "verified" ? "关系已确认" : "关系已拒绝，后续图推理将忽略它");
      const { items, total } = await fetchEdgeReviewQueue(modalLease.owner.notebookId);
      if (!modals.owns(modalLease)) return;
      setEdgeQueue(items);
      setEdgeQueueTotal(total);
    } catch (error) {
      if (modals.owns(modalLease)) throw error;
    } finally {
      if (edgeOperationRef.current === operation) {
        edgeOperationRef.current = null;
        setEdgeBusy(false);
      }
    }
  }

  function clearQueue() {
    setEdgeQueue(null);
    setEdgeQueueTotal(null);
  }

  return {
    view: {
      // Ternary shape (not `??`), matching use-ask-session.ts's NO_TURNS
      // precedent: hook-view-stable-empty-guard only inspects
      // ConditionalExpression branches, not a bare `??` fallback.
      edges: edgeQueue ? edgeQueue : NO_EDGES,
      total: edgeQueueTotal,
      busy: edgeBusy,
    },
    openEdgeReviewQueue,
    decideEdge,
    clearQueue,
  };
}
