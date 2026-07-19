export const KG_TYPE_STYLE: Record<
  string,
  { color: string; border: string; text: string; glyph: string }
> = {
  concept: { color: "#2f80ed", border: "#1555a8", text: "C", glyph: "circle" },
  claim: { color: "#16a085", border: "#0f6f5f", text: "CL", glyph: "triangle" },
  formula: { color: "#a855f7", border: "#6d28d9", text: "F", glyph: "diamond" },
  procedure: { color: "#f59e0b", border: "#b45309", text: "P", glyph: "square" },
};


export const KG_TYPE_LABELS: Record<string, string> = {
  concept: "概念 Concept",
  claim: "论断 Claim",
  formula: "公式 Formula",
  procedure: "过程 Procedure",
};


export function kgTypeLabel(type: string): string {
  return Object.hasOwn(KG_TYPE_LABELS, type) ? KG_TYPE_LABELS[type] : type;
}
