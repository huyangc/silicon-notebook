"use client";

import { FormulaView } from "./formula-view";


export function KgEvidenceBody({
  text,
  elementType,
}: {
  text?: string | null;
  elementType?: string | null;
}) {
  const value = (text ?? "").trim();
  if (!value) {
    return <p className="kg-evidence-body">暂无原文片段</p>;
  }
  if ((elementType ?? "").trim().toLowerCase() === "formula") {
    return (
      <div className="kg-evidence-body">
        <FormulaView latex={value} />
      </div>
    );
  }
  return <p className="kg-evidence-body">{value}</p>;
}
