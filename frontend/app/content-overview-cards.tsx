import { AlertTriangle, Brain, Table2 } from "lucide-react";

import type { KnowhowHealthFilter } from "./knowhow-model";
import type { NotebookContentOverview } from "./workspace-model";

export type ContentOverviewCardsProps = {
  overview: NotebookContentOverview | null;
  loading: boolean;
  error: string;
  readOnly: boolean;
  onOpenMemory: (
    status: "candidate" | "confirmed" | null,
    itemId: string | null,
  ) => void;
  onOpenKnowhow: (
    filter: KnowhowHealthFilter,
    tableId: string | null,
  ) => void;
};

function countLabel(count: number, unit: string): string {
  return `${count} ${unit}`;
}

type BarSegment = {
  key: string;
  value: number;
  tone: "ok" | "warn" | "danger" | "neutral";
};

function ProportionBar({
  segments,
  total,
  label,
}: {
  segments: BarSegment[];
  total: number;
  label: string;
}) {
  const sum = segments.reduce((acc, seg) => acc + seg.value, 0);
  const denom = Math.max(total, sum, 1);
  return (
    <div className="content-overview-bar" role="img" aria-label={label}>
      {segments.map((seg) => (
        <span
          key={seg.key}
          className={`seg-${seg.tone}`}
          aria-hidden="true"
          style={{ width: `${(seg.value / denom) * 100}%` }}
        />
      ))}
    </div>
  );
}

