import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { memoryHash, notebookHash, parseMemoryHash, parseWorkspaceHash } from "./memory-model.ts";
import { CHAT_MODES } from "./workspace-model.ts";

const page = await readFile(new URL("./page.tsx", import.meta.url), "utf8");
const panel = await readFile(new URL("./memory-panel.tsx", import.meta.url), "utf8");

test("memory count deep-link targets the notebook memory tab", () => {
  assert.equal(memoryHash("nb-1"), "#notebook=nb-1&tab=memory");
  assert.deepEqual(parseMemoryHash("#notebook=nb-1&tab=memory"), {
    scope: "notebook",
    notebookId: "nb-1",
  });
});

test("workspace hash round-trips a bare notebook deep-link", () => {
  assert.equal(notebookHash("nb-1"), "#notebook=nb-1");
  assert.deepEqual(parseWorkspaceHash("#notebook=nb-1"), { notebookId: "nb-1" });
});

test("workspace hash encodes ids that need escaping", () => {
  assert.equal(notebookHash("nb//1?x"), "#notebook=nb%2F%2F1%3Fx");
  assert.deepEqual(parseWorkspaceHash(notebookHash("nb//1?x")), { notebookId: "nb//1?x" });
});

test("workspace hash yields to the memory tab and ignores unrelated hashes", () => {
  // 带 tab=memory 的归 parseMemoryHash 管,workspace 解析器必须让路,
  // 否则挂载时两个分支会抢同一条 hash。
  assert.equal(parseWorkspaceHash("#notebook=nb-1&tab=memory"), null);
  assert.equal(parseWorkspaceHash("#memory"), null);
  assert.equal(parseWorkspaceHash(""), null);
  assert.equal(parseWorkspaceHash("#"), null);
  assert.equal(parseWorkspaceHash("#notebook="), null);
});

test("the two hash parsers stay mutually exclusive", () => {
  for (const hash of ["#notebook=nb-1", "#notebook=nb-1&tab=memory", "#memory", "", "#zzz"]) {
    const both = parseMemoryHash(hash) !== null && parseWorkspaceHash(hash) !== null;
    assert.equal(both, false, `${hash} 同时被两个解析器认领`);
  }
});

test("global Memory has a stable outer-page location", () => {
  assert.equal(memoryHash(null), "#memory");
  assert.deepEqual(parseMemoryHash("#memory"), { scope: "global", notebookId: null });
});

test("workspace tabs keep 问答, 知识库, 记忆, 深度报告 in exact order", () => {
  assert.deepEqual(CHAT_MODES, [
    ["ask", "问答"],
    ["rules", "知识库"],
    ["memory", "记忆"],
    ["reports", "深度报告"],
  ]);
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
  assert.doesNotMatch(page, /className="outer-nav"/);
  assert.match(page, /<span>私有记忆<\/span>/);
  assert.match(page, /setAccountMenuOpen\(false\); showGlobalMemory\(\);/);
  assert.match(page, /function openNotebookMemory\(notebookId: string\)/);
  assert.match(page, /<MemoryPanel scope="global" notebookId=\{null\}/);
  assert.match(page, /<MemoryPanel scope="notebook" notebookId=\{currentNotebookId\}/);
  assert.match(panel, /export function MemoryPanel\(/);
});

test("notebook deletion warns about lifecycle cleanup without exposing private counts", () => {
  assert.match(page, /所有成员各自绑定到此笔记本的私有记忆/);
  assert.doesNotMatch(page, /成员.*\{.*counts\.memories/);
});

test("global Agent access keeps profile and token pagination independent", () => {
  assert.match(panel, /profilePageControllerRef/);
  assert.match(panel, /tokenPageControllerRef/);
  assert.match(panel, /loadMoreProfiles/);
  assert.match(panel, /loadMoreTokens/);
  assert.match(panel, /加载更多 Profile/);
  assert.match(panel, /加载更多 Token/);
  assert.match(panel, /agentPagePath\("\/agent-profiles", profileOffsetRef\.current\)/);
  assert.match(panel, /agentPagePath\("\/agent-tokens", tokenOffsetRef\.current\)/);
  assert.match(panel, /profileOffsetRef\.current = 0;\s+tokenOffsetRef\.current = 0;/);
  assert.match(panel, /profilePageControllerRef\.current\?\.abort\(\)/);
  assert.match(panel, /tokenPageControllerRef\.current\?\.abort\(\)/);
  assert.equal((panel.match(/setRefresh\(\(value\) => value \+ 1\)/g) || []).length >= 4, true);
});
