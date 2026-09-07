"use client";

import { KgEvidenceBody } from "./kg-evidence-body";
import { kgConfidenceLabel } from "./kg-evidence-list";
import { ELEMENT_TYPE, label } from "./vocabulary";
import type { KgOccurrence, KgProcedureStep, UnifiedConceptNode } from "./workspace-model.ts";

/**
 * 知识对象的叶子呈现件与字段/节点名的界面名。
 *
 * 为什么单独一个模块：知识图谱视图（`kg-graph-view.tsx`）与 page.tsx 里的
 * `KnowledgeBrowser` / `fgData` / `selectedKgEdges` **都**要用这几件。从 page.tsx 反向
 * import 会成环，复制一份又会漂移，所以按「单一定义」提到这里，内容逐字照搬。
 */

export function kgNodeName(node: UnifiedConceptNode): string {
  const name = typeof node.payload.name === "string" ? node.payload.name.trim() : "";
  return name || node.id.replace(/^K-/, "");
}

export function KgOccurrenceCard({ occurrence, index }: { occurrence: KgOccurrence; index: number }) {
  const sourceLabel = occurrence.source_title || occurrence.source_id || "未知来源";
  const meta = [
    occurrence.location_label,
    label(ELEMENT_TYPE, occurrence.element_type ?? "", ""),
    kgConfidenceLabel(occurrence.confidence)
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
        elementType={occurrence.element_type}
        text={occurrence.element_text || occurrence.quoted_span}
      />
    </article>
  );
}

export function KgProcedureStepCard({ step, index }: { step: KgProcedureStep; index: number }) {
  return (
    <article className="kg-evidence-card kg-step-card">
      <div className="kg-evidence-header">
        <span className="kg-evidence-index">{index + 1}</span>
        <div className="kg-evidence-source">
          <strong>{step.name || `步骤 ${index + 1}`}</strong>
          <div className="kg-evidence-meta"><span>流程步骤</span></div>
        </div>
      </div>
      <KgEvidenceBody text={step.element_text} />
    </article>
  );
}

// Field-key labels for the generic (case/claim/finding/concept/...) renderer.
const FIELD_LABELS: Record<string, string> = {
  statement: "陈述", claim_type: "类型", measurement_condition: "测量条件",
  limitation: "局限", metric: "指标", condition: "条件", dataset: "数据集",
  term: "术语", definition: "定义", why_it_matters: "意义", related_concepts: "相关概念",
  rationale: "依据", applies_to: "适用范围", problem: "问题", approach: "做法",
  result: "结果", symptom: "症状", context: "背景", root_cause: "根因",
  resolution: "解决", lesson_learned: "经验", required_evidence: "所需证据",
  question: "检查项", related_claims: "相关论断",
  related_formulas: "相关公式", related_procedures: "相关过程"
};

/**
 * 知识对象字段名的界面名。未命中时**刻意**原样显示 key —— 与 relationLabel 的中性
 * 兜底相反,理由是 object schema 允许用户自建类型与字段(「图谱 Schema」里的新增
 * 类型 / 归纳候选),此时 key 就是用户自己起的名字,原样显示才诚实;换成中性词反而
 * 把用户唯一能辨认这个字段的信息抹掉。故它不是「兜底即原值」那个 bug,而是一条经
 * 评审的透出路径,写法与 kg-type-mark.tsx 透出自定义 object_type 保持一致。
 *
 * 用 Object.hasOwn 而非 `FIELD_LABELS[k] ?? k`:后者走原型链,key 撞上
 * "constructor"/"toString"/"__proto__" 时会返回函数/对象,渲染进 JSX 就是
 * "Objects are not valid as a React child" 白屏(vocabulary.ts 的 label() 记着同一个
 * 坑)。字段名由用户自定义,撞上这些词完全可能。
 */
export function fieldLabel(key: string): string {
  return Object.hasOwn(FIELD_LABELS, key) ? FIELD_LABELS[key] : key;
}
