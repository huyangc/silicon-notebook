import test from "node:test";
import assert from "node:assert/strict";

import {
  callsIn,
  findFunction,
  findFunctionIn,
  importsIn,
  parseModule,
} from "../../test-support/semantic-source.mjs";

const [page, hook, ask] = await Promise.all([
  parseModule("page.tsx"),
  parseModule("use-notebook-collection.ts"),
  parseModule("ask-api.ts"),
]);
const pageText = page.getFullText();
const hookText = hook.getFullText();

test("page composes one collection owner and no longer owns its state cluster", () => {
  const home = findFunction(page, "Home");
  assert.ok(home);
  assert.equal(callsIn(home).filter((call) => call === "useNotebookCollection").length, 1);
  for (const legacy of [
    "setNotebooks",
    "setSearchHits",
    "notebookListSeqRef",
    "notebookListPublishedRef",
    "openNotebookEditor",
    "openDeleteConfirm",
    "confirmDeleteNotebook",
  ]) {
    assert.ok(!pageText.includes(legacy), `page 仍持有 collection owner: ${legacy}`);
  }
});

test("collection hook has a positive dependency allowlist and no cross-domain owner", () => {
  const allowed = new Set([
    "react",
    "./collection-search.ts",
    "./indexing-pipeline-settings.ts",
    "./notebook-api.ts",
    "./notebook-bases.ts",
    "./notebook-creation.ts",
    "./workspace-model.ts",
  ]);
  const actual = [...new Set(importsIn(hook).map((item) => item.module))].sort();
  assert.deepEqual(actual, [...actual].filter((item) => allowed.has(item)), actual);
  for (const forbidden of [
    "source-api",
    "ask-api",
    "report-api",
    "knowledge-api",
    "notebook-share",
    "page",
    "zustand",
    "redux",
  ]) {
    assert.ok(!hookText.includes(forbidden), `collection hook 越界依赖 ${forbidden}`);
  }
});

test("hook exports readonly views and named commands, never raw setters", () => {
  const returned = hookText.slice(hookText.lastIndexOf("  return {"));
  assert.ok(returned.includes("beginListRead"));
  assert.ok(returned.includes("commitListSnapshot"));
  assert.ok(returned.includes("refreshAfterAccessChange"));
  assert.ok(!/\bset[A-Z][A-Za-z0-9_]*\s*,/.test(returned), "public surface 暴露 raw setter");
});

test("shell preserves the composite list bundle through the hook watermark", () => {
  const load = findFunction(page, "loadNotebookCollection");
  assert.ok(load);
  const calls = callsIn(load);
  assert.ok(calls.includes("notebookCollection.beginListRead"));
  assert.ok(calls.includes("listNotebooks"));
  assert.ok(calls.includes("notebookCollection.commitListSnapshot"));
  assert.ok(calls.includes("fetchHealth"));
  assert.ok(calls.includes("fetchSystemConfiguration"));
});

test("collection search is not reverse-owned by Ask or page", () => {
  assert.ok(!importsIn(hook).some((item) => item.module === "./ask-api"));
  assert.ok(!importsIn(page).some((item) => item.imported === "searchNotebooksBounded"));
  assert.ok(
    ask.getFullText().includes('from "./collection-search.ts"')
      && ask.getFullText().includes("searchNotebooksBounded"),
    "Ask compatibility surface should re-export the canonical collection adapter",
  );
});

test("authentication binds collection authority before publishing currentUser", () => {
  // notebookCollection.activateActor 现在只经共享函数 activateWorkspaceOwners
  // 间接调用；守卫 workspace-owner-transition-guard 钉住「只能从那里发出」。
  assert.ok(
    callsIn(findFunctionIn(page, "Home", "activateWorkspaceOwners")).includes("notebookCollection.activateActor"),
    "activateWorkspaceOwners must activate the collection owner",
  );
  const bootstrap = pageText.indexOf("activateWorkspaceOwners(u.id)");
  const publish = pageText.indexOf("setCurrentUser(u)", bootstrap);
  assert.ok(bootstrap >= 0 && publish > bootstrap);
  const authGate = pageText.lastIndexOf("activateWorkspaceOwners(u.id)");
  const authPublish = pageText.indexOf("setCurrentUser(u)", authGate);
  assert.ok(authGate > bootstrap && authPublish > authGate);
});
