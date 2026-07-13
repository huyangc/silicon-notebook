import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const answerPanel = await readFile(new URL("./answer-panel.tsx", import.meta.url), "utf8");
const page = await readFile(new URL("./page.tsx", import.meta.url), "utf8");
const memoryPanel = await readFile(new URL("./memory-panel.tsx", import.meta.url), "utf8");

test("AnswerView exposes a manual save-to-Memory action", () => {
  assert.match(answerPanel, /onSaveMemory,/);
  assert.match(answerPanel, /memorySaved,/);
  assert.match(answerPanel, /onSaveMemory: \(answerId: string\) => void/);
  assert.match(answerPanel, /memorySaved: boolean/);
  assert.match(answerPanel, /onClick=\{\(\) => onSaveMemory\(answer\.answer_id\)\}/);
  assert.match(answerPanel, /保存到 Memory/);
});

test("answer capture uses preview then explicit edited confirmation", () => {
  assert.match(memoryPanel, /export function MemorySaveDialog\(/);
  assert.match(memoryPanel, /`\/answers\/\$\{encodeURIComponent\(answerId\)\}\/memory-preview`/);
  assert.match(memoryPanel, /`\/notebooks\/\$\{encodeURIComponent\(notebookId\)\}\/memories\/from-answer`/);
  assert.match(memoryPanel, /value=\{draft\.title\}/);
  assert.match(memoryPanel, /value=\{draft\.content_md\}/);
  assert.match(memoryPanel, /确认保存/);
});

test("workspace tracks saved answers and closes stale save dialogs on navigation", () => {
  assert.match(page, /const \[memorySavedAnswers, setMemorySavedAnswers\] = useState<Record<string, boolean>>\(\{\}\);/);
  assert.match(page, /memorySaved=\{Boolean\(memorySavedAnswers\[turn\.response\.answer_id\]\)\}/);
  assert.match(page, /onSaveMemory=\{\(answerId\) => setMemoryAnswerId\(answerId\)\}/);
  assert.match(page, /<MemorySaveDialog/);
  assert.ok((page.match(/setMemoryAnswerId\(null\);/g) ?? []).length >= 2);
});
