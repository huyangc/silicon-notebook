"use client";
import type { Summary } from "../types";
import { LogRow } from "./LogRow";

export function LogList({
  records,
  selectedId,
  onSelect,
  hasMore,
  onLoadMore,
  newCount,
  onShowNew,
  loading,
}: {
  records: Summary[];
  selectedId: string | null;
  onSelect: (rec: Summary) => void;
  hasMore: boolean;
  onLoadMore: () => void;
  newCount: number;
  onShowNew: () => void;
  loading: boolean;
}) {
  return (
    <div className="logview-list">
      {newCount > 0 ? (
        <button className="newbar" onClick={onShowNew}>
          ↑ {newCount} 条新记录
        </button>
      ) : null}
      {records.length === 0 && !loading ? <div className="empty">没有匹配的记录</div> : null}
      {records.map((r) => (
        <LogRow key={`${r.seq}-${r.id}`} rec={r} selected={r.id === selectedId} onSelect={onSelect} />
      ))}
      {loading ? <div className="empty">加载中…</div> : null}
      {hasMore ? (
        <button className="loadmore" onClick={onLoadMore}>
          加载更多
        </button>
      ) : null}
    </div>
  );
}
