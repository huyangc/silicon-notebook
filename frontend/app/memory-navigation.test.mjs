import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { memoryHash, parseMemoryHash } from "./memory-model.ts";

const page = await readFile(new URL("./page.tsx", import.meta.url), "utf8");
const panel = await readFile(new URL("./memory-panel.tsx", import.meta.url), "utf8");

test("memory count deep-link targets the notebook memory tab", () => {
  assert.equal(memoryHash("nb-1"), "#notebook=nb-1&tab=memory");
  assert.deepEqual(parseMemoryHash("#notebook=nb-1&tab=memory"), {
    scope: "notebook",
    notebookId: "nb-1",
  });
});

test("global Memory has a stable outer-page location", () => {
  assert.equal(memoryHash(null), "#memory");
  assert.deepEqual(parseMemoryHash("#memory"), { scope: "global", notebookId: null });
});

test("an inaccessible notebook Memory deep-link falls back without invalidating login", () => {
  const start = page.indexOf("const target = parseMemoryHash(window.location.hash);");
  const end = page.indexOf("\n      })\n      .catch(() => { clearToken(); })", start);
  const deepLinkBlock = page.slice(start, end);
  assert.ok(start > -1 && end > start);
  assert.match(deepLinkBlock, /try \{[\s\S]*await openNotebookMemory\(target\.notebookId\);[\s\S]*\} catch \{[\s\S]*showCollection\(\);/);
});

test("workspace orchestration exposes global and notebook Memory surfaces", () => {
  assert.match(page, /import \{ MemoryPanel, MemorySaveDialog \} from "\.\/memory-panel";/);
  assert.match(page, /className="outer-nav"/);
  assert.match(page, />Notebooks</);
  assert.match(page, />Memory</);
  assert.match(page, /function openNotebookMemory\(notebookId: string\)/);
  assert.match(page, /<MemoryPanel scope="global" notebookId=\{null\}/);
  assert.match(page, /<MemoryPanel scope="notebook" notebookId=\{currentNotebookId\}/);
  assert.match(panel, /export function MemoryPanel\(/);
});

test("notebook deletion warns about lifecycle cleanup without exposing private counts", () => {
  assert.match(page, /所有成员各自绑定到此笔记本的私有 Memory/);
  assert.doesNotMatch(page, /成员.*\{.*counts\.memories/);
});
