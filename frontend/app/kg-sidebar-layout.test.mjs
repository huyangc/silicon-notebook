import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const page = await readFile(new URL("./page.tsx", import.meta.url), "utf8");
const css = await readFile(new URL("./globals.css", import.meta.url), "utf8");

function zIndexFor(selector) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escapedSelector}\\s*\\{[^}]*z-index:\\s*(\\d+);`, "s"));
  assert.ok(match, `Expected ${selector} to define a z-index`);
  return Number(match[1]);
}

test("KG sidebar renders source evidence as structured cards", () => {
  assert.ok(page.includes("function KgEvidenceCard"));
  assert.ok(page.includes("function KgOccurrenceCard"));
  assert.ok(page.includes("className=\"kg-evidence-card\""));
  assert.ok(page.includes("className=\"kg-evidence-body\""));
  assert.ok(page.includes("className=\"kg-evidence-meta\""));
  assert.equal(page.includes('<div className="kg-evidence" key={i}><span className="tag">{ev.source_title || ev.source_id}</span>'), false);
});

test("KG evidence text has its own wrapping block instead of a narrow flex row", () => {
  assert.match(css, /\.kg-evidence-card\s*{/);
  assert.match(css, /\.kg-evidence-body\s*{/);
  assert.match(css, /overflow-wrap:\s*anywhere;/);
  assert.doesNotMatch(css, /\.kg-evidence\s*{[^}]*display:\s*flex;/s);
});

test("KG merge decisions optimistically remove the decided pending row", () => {
  assert.match(page, /function mergePairKey\(candidate: PendingMerge\): string/);
  assert.match(page, /async function decideMerge\(candidate: PendingMerge, confirm: boolean\)/);
  assert.match(page, /const decidedPairKey = mergePairKey\(candidate\);/);
  assert.match(page, /confirmMergeApi\(currentNotebookId, candidate\.id\)/);
  assert.match(page, /rejectMergeApi\(currentNotebookId, candidate\.id\)/);
  assert.match(page, /setPendingMerges\(\(items\) =>\s*\n\s*items\.filter\(\(item\) => item\.id !== candidate\.id && mergePairKey\(item\) !== decidedPairKey\)\s*\n\s*\);/);
  assert.match(page, /setPendingMerges\(\s*\n\s*pend\.filter\(\(item\) => item\.id !== candidate\.id && mergePairKey\(item\) !== decidedPairKey\)\s*\n\s*\);/);
  assert.match(page, /onClick=\{\(\) => decideMerge\(m, true\)\}/);
  assert.match(page, /onClick=\{\(\) => decideMerge\(m, false\)\}/);
});

test("KG rebuild confirmation modal renders above the full-screen graph overlay", () => {
  assert.match(page, /清空现有知识图谱并重新分析全部来源/);
  assert.match(page, /className="utility-modal"[\s\S]*infoModal/);
  assert.ok(zIndexFor(".utility-modal") > zIndexFor(".kg-view"));
});
