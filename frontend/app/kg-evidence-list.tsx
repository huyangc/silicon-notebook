"use client";

import { useEffect, useState } from "react";

import { KgEvidenceBody } from "./kg-evidence-body";
import { type EvidenceItem } from "./workspace-model";
import { label, ELEMENT_TYPE } from "./vocabulary";

const EVIDENCE_PAGE_SIZE = 20;

export function kgConfidenceLabel(confidence?: number) {
  if (typeof confidence !== "number" || !Number.isFinite(confidence)) return "";
  const normalized = confidence > 1 ? confidence : confidence * 100;
  return `置信 ${Math.round(normalized)}%`;
}

export function KgEvidenceCard({ evidence, index }: { evidence: EvidenceItem; index: number }) {
  const sourceLabel = evidence.source_title || evidence.source_id || "未知来源";
  const meta = [
    evidence.location_label,
    label(ELEMENT_TYPE, evidence.element_type, ""),
    kgConfidenceLabel(evidence.confidence)
  ].filter(Boolean);

  return (
    <article className="kg-evidence-card">
      <div className="kg-evidence-header">
        <span className="kg-evidence-index">{index + 1}</span>
        <div className="kg-evidence-source">
          <strong title={sourceLabel}>{sourceLabel}</strong>
          {meta.length > 0 && (
            <div className="kg-evidence-meta">
              {meta.map((item) => <span key={item}>{item}</span>)}
            </div>
          )}
        </div>
      </div>
      <KgEvidenceBody
        elementType={evidence.element_type}
        text={evidence.element_text || evidence.quoted_span}
      />
    </article>
  );
}

/**
 * 出处列表的渐进披露（codex PR #639 R1 P2）：初始只渲染前 20 条，「显示更多
 * 出处」按钮每次 +20。`resetKey` 标识「同一份首页世代」——概念详情里「加载
 * 更多成员」把新成员的出处并进同一份 `evidence`（`resetKey` 不变，保留用户
 * 已展开的进度）；换节点或**任何**首页重载（含同概念的 merge/rebuild 刷新，
 * codex #639 R2 P2——canonical_id 单独作键在这里不变化）会换 `resetKey`
 * （概念详情用 `canonical_id:conceptDetailGeneration`，世代号在
 * setConceptDetailFirstPage 每次首页落地时自增），
 * 回落到 20 条。这条重置规则是本组件唯一的状态管理职责，其余渲染都是纯函数。
 */
export function KgEvidenceList({ evidence, resetKey }: { evidence: EvidenceItem[]; resetKey: string }) {
  const [visibleCount, setVisibleCount] = useState(EVIDENCE_PAGE_SIZE);
  useEffect(() => {
    setVisibleCount(EVIDENCE_PAGE_SIZE);
  }, [resetKey]);

  if (evidence.length === 0) return <p className="tool-hint">无</p>;

  return (
    <>
      {evidence.slice(0, visibleCount).map((ev, i) => (
        <KgEvidenceCard evidence={ev} index={i} key={`${ev.source_id}-${ev.element_id}-${i}`} />
      ))}
      {evidence.length > visibleCount && (
        <button
          type="button"
          className="kg-load-more-evidence"
          onClick={() => setVisibleCount((count) => count + EVIDENCE_PAGE_SIZE)}
        >
          {`显示更多出处（已显示 ${visibleCount}/共 ${evidence.length}）`}
        </button>
      )}
    </>
  );
}