export function ContentOverviewCards({
  overview,
  loading,
  error,
  readOnly,
  onOpenMemory,
  onOpenKnowhow,
}: ContentOverviewCardsProps) {
  if (loading) {
    return (
      <section className="content-overview" aria-labelledby="content-overview-heading">
        <h2 id="content-overview-heading">内容资产</h2>
        <p className="content-overview-state" role="status">正在加载内容资产…</p>
      </section>
    );
  }

  if (error || !overview) {
    return (
      <section className="content-overview" aria-labelledby="content-overview-heading">
        <h2 id="content-overview-heading">内容资产</h2>
        <p className="content-overview-state" role="alert">内容资产暂时不可用</p>
      </section>
    );
  }

  const { memory, knowhow } = overview;

  const memoryRemainder = memory.total - memory.confirmed - memory.candidate;
  const memorySegments: BarSegment[] = [
    { key: "confirmed", value: memory.confirmed, tone: "ok" },
    { key: "candidate", value: memory.candidate, tone: "warn" },
  ];
  if (memoryRemainder > 0) {
    memorySegments.push({ key: "other", value: memoryRemainder, tone: "neutral" });
  }

  const knowhowSynced = Math.max(
    0,
    knowhow.table_count - knowhow.projection_pending - knowhow.projection_failed,
  );
  const knowhowSegments: BarSegment[] = [
    { key: "synced", value: knowhowSynced, tone: "ok" },
    { key: "pending", value: knowhow.projection_pending, tone: "warn" },
    { key: "failed", value: knowhow.projection_failed, tone: "danger" },
  ];

  return (
    <section className="content-overview" aria-labelledby="content-overview-heading">
      <h2 id="content-overview-heading">内容资产</h2>
      <div className="content-overview-grid">
        <article className="content-overview-card" aria-labelledby="content-overview-memory">
          <div className="content-overview-card-head">
            <span className="content-overview-ic" aria-hidden="true">
              <Brain size={17} />
            </span>
            <h3 id="content-overview-memory">Memory</h3>
            <button type="button" aria-label="查看全部记忆" onClick={() => onOpenMemory(null, null)}>
              查看全部
            </button>
          </div>
          <p className="content-overview-total">
            <strong>{memory.total}</strong> 条
          </p>
          {memory.total > 0 ? (
            <>
              <ProportionBar
                segments={memorySegments}
                total={memory.total}
                label={`已确认 ${countLabel(memory.confirmed, "条")}，待确认 ${countLabel(memory.candidate, "条")}`}
              />
              <div className="content-overview-legend">
                <button
                  type="button"
                  aria-label={`查看 ${countLabel(memory.confirmed, "条")}已确认记忆`}
                  onClick={() => onOpenMemory("confirmed", null)}
                >
                  <span className="content-overview-dot dot-ok" aria-hidden="true" />
                  已确认 {countLabel(memory.confirmed, "条")}
                </button>
                <button
                  type="button"
                  aria-label={`查看 ${countLabel(memory.candidate, "条")}待确认记忆`}
                  onClick={() => onOpenMemory("candidate", null)}
                >
                  <span className="content-overview-dot dot-warn" aria-hidden="true" />
                  待确认 {countLabel(memory.candidate, "条")}
                </button>
              </div>
            </>
          ) : null}
          {memory.recent.length > 0 ? (
            <div className="content-overview-recent" aria-label="最近记忆">
              {memory.recent.slice(0, 3).map((item) => (
                <button
                  key={item.id}
                  type="button"
                  aria-label={`打开记忆 ${item.title}`}
                  onClick={() => onOpenMemory(null, item.id)}
                >
                  {item.title}
                </button>
              ))}
            </div>
          ) : <p className="content-overview-empty">还没有已保存的记忆</p>}
        </article>

        <article className="content-overview-card" aria-labelledby="content-overview-knowhow">
          <div className="content-overview-card-head">
            <span className="content-overview-ic" aria-hidden="true">
              <Table2 size={17} />
            </span>
            <h3 id="content-overview-knowhow">Knowhow</h3>
            {readOnly ? <span className="content-overview-read-only">只读</span> : null}
            <button type="button" aria-label="查看全部 Knowhow 表" onClick={() => onOpenKnowhow("all", null)}>
              查看全部
            </button>
          </div>
          <p className="content-overview-total">
            <strong>{knowhow.table_count}</strong> 张表
            <span className="content-overview-total-sub">· {countLabel(knowhow.row_count, "行")}</span>
          </p>
          {knowhow.table_count > 0 ? (
            <>
              <ProportionBar
                segments={knowhowSegments}
                total={knowhow.table_count}
                label={`已同步 ${countLabel(knowhowSynced, "张")}，待同步 ${countLabel(knowhow.projection_pending, "张")}，同步失败 ${countLabel(knowhow.projection_failed, "张")}`}
              />
              <div className="content-overview-legend">
                <span className="legend-static">
                  <span className="content-overview-dot dot-ok" aria-hidden="true" />
                  已同步 {countLabel(knowhowSynced, "张")}
                </span>
                <button
                  type="button"
                  aria-label={`查看 ${countLabel(knowhow.projection_pending, "张")}待同步表`}
                  onClick={() => onOpenKnowhow("projection_pending", null)}
                >
                  <span className="content-overview-dot dot-warn" aria-hidden="true" />
                  待同步 {countLabel(knowhow.projection_pending, "张")}
                </button>
                <button
                  type="button"
                  aria-label={`查看 ${countLabel(knowhow.projection_failed, "张")}同步失败表`}
                  onClick={() => onOpenKnowhow("projection_failed", null)}
                >
                  <span className="content-overview-dot dot-danger" aria-hidden="true" />
                  同步失败 {countLabel(knowhow.projection_failed, "张")}
                </button>
              </div>
            </>
          ) : null}
          <button
            type="button"
            className="content-overview-stale"
            aria-label={`查看含 ${knowhow.stale_code_count} 个过期代码单元格的表`}
            onClick={() => onOpenKnowhow("stale_code", null)}
          >
            <AlertTriangle size={13} aria-hidden="true" />
            代码过期 {countLabel(knowhow.stale_code_count, "格")}
          </button>
          {knowhow.recent_tables.length > 0 ? (
            <div className="content-overview-recent" aria-label="最近 Knowhow 表">
              {knowhow.recent_tables.slice(0, 3).map((table) => (
                <button
                  key={table.id}
                  type="button"
                  aria-label={`打开 Knowhow 表 ${table.title}`}
                  onClick={() => onOpenKnowhow("all", table.id)}
                >
                  {table.title}
                </button>
              ))}
            </div>
          ) : <p className="content-overview-empty">还没有 Knowhow 表</p>}
        </article>
      </div>
    </section>
  );
}
