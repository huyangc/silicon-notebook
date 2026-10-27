"use client";

import { useEffect, useRef, useState, type RefObject } from "react";
import type { NotebookDeletions } from "./notebook-deletions-block";

export const DELETION_RECEIPT_HOLD_MS = 3500;
export const DELETION_RECEIPT_FOLD_MS = 550;

/** 每个删除窗口只提示一次；完整说明和操作保留在铃铛里。 */
export function NotebookDeletionReceipt({ deletions, enabled, destinationRef, onOpen, introduced }: {
  deletions: NotebookDeletions;
  enabled: boolean;
  destinationRef: RefObject<HTMLButtonElement | null>;
  onOpen: () => void;
  introduced?: Set<string>;
}) {
  const seen = useRef(new Set<string>());
  const introducedWindows = introduced ?? seen.current;
  const [receipt, setReceipt] = useState<{ text: string } | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const { state } = deletions;

  useEffect(() => {
    if (!enabled) { setReceipt(null); return; }
    if (state.status !== "ready") return;
    const fresh = state.items.filter((item) => !introducedWindows.has(`${item.notebook_id}@${item.delete_after}`));
    if (!fresh.length) return;
    for (const item of fresh) introducedWindows.add(`${item.notebook_id}@${item.delete_after}`);
    setReceipt({ text: fresh.length === 1 ? `「${fresh[0].name}」已安排删除。` : `${fresh.length} 本笔记本已安排删除。` });
  }, [enabled, state, introducedWindows]);

  useEffect(() => {
    if (!receipt) return;
    const card = cardRef.current;
    let finishTimer: ReturnType<typeof setTimeout> | undefined;
    const animations: Animation[] = [];
    const timer = setTimeout(() => {
      const container = containerRef.current;
      const bell = destinationRef.current;
      const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      if (!card || !container || !bell || reduced || !card.animate) { setReceipt(null); return; }
      const from = card.getBoundingClientRect();
      const to = bell.getBoundingClientRect();
      Object.assign(card.style, { position: "fixed", left: `${from.left}px`, top: `${from.top}px`, width: `${from.width}px`, margin: "0", zIndex: "45" });
      const options = { duration: DELETION_RECEIPT_FOLD_MS, easing: "cubic-bezier(.4,0,.2,1)", fill: "forwards" as const };
      animations.push(container.animate([{ height: `${from.height}px` }, { height: "0px" }], options));
      animations.push(card.animate([
        { transform: "translate(0, 0) scale(1)", opacity: 1 },
        { transform: `translate(${to.left + to.width / 2 - from.left - from.width / 2}px, ${to.top + to.height / 2 - from.top - from.height / 2}px) scale(.04)`, opacity: 0 },
      ], options));
      // 定时兜底：后台页和动画事件缺失都不会让提示一直占位。
      finishTimer = setTimeout(() => setReceipt(null), DELETION_RECEIPT_FOLD_MS);
    }, DELETION_RECEIPT_HOLD_MS);
    return () => {
      clearTimeout(timer);
      clearTimeout(finishTimer);
      animations.forEach((animation) => animation.cancel());
      card?.removeAttribute("style");
    };
  }, [receipt, destinationRef]);

  if (!enabled || !receipt) return null;
  return (
    <div ref={containerRef} className="notebook-deletion-receipt-wrap">
      <div ref={cardRef} className="notebook-deletion-receipt" role="status">
        {receipt.text}后续操作请打开
        <button type="button" className="link-button inline-action" onClick={onOpen}>待确认中心</button>。
      </div>
    </div>
  );
}
