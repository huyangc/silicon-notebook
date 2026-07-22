"use client";

import { useEffect, useState } from "react";

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
  const [feedback, setFeedback] = useState("");
  const [copying, setCopying] = useState(false);

  useEffect(() => {
    setFeedback("");
    setCopying(false);
  }, [safeSupportId]);

  if (!safeSupportId) return null;

  async function copySupportId() {
    if (copying) return;
    setCopying(true);
    setFeedback("");
    const copied = await copyTextSafely(safeSupportId);
    setFeedback(copied ? "已复制" : "复制失败，请手动选择支持编号");
    setCopying(false);
  }

  return (
    <span className={className}>
      <span>支持编号：</span>
      <code>{safeSupportId}</code>
      <button type="button" disabled={copying} onClick={() => { void copySupportId(); }}>
        {copying ? "复制中…" : "复制支持编号"}
      </button>
      {feedback && <span role="status" aria-live="polite">{feedback}</span>}
    </span>
  );
}
