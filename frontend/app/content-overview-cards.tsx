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
  return (
    <section className="content-overview" aria-labelledby="content-overview-heading">
      <h2 id="content-overview-heading">内容资产</h2>
      <div className="content-overview-grid">
        <article className="content-overview-card" aria-labelledby="content-overview-memory">
          <div className="content-overview-card-head">
            <h3 id="content-overview-memory">Memory</h3>
            <button type="button" aria-label="查看全部记忆" onClick={() => onOpenMemory(null, null)}>
              查看全部
            </button>
          </div>
          <div className="content-overview-metrics">
            <span>{countLabel(memory.total, "条")}</span>
            <button
              type="button"
              aria-label={`查看 ${countLabel(memory.confirmed, "条")}已确认记忆`}
              onClick={() => onOpenMemory("confirmed", null)}
            >
              已确认 {countLabel(memory.confirmed, "条")}
            </button>
            <button
              type="button"
              aria-label={`查看 ${countLabel(memory.candidate, "条")}待确认记忆`}
              onClick={() => onOpenMemory("candidate", null)}
            >
              待确认 {countLabel(memory.candidate, "条")}
            </button>
          </div>
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
            <h3 id="content-overview-knowhow">Knowhow</h3>
            {readOnly ? <span className="content-overview-read-only">只读</span> : null}
            <button type="button" aria-label="查看全部 Knowhow 表" onClick={() => onOpenKnowhow("all", null)}>
              查看全部
            </button>
          </div>
          <div className="content-overview-metrics">
            <span>{countLabel(knowhow.table_count, "张表")}</span>
            <span>{countLabel(knowhow.row_count, "行")}</span>
            <button
              type="button"
              aria-label={`查看 ${countLabel(knowhow.projection_pending, "张")}待投影表`}
              onClick={() => onOpenKnowhow("projection_pending", null)}
            >
              待投影 {countLabel(knowhow.projection_pending, "张")}
            </button>
            <button
              type="button"
              aria-label={`查看 ${countLabel(knowhow.projection_failed, "张")}投影失败表`}
              onClick={() => onOpenKnowhow("projection_failed", null)}
            >
              投影失败 {countLabel(knowhow.projection_failed, "张")}
            </button>
            <button
              type="button"
              aria-label={`查看含 ${knowhow.stale_code_count} 个过期代码单元格的表`}
              onClick={() => onOpenKnowhow("stale_code", null)}
            >
              代码过期 {countLabel(knowhow.stale_code_count, "格")}
            </button>
          </div>
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
