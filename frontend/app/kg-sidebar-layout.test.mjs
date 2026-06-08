import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const page = await readFile(new URL("./page.tsx", import.meta.url), "utf8");
const css = await readFile(new URL("./globals.css", import.meta.url), "utf8");

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
