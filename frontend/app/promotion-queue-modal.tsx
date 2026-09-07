"use client";

/**
 * promotion-queue-modal.tsx
 *
 * 「内容审核」弹窗（root modal slot `promotion-queue`）的呈现层。从 page.tsx 原样搬出，
 * 状态与写入在 `use-promotion-queue.ts`；这里只画，不发请求、不持有 busy。
 *
 * 协调器语义照搬 `kg-analysis-view.tsx` / `memory-panel.tsx` 的既定形态：section 自持
 * `aria-modal` / `aria-hidden` / `inert` / `zIndex`，被别的 primary 弹窗盖住时整体退出
 * 交互树。关闭回调带 reason——背景点击是 `"backdrop"`、「×」是 `"button"`，
 * `ROOT_MODAL_POLICIES` 按 reason 判这个 slot 允不允许这样关，两者不可合并成一个无参
 * `onClose`（先例：model-service-panel.tsx 的 `onClose("escape")`）。
 */

import { FloatingModalCard } from "./floating-modal-card";
import { PromotionCandidateActions } from "./promotion-candidate-actions";
import type { PromotionCandidate } from "./promotion-queue";
import { promotionReviewSections } from "./promotion-review";
import { label, PROMOTION_STATUS } from "./vocabulary";

export function PromotionQueueModal({
  candidates,
  busy,
  lookupNotebookName,
  interactive = true,
  zIndex,
  onRequestClose,
  onApprove,
  onReject,
}: {
  candidates: readonly PromotionCandidate[];
  busy: boolean;
  /**
   * 目标公共知识库名字的**第二级**回退。优先用后端 join 出来的 `target_base_name`
   * （策展人不一定是目标库 owner，本地的笔记本列表猜不出别人创建的公共库真名）；
   * 查不到才回退到调用方的本地列表，最后兜底截断 id。
   */
  lookupNotebookName: (notebookId: string) => string | undefined;
  interactive?: boolean;
  zIndex?: number;
  onRequestClose: (reason: "button" | "backdrop") => void;
  onApprove: (candidateId: string) => void;
  onReject: (candidateId: string) => void;
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
      <FloatingModalCard storageKey="promotion.window" className="utility-modal-card">
        {(floating) => (<>
        <div className="source-modal-header" {...floating.dragHandleProps}>
          <div>
            <h2>内容审核</h2>
            <p>个人知识库中的内容与记忆候选申请收录到公共知识库。批准后会合并重复并加入所选的目标公共知识库。</p>
          </div>
          <button className="icon-button" onClick={() => onRequestClose("button")} title="Close">×</button>
        </div>
        <div className="source-detail-body">
          {candidates.length === 0 ? (
            <p className="tool-hint">暂无待审核的收录申请。</p>
          ) : (
            <div className="stack">
              {candidates.map((cand) => {
                const review = promotionReviewSections(cand);
                return (
                <article className="item" key={cand.id}>
                  <div className="tag-row">
                    <span className="tag">{label(PROMOTION_STATUS, cand.status, "处理中")}</span>
                    <span className="tag">{cand.object_type}</span>
                    {cand.source_kind === "memory" && <span className="tag">记忆提取候选</span>}
                    {cand.source_kind === "memory" && review.sourceRevision > 0 && (
                      <span className="tag">固定修订 #{review.sourceRevision}</span>
                    )}
                    {cand.base_match_id && (
                      <span className="tag conflict">疑似重复: {cand.base_match_id.slice(0, 10)}</span>
                    )}
                  </div>
                  <h3>{String((cand.payload as Record<string, unknown>).name ?? (cand.payload as Record<string, unknown>).title ?? cand.object_id)}</h3>
                  {cand.source_kind === "memory" && review.candidates.length > 0 && (
                    <div className="stack" aria-label="记忆待审知识对象">
                      {review.candidates.map((item, index) => (
                        <section className="item" key={`${cand.id}-${item.objectType}-${index}`}>
                          <strong>{item.objectType}</strong>
                          {item.fields.map(([label, value]) => (
                            <div key={label}>
                              <span className="tool-hint">{label}</span>
                              <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{value}</div>
                            </div>
                          ))}
                        </section>
                      ))}
                    </div>
                  )}
                  <p className="tool-hint">来源笔记本: {cand.notebook_id.slice(0, 10)}</p>
                  {cand.target_base_id && (
                    <p className="tool-hint">
                      {/* Task 13 审查 #4:优先用后端 join 出来的 target_base_name(策展人不一定是
                          目标库 owner,本地笔记本列表只覆盖自有∪只读加入,猜不出别人创建的公共库
                          真名)；查不到再回退调用方的本地列表(lookupNotebookName),最后兜底截断 id。 */}
                      目标公共知识库: {cand.target_base_name || lookupNotebookName(cand.target_base_id) || cand.target_base_id.slice(0, 10)}
                    </p>
                  )}
                  {review.evidence.length > 0 && (
                    <div className="stack" aria-label="服务端校验证据">
                      <strong>证据</strong>
                      {review.evidence.map((evidence, index) => (
                        <article className="item" key={`${cand.id}-evidence-${index}`}>
                          <div className="tool-hint">
                            {evidence.sourceTitle || "来源"}
                            {evidence.locationLabel ? ` · ${evidence.locationLabel}` : ""}
                          </div>
                          <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                            {evidence.quotedSpan}
                          </div>
                        </article>
                      ))}
                    </div>
                  )}
                  {cand.base_match_id && (
                    <p className="conflict-note">公共知识库中已有相似内容 — 批准后将合并。</p>
                  )}
                  {(cand.status === "proposed" || cand.status === "under_review") && (
                    <PromotionCandidateActions
                      hasTargetBase={Boolean(cand.target_base_id)}
                      busy={busy}
                      onApprove={() => onApprove(cand.id)}
                      onReject={() => onReject(cand.id)}
                    />
                  )}
                </article>
                );
              })}
            </div>
          )}
        </div>
        </>)}
      </FloatingModalCard>
    </section>
  );
}
