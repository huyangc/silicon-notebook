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
  if (!safeSupportId) return null;
  return (
    <SupportIdCopyValue
      key={safeSupportId}
      supportId={safeSupportId}
      className={className}
    />
  );
}


function SupportIdCopyValue({
  supportId,
  className,
}: {
  supportId: string;
  className: string;
}) {
  const generationRef = useRef(0);
  const mountedRef = useRef(false);
  const [feedback, setFeedback] = useState("");
  const [copying, setCopying] = useState(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
    };
  }, []);

  async function copySupportId() {
    if (copying) return;
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    setCopying(true);
    setFeedback("");
    const copied = await copyTextSafely(supportId);
    if (!mountedRef.current || generationRef.current !== generation) return;
    setFeedback(copied ? "已复制" : "复制失败，请手动选择支持编号");
    setCopying(false);
  }

  return (
    <span className={className}>
      <span>支持编号：</span>
      <code>{supportId}</code>
      <button type="button" disabled={copying} onClick={() => { void copySupportId(); }}>
        {copying ? "复制中…" : "复制支持编号"}
      </button>
      {feedback && <span role="status" aria-live="polite">{feedback}</span>}
    </span>
  );
}
