"use client";
import type { FullRecord } from "../types";
import { formatLatency, shortId, statusClass } from "../format";
import { ChatTranscript } from "./ChatTranscript";
import { CopyButton } from "./CopyButton";

function Meta({ rec }: { rec: FullRecord }) {
  const u = rec.usage ?? {};
  return (
    <div className="detail-meta">
      <span className={`badge ${statusClass(rec.status)}`}>{rec.status}</span>
      <span className="badge kind">{rec.kind}</span>
      <span className="detail-meta-item">model: {rec.model}</span>
      <span className="detail-meta-item">延迟: {formatLatency(rec.latency_ms)}</span>
      {u.total_tokens != null ? (
        <span className="detail-meta-item">
          tokens: {u.prompt_tokens ?? "?"}/{u.completion_tokens ?? "?"}/{u.total_tokens}
        </span>
      ) : null}
      <span className="detail-meta-item">id: {shortId(rec.id)}</span>
      <span className="detail-meta-item">{rec.ts}</span>
      <span className="logrow-spacer" />
      <CopyButton text={JSON.stringify(rec, null, 2)} label="复制整条 JSON" />
    </div>
  );
}

export function LogDetail({ record, loading }: { record: FullRecord | null; loading: boolean }) {
  if (loading) return <div className="logview-detail empty">加载中…</div>;
  if (!record) return <div className="logview-detail empty">← 选择左侧一条记录查看详情</div>;
  return (
    <div className="logview-detail">
      <Meta rec={record} />
      {record.error ? (
        <div className="detail-error">
          <strong>error{record.attempt != null ? `（attempt ${record.attempt}）` : ""}:</strong> {record.error}
        </div>
      ) : null}
      {record.kind === "chat" ? <ChatTranscript rec={record} /> : null}
      {record.kind === "embed" ? (
        <div className="transcript">
          <div className="transcript-section-title">embedding 调用</div>
          <div className="detail-meta-item">input_chars: {record.input_chars ?? "—"}</div>
          <div className="detail-meta-item">dims: {record.dims ?? "—"}</div>
        </div>
      ) : null}
    </div>
  );
}
