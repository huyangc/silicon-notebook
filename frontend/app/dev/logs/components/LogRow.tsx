"use client";
import type { ScopedSummary } from "../types";
import { formatLatency, formatTokens, statusClass } from "../format";

export function LogRow({
  rec,
  selected,
  onSelect,
}: {
  rec: ScopedSummary;
  selected: boolean;
  onSelect: (rec: ScopedSummary) => void;
}) {
  return (
    <button className={`logrow${selected ? " selected" : ""}`} onClick={() => onSelect(rec)}>
      <div className="logrow-head">
        <span className={`badge ${statusClass(rec.status)}`}>{rec.status}</span>
        <span className="badge kind">{rec.kind}</span>
        <span className="logrow-model">{rec.model}</span>
        <span className="logrow-spacer" />
        <span className="logrow-num">{formatLatency(rec.latency_ms)}</span>
        <span className="logrow-num">{formatTokens(rec.total_tokens)} tok</span>
      </div>
      <div className="logrow-preview">{rec.preview || "（无预览）"}</div>
      <div className="logrow-ts">{rec.ts}</div>
    </button>
  );
}
