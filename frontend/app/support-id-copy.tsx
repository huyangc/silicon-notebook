"use client";

import { useEffect, useRef, useState } from "react";

import { copyTextSafely } from "./copy-text";
import { sanitizeModelSupportId } from "./model-services";


export function SupportIdCopy({
  supportId,
  className,
}: {
  supportId: unknown;
  className: string;
}) {
  const safeSupportId = sanitizeModelSupportId(supportId);
  const supportIdRef = useRef(safeSupportId);
  const generationRef = useRef(0);
  const mountedRef = useRef(false);
  const [feedback, setFeedback] = useState<{
    supportId: string;
    generation: number;
    text: string;
  } | null>(null);
  const [copying, setCopying] = useState<{
    supportId: string;
    generation: number;
  } | null>(null);

  if (supportIdRef.current !== safeSupportId) {
    supportIdRef.current = safeSupportId;
    generationRef.current += 1;
  }

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
    };
  }, []);

  if (!safeSupportId) return null;

  const isCopying = copying?.supportId === safeSupportId
    && copying.generation === generationRef.current;
  const feedbackMessage = feedback?.supportId === safeSupportId
    && feedback.generation === generationRef.current
    ? feedback.text
    : "";

  async function copySupportId() {
    if (isCopying) return;
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    const request = { supportId: safeSupportId, generation };
    setCopying(request);
    setFeedback(null);
    const copied = await copyTextSafely(safeSupportId);
    if (
      !mountedRef.current
      || generationRef.current !== generation
      || supportIdRef.current !== safeSupportId
    ) return;
    setFeedback({
      ...request,
      text: copied ? "已复制" : "复制失败，请手动选择支持编号",
    });
    setCopying(null);
  }

  return (
    <span className={className}>
      <span>支持编号：</span>
      <code>{safeSupportId}</code>
      <button type="button" disabled={isCopying} onClick={() => { void copySupportId(); }}>
        {isCopying ? "复制中…" : "复制支持编号"}
      </button>
      {feedbackMessage && <span role="status" aria-live="polite">{feedbackMessage}</span>}
    </span>
  );
}
