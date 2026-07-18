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
  assert.match(answerPanel, /保存到记忆/);
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

test("list requests and Memory mutations have independent abort lifecycles", () => {
  assert.match(memoryPanel, /const listRequestEpochRef = useRef\(0\);/);
  assert.match(memoryPanel, /const mutationControllersRef = useRef\(new Set<AbortController>\(\)\);/);
  assert.match(memoryPanel, /mutationControllersRef\.current\.forEach\(\(controller\) => controller\.abort\(\)\);/);
  assert.match(memoryPanel, /async function updateMemory[\s\S]*const controller = new AbortController\(\);[\s\S]*signal: controller\.signal/);
  const mutationStart = memoryPanel.indexOf("async function updateMemory");
  const mutationEnd = memoryPanel.indexOf("\n  function submitSearch", mutationStart);
  const mutationBody = memoryPanel.slice(mutationStart, mutationEnd);
  assert.match(mutationBody, /finally \{[\s\S]*setBusyId\(null\);/);
  assert.equal(mutationBody.includes("listRequestEpochRef.current ==="), false);
});

test("answer save is abortable and its dialog cannot close while saving", () => {
  const saveStart = memoryPanel.indexOf("async function save()");
  const saveEnd = memoryPanel.indexOf("\n  return (", saveStart);
  const saveBody = memoryPanel.slice(saveStart, saveEnd);
  assert.match(saveBody, /const controller = new AbortController\(\);/);
  assert.match(saveBody, /signal: controller\.signal/);
  assert.match(memoryPanel, /saveControllerRef\.current\?\.abort\(\);/);
  assert.match(memoryPanel, /className="memory-close"[\s\S]*disabled=\{saving\}/);
  assert.match(memoryPanel, /<button type="button" disabled=\{saving\} onClick=\{onClose\}>取消<\/button>/);
});

test("workspace hydrates saved answers with bounded notebook batches and epoch guards", () => {
  assert.match(page, /answerIdBatches\(turns\.map\(\(turn\) => turn\.response\.answer_id\)\)/);
  assert.match(page, /`\/notebooks\/\$\{notebookId\}\/answer-memory-links`/);
  assert.match(page, /body: JSON\.stringify\(\{ answer_ids: batch \}\)/);
  assert.match(page, /memoryLinksAbortRef\.current\?\.abort\(\);/);
  assert.match(page, /workspaceEpochRef\.current !== workspaceEpoch/);
  assert.match(page, /collectSavedAnswerFlags\(/);
});

test("logout synchronously broadcasts Memory teardown before awaiting session logout", () => {
  assert.match(memoryPanel, /sessionSignal: AbortSignal/);
  assert.match(memoryPanel, /subscribeMemorySessionAbort\(sessionSignal/);
  assert.match(memoryPanel, /listControllerRef\.current\?\.abort\(\);/);
  const mutationAbort = /mutationControllersRef\.current\.forEach\(\(controller\) => controller\.abort\(\)\);/;
  assert.match(memoryPanel, mutationAbort);
  assert.equal((page.match(/sessionSignal=\{memorySessionAbortRef\.current\.signal\}/g) ?? []).length, 3);

  const start = page.indexOf("async function handleLogout() {");
  const end = page.indexOf("\n  }", start);
  const body = page.slice(start, end);
  assert.ok(body.indexOf("memorySessionAbortRef.current.abort()") > -1);
  assert.ok(body.indexOf("memorySessionAbortRef.current.abort()") < body.indexOf("await logoutUser()"));
});

test("saving an answer invalidates hydration before merging the saved flag", () => {
  const start = page.indexOf("async function handleMemorySaved(memory: MemoryRecord) {");
  const end = page.indexOf("\n  }", start);
  const body = page.slice(start, end);
  assert.ok(body.indexOf("memoryLinksAbortRef.current?.abort()") > -1);
  assert.ok(body.indexOf("memoryLinksAbortRef.current?.abort()") < body.indexOf("setMemorySavedAnswers"));
  assert.ok(body.includes("memoryLinksAbortRef.current = null"));
});

test("memory list supports single and bulk hard delete behind a confirm dialog", () => {
  assert.match(memoryPanel, /async function deleteMemory\(/);
  assert.match(memoryPanel, /method: "DELETE"/);
  assert.match(memoryPanel, /"\/memories\/bulk-delete"/);
  assert.match(memoryPanel, /memory_ids: ids/);
  assert.match(memoryPanel, /function confirmDelete\(\)/);
  assert.match(memoryPanel, /删除后不可恢复/);
  assert.match(memoryPanel, /const \[selectionMode/);
  assert.match(memoryPanel, /className="memory-select-box"/);
  assert.match(memoryPanel, /className="memory-delete-action"/);
});
