import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const page = await readFile(new URL("./page.tsx", import.meta.url), "utf8");
const answerPanel = await readFile(new URL("./answer-panel.tsx", import.meta.url), "utf8");
const workspaceModel = await readFile(new URL("./workspace-model.ts", import.meta.url), "utf8");
const kgTypeMark = await readFile(new URL("./kg-type-mark.tsx", import.meta.url), "utf8");

test("workspace orchestrator imports answer UI instead of reimplementing it", () => {
  assert.match(page, /import \{ AnswerView, LatexText, ReasoningTracePanel \} from "\.\/answer-panel";/);
  assert.equal(page.includes("function AnswerView("), false);
  assert.equal(page.includes("function ReasoningTracePanel("), false);
  assert.match(answerPanel, /export function AnswerView\(/);
  assert.match(answerPanel, /export function ReasoningTracePanel\(/);
});

test("workspace API models have one shared module", () => {
  assert.match(page, /from "\.\/workspace-model";/);
  assert.equal(page.includes("type NotebookSummary ="), false);
  assert.equal(page.includes("type AskResponse ="), false);
  assert.match(workspaceModel, /export type NotebookSummary =/);
  assert.match(workspaceModel, /export type AskResponse =/);
});

test("KG type marks are shared by graph and answer panels", () => {
  assert.match(page, /import \{ KG_TYPE_STYLE, KgTypeMark, kgTypeLabel \} from "\.\/kg-type-mark";/);
  assert.match(answerPanel, /import \{ KgTypeMark, kgTypeLabel \} from "\.\/kg-type-mark";/);
  assert.match(kgTypeMark, /export function KgTypeMark\(/);
  assert.equal(page.includes("function KgTypeMark("), false);
});
