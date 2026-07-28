"use client";

import katex from "katex";

import { unwrapStandaloneLatex } from "./math-markdown";


export function FormulaView({ latex }: { latex: string }) {
  let html = "";
  const normalized = unwrapStandaloneLatex(latex);
  try {
    html = katex.renderToString(normalized, {
      throwOnError: true,
      strict: "ignore",
      displayMode: true,
    });
  } catch {
    html = "";
  }
  if (!html) {
    return <pre className="element-formula-raw">{latex}</pre>;
  }
  return <div className="element-formula" dangerouslySetInnerHTML={{ __html: html }} />;
}
