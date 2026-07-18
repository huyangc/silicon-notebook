export const KG_TYPE_STYLE: Record<
  string,
  { color: string; border: string; text: string; glyph: string }
> = {
  concept: { color: "#2f80ed", border: "#1555a8", text: "C", glyph: "circle" },
  claim: { color: "#16a085", border: "#0f6f5f", text: "CL", glyph: "triangle" },
  formula: { color: "#a855f7", border: "#6d28d9", text: "F", glyph: "diamond" },
  procedure: { color: "#f59e0b", border: "#b45309", text: "P", glyph: "square" },
};

// 内置类型显示名——逐字等于后端 OBJECT_TYPE_LABELS(extraction_profiles.py),
// 由 scripts/check_object_type_labels_contract.py 钉住。中英并排是刻意的:后端同款
// label 参与搜索匹配,前端保持一致以求全站统一。这张小表服务「只有 object_type
// 字符串、拿不到 API label」的调用点(引用浮层、图节点);能拿到 API label 的调用点
// (KnowledgeBrowser)走 API label,覆盖自定义类型的中文名。
const KG_TYPE_LABELS: Record<string, string> = {
  concept: "概念 Concept",
  claim: "论断 Claim",
  formula: "公式 Formula",
  procedure: "过程 Procedure",
};

export function kgTypeLabel(type: string): string {
  // 自定义/未知类型显示原 object_type(用户自己起的 id)——诚实,不再 TitleCase 成
  // 假英文(evidence_tier → "Evidence Tier" 那种泄漏)。Object.hasOwn 而非
  // KG_TYPE_LABELS[type]:后者走原型链,map["constructor"] 会返回函数(PR A 教训)。
  return Object.hasOwn(KG_TYPE_LABELS, type) ? KG_TYPE_LABELS[type] : type;
}

export function KgTypeMark({ type }: { type: string }) {
  // Object.hasOwn 而非 KG_TYPE_STYLE[type]:后者走原型链,自定义类型名为 "constructor"/
  // "__proto__" 时会命中继承属性(函数/对象),style.glyph/color 变 undefined、渲染异常。
  // 与 kgTypeLabel 同款防护(PR A 原型链教训)。
  const style = Object.hasOwn(KG_TYPE_STYLE, type) ? KG_TYPE_STYLE[type] : {
    color: "#64748b",
    border: "#334155",
    text: type.slice(0, 2).toUpperCase(),
    glyph: "circle",
  };
  return (
    <span
      className={`kg-shape-mark ${style.glyph}`}
      style={{ background: style.color, borderColor: style.border }}
      aria-hidden="true"
    >
      {style.glyph === "circle" ? style.text : ""}
    </span>
  );
}
