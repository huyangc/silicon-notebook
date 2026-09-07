"use client";

/**
 * edge-review-modal.tsx
 *
 * 「关系审核队列」弹窗（root modal slot `edge-review`）的呈现层。从 page.tsx 原样搬出，
 * 状态与写入在 `use-edge-review-queue.ts`。协调器语义与关闭 reason 的处理同
 * `promotion-queue-modal.tsx`，见那个文件的说明。
 */

import { formatEdgeReviewQueueTitle, type EdgeReviewItem } from "./edge-review-queue";
import { FloatingModalCard } from "./floating-modal-card";

export function EdgeReviewModal({
  edges,
  total,
  busy,
  interactive = true,
  zIndex,
  onRequestClose,
  onDecide,
}: {
  edges: readonly EdgeReviewItem[];
  /** 端点报的真实队列长度（可能大于本页 `edges.length`）；还没拉到时是 null。 */
  total: number | null;
  busy: boolean;
  interactive?: boolean;
  zIndex?: number;
  onRequestClose: (reason: "button" | "backdrop") => void;
  onDecide: (relId: string, status: "verified" | "rejected") => void;
}) {
  return (
    <section
      className="utility-modal"
      role="dialog"
      aria-modal={interactive}
      aria-hidden={!interactive}
      inert={interactive ? undefined : true}
      style={{ zIndex }}
      onClick={(event) => { if (event.currentTarget === event.target) onRequestClose("backdrop"); }}
    >
      <FloatingModalCard storageKey="edgeReview.window" className="utility-modal-card">
        {(floating) => (<>
        <div className="source-modal-header" {...floating.dragHandleProps}>
          <div>
            <h2>关系审核队列{formatEdgeReviewQueueTitle(total, edges.length)}</h2>
            <p>按「高中心性 × 低可信」排序的关系。确认可信的关联，或拒绝错误的关联（被拒的关联将从所有图推理遍历中排除）。</p>
          </div>
          <button className="icon-button" onClick={() => onRequestClose("button")} title="Close">×</button>
        </div>
        <div className="source-detail-body">
          {edges.length === 0 ? (
            <p className="tool-hint">暂无待审关系。</p>
          ) : (
            <div className="stack">
              {edges.map((edge) => (
                <article className="item" key={edge.rel_id}>
                  <div className="tag-row">
                    <span className="tag">{edge.edge_type}</span>
                    <span className="tag">{edge.review_status}</span>
                    <span className="tag">可信 {edge.trust_score.toFixed(2)}</span>
                    <span className="tag">优先级 {edge.review_priority.toFixed(2)}</span>
                  </div>
                  <h3>{(edge.source_name || edge.source_object_id)} → {(edge.target_name || edge.target_object_id)}</h3>
                  {edge.review_status !== "rejected" && (
                    <div className="modal-actions">
                      <button
                        className="sort-button"
                        disabled={busy}
                        onClick={() => onDecide(edge.rel_id, "rejected")}
                      >
                        拒绝
                      </button>
                      <button
                        className="new-pill"
                        disabled={busy}
                        onClick={() => onDecide(edge.rel_id, "verified")}
                      >
                        确认可信
                      </button>
                    </div>
                  )}
                </article>
              ))}
            </div>
          )}
        </div>
        </>)}
      </FloatingModalCard>
    </section>
  );
}
