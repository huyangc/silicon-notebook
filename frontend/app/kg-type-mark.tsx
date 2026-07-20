import { KG_TYPE_STYLE, kgTypeLabel } from "./kg-type-model";

export { KG_TYPE_STYLE, kgTypeLabel } from "./kg-type-model";

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
