export const KG_TYPE_STYLE: Record<
  string,
  { color: string; border: string; text: string; glyph: string }
> = {
  concept: { color: "#2f80ed", border: "#1555a8", text: "C", glyph: "circle" },
  claim: { color: "#16a085", border: "#0f6f5f", text: "CL", glyph: "triangle" },
  formula: { color: "#a855f7", border: "#6d28d9", text: "F", glyph: "diamond" },
  procedure: { color: "#f59e0b", border: "#b45309", text: "P", glyph: "square" },
};

const KG_TYPE_LABELS: Record<string, string> = {
  concept: "Concept",
  claim: "Claim",
  formula: "Formula",
  procedure: "Procedure",
};

export function kgTypeLabel(type: string): string {
  return KG_TYPE_LABELS[type]
    ?? type.replace(
      /(^|_)([a-z])/g,
      (_match, separator, char) => `${separator ? " " : ""}${char.toUpperCase()}`,
    );
}

export function KgTypeMark({ type }: { type: string }) {
  const style = KG_TYPE_STYLE[type] ?? {
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
