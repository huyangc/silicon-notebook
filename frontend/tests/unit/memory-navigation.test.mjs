import test from "node:test";
import assert from "node:assert/strict";

import {
  memoryHash,
  notebookHash,
  parseMemoryHash,
  parseWorkspaceHash,
} from "../../app/memory-model.ts";
import {
  NOTEBOOK_PRIVATE_MEMORY_DELETE_WARNING,
  openMemoryDeepLink,
} from "../../app/workspace-transitions.ts";
import { CHAT_MODES } from "../../app/workspace-model.ts";
import {
  callSitesIn,
  declarations,
  findFunctionIn,
  importsFrom,
  jsxElements,
  parseModule,
} from "../../test-support/semantic-source.mjs";


test("memory count deep-link targets the notebook memory tab", () => {
  assert.equal(memoryHash("nb-1"), "#notebook=nb-1&tab=memory");
  assert.deepEqual(parseMemoryHash("#notebook=nb-1&tab=memory"), {
    scope: "notebook",
    notebookId: "nb-1",
    filterNotebookId: null,
    status: null,
    itemId: null,
  });
});


test("workspace hash round-trips a bare notebook deep-link", () => {
  assert.equal(notebookHash("nb-1"), "#notebook=nb-1");
  assert.deepEqual(parseWorkspaceHash("#notebook=nb-1"), { notebookId: "nb-1", sourceId: "" });
});


test("workspace hash encodes ids that need escaping", () => {
  assert.equal(notebookHash("nb//1?x"), "#notebook=nb%2F%2F1%3Fx");
  assert.deepEqual(
    parseWorkspaceHash(notebookHash("nb//1?x")),
    { notebookId: "nb//1?x", sourceId: "" },
  );
});

test("工作区 hash 可携带只读来源定位", () => {
  assert.equal(notebookHash("nb-1", "src-2"), "#notebook=nb-1&source=src-2");
  assert.deepEqual(
    parseWorkspaceHash("#notebook=nb-1&source=src-2"),
    { notebookId: "nb-1", sourceId: "src-2" },
  );
});


test("workspace hash yields to memory links and ignores unrelated hashes", () => {
  assert.equal(parseWorkspaceHash("#notebook=nb-1&tab=memory"), null);
  assert.equal(parseWorkspaceHash("#memory"), null);
  assert.equal(parseWorkspaceHash(""), null);
  assert.equal(parseWorkspaceHash("#"), null);
  assert.equal(parseWorkspaceHash("#notebook="), null);
});


test("the two hash parsers stay mutually exclusive", () => {
  for (const { hash, isMemoryHash } of [
    { hash: "#notebook=nb-1", isMemoryHash: false },
    { hash: "#notebook=nb-1&tab=memory", isMemoryHash: true },
    { hash: "#memory", isMemoryHash: true },
    {
      hash: "#memory&notebook=nb%201&status=confirmed&item=mem%2F1",
      isMemoryHash: true,
    },
    { hash: "", isMemoryHash: false },
    { hash: "#zzz", isMemoryHash: false },
  ]) {
    assert.equal(
      parseMemoryHash(hash) !== null && parseWorkspaceHash(hash) !== null,
      false,
      `${hash} 同时被两个解析器认领`,
    );
    if (isMemoryHash) {
      assert.notEqual(parseMemoryHash(hash), null, `${hash} 未被 Memory 解析器认领`);
      assert.equal(parseWorkspaceHash(hash), null, `${hash} 被工作区解析器误认领`);
    }
  }
});


test("global Memory has a stable outer-page location", () => {
  assert.equal(memoryHash(null), "#memory");
  assert.deepEqual(
    parseMemoryHash("#memory"),
    {
      scope: "global",
      notebookId: null,
      filterNotebookId: null,
      status: null,
      itemId: null,
    },
  );
});


test("global Memory deep-links preserve optional filters and a detail target", () => {
  assert.equal(
    memoryHash(null, {
      notebookId: "nb 1",
      status: "confirmed",
      itemId: "mem/1",
    }),
    "#memory&notebook=nb%201&status=confirmed&item=mem%2F1",
  );
  assert.deepEqual(
    parseMemoryHash("#memory&notebook=nb%201&status=confirmed&item=mem%2F1"),
    {
      scope: "global",
      notebookId: null,
      filterNotebookId: "nb 1",
      status: "confirmed",
      itemId: "mem/1",
    },
  );
});


test("workspace tabs keep the product order", () => {
  assert.deepEqual(CHAT_MODES, [
    ["ask", "问答"],
    ["rules", "知识库"],
    ["memory", "记忆"],
    ["reports", "深度报告"],
  ]);
});


test("an inaccessible Memory deep-link falls back without rejecting login", async () => {
  let fellBack = false;
  const opened = await openMemoryDeepLink(
    "nb-missing",
    async () => { throw new Error("not found"); },
    () => { fellBack = true; },
  );

  assert.equal(opened, false);
  assert.equal(fellBack, true);
});


test("workspace composes the shared Memory surfaces", async () => {
  const page = await parseModule("page.tsx");
  const panel = await parseModule("memory-panel.tsx");
  const memoryImports = importsFrom(page, "./memory-panel").map(
    (item) => item.imported,
  );
  const memoryPanels = jsxElements(page, "MemoryPanel");

  assert.deepEqual(memoryImports, ["MemoryPanel", "MemorySaveDialog"]);
  assert.equal(memoryPanels.length, 2);
  assert.equal(
    declarations(panel).some(
      (finding) => (
        finding.kind === "function"
        && finding.name === "MemoryPanel"
        && finding.exported
      ),
    ),
    true,
  );
});


test("global Memory navigation owns and forwards explicit destination state", async () => {
  const page = await parseModule("page.tsx");
  const showGlobalMemory = findFunctionIn(page, "Home", "showGlobalMemory");
  const globalPanel = jsxElements(page, "MemoryPanel").find(
    ({ attributes }) => attributes.scope === "global",
  );

  assert.ok(
    callSitesIn(showGlobalMemory).some(
      ({ target, arguments: args }) => (
        target === "setMemoryNavigationTarget"
        && args[0] === "target"
      ),
    ),
  );
  assert.ok(
    callSitesIn(showGlobalMemory).some(
      ({ target, arguments: args }) => (
        target === "memoryHash"
        && args[0] === "null"
        && args[1] === "target"
      ),
    ),
  );
  assert.equal(
    globalPanel?.bindings?.initialNotebookId,
    "memoryNavigationTarget.notebookId",
  );
  assert.equal(
    globalPanel?.bindings?.initialStatus,
    "memoryNavigationTarget.status",
  );
  assert.equal(
    globalPanel?.bindings?.initialMemoryId,
    "memoryNavigationTarget.itemId",
  );
});


test("notebook deletion warning states private lifecycle cleanup without counts", () => {
  assert.match(NOTEBOOK_PRIVATE_MEMORY_DELETE_WARNING, /私有记忆/);
  assert.doesNotMatch(NOTEBOOK_PRIVATE_MEMORY_DELETE_WARNING, /\d/);
});
