import test from "node:test";
import assert from "node:assert/strict";

import {
  followLatestNotebookRequest,
  historyModeForTransition,
  notebookIsActive,
  openMemoryDeepLink,
  ownsWorkspaceRun,
  restoreLatestConversation,
  sessionListRequestIsCurrent,
  sourceDeleteAllowed,
  workspaceCapabilities,
} from "../../app/workspace-transitions.ts";


function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}


test("workspace run ownership requires both epochs and notebook identity", () => {
  assert.equal(ownsWorkspaceRun(2, 2, 4, 4, "nb-1", "nb-1"), true);
  assert.equal(ownsWorkspaceRun(1, 2, 4, 4, "nb-1", "nb-1"), false);
  assert.equal(ownsWorkspaceRun(2, 2, 3, 4, "nb-1", "nb-1"), false);
  assert.equal(ownsWorkspaceRun(2, 2, 4, 4, "nb-1", "nb-2"), false);
});


test("only the latest session-list request may publish into its workspace", () => {
  assert.equal(sessionListRequestIsCurrent(3, 3, "nb-1", "nb-1"), true);
  assert.equal(sessionListRequestIsCurrent(2, 3, "nb-1", "nb-1"), false);
  assert.equal(sessionListRequestIsCurrent(3, 3, "nb-1", "nb-2"), false);
});


test("session-list ownership follows the notebook, not the selected conversation epoch", () => {
  assert.equal(sessionListRequestIsCurrent(3, 3, "nb-1", "nb-1"), true);
  assert.equal(sessionListRequestIsCurrent(3, 3, "nb-1", "nb-2"), false);
});


test("Ask history may follow the active notebook after the visible run loses ownership", () => {
  assert.equal(notebookIsActive("nb-1", "nb-1"), true);
  assert.equal(notebookIsActive("nb-1", "nb-2"), false);
  assert.equal(notebookIsActive("nb-1", null), false);
});


test("an older session-list caller follows the newest same-notebook request", async () => {
  const first = deferred();
  const second = deferred();
  let latest = { notebookId: "nb-1", requestId: 1, promise: first.promise };
  const resultPromise = followLatestNotebookRequest(
    latest,
    () => latest,
    () => true,
  );
  latest = { notebookId: "nb-1", requestId: 2, promise: second.promise };
  first.resolve(["old"]);
  await Promise.resolve();
  second.resolve(["new"]);

  assert.deepEqual(await resultPromise, {
    requestId: 2,
    generationId: 2,
    value: ["new"],
  });
});


test("a failed superseding session-list request preserves a valid caller fallback", async () => {
  const first = deferred();
  const second = deferred();
  let latest = { notebookId: "nb-1", requestId: 1, promise: first.promise };
  const resultPromise = followLatestNotebookRequest(
    latest,
    () => latest,
    () => true,
  );
  latest = { notebookId: "nb-1", requestId: 2, promise: second.promise };
  first.resolve(["fallback"]);
  await Promise.resolve();
  second.reject(new Error("refresh failed"));

  assert.deepEqual(await resultPromise, {
    requestId: 1,
    generationId: 2,
    value: ["fallback"],
  });
});


test("a failed intermediate session-list request follows an already newer request", async () => {
  const first = deferred();
  const second = deferred();
  const third = deferred();
  let latest = { notebookId: "nb-1", requestId: 1, promise: first.promise };
  const resultPromise = followLatestNotebookRequest(
    latest,
    () => latest,
    () => true,
  );
  latest = { notebookId: "nb-1", requestId: 2, promise: second.promise };
  first.resolve(["old"]);
  await Promise.resolve();
  latest = { notebookId: "nb-1", requestId: 3, promise: third.promise };
  second.reject(new Error("intermediate refresh failed"));
  await Promise.resolve();
  third.resolve(["newest"]);

  assert.deepEqual(await resultPromise, {
    requestId: 3,
    generationId: 3,
    value: ["newest"],
  });
});


test("history transitions replace the current notebook and push a new one", () => {
  assert.equal(historyModeForTransition("nb-1", "nb-1"), "replace");
  assert.equal(historyModeForTransition("nb-1", "nb-2"), "push");
  assert.equal(historyModeForTransition(null, "nb-1"), "push");
});


test("latest conversation restore is empty-safe and failure-safe", async () => {
  assert.equal(await restoreLatestConversation([], async () => "unused"), null);
  assert.equal(
    await restoreLatestConversation(
      [{ id: "conv-2" }, { id: "conv-1" }],
      async (id) => `restored:${id}`,
    ),
    "restored:conv-2",
  );
  assert.equal(
    await restoreLatestConversation(
      [{ id: "conv-2" }],
      async () => { throw new Error("stale"); },
    ),
    null,
  );
});


test("Memory deep-link restore returns failure after applying fallback", async () => {
  let fallbackCalls = 0;
  assert.equal(
    await openMemoryDeepLink(
      "nb-1",
      async () => { throw new Error("gone"); },
      () => { fallbackCalls += 1; },
    ),
    false,
  );
  assert.equal(fallbackCalls, 1);
});


// ---------------------------------------------------------------------------
// 分享编辑与协作 P1-T12:五种形态 × 每个能力位(规格 §9 前端能力判据逐格表)。
//
// 每一格都写死,不从实现推导:编辑者与组管理员的差别(管理档)、镜像围栏、恒 owner 的那几格
// 一旦漂移,这张表当场红。参数形态与页面传参同序:access, role, can_manage_content,
// sync_origin, can_manage_notebook。
const FORMS = {
  owner: ["owner", "user", true, "", true],
  groupAdmin: ["reader", "user", true, "", true],
  editor: ["reader", "user", true, "", false],
  reader: ["reader", "user", false, "", false],
  mirrorOwner: ["owner", "user", true, "site-a", true],
};

const EXPECTED = {
  //                          owner  groupAdmin editor reader mirrorOwner
  mirrored:                  [false, false,     false, false, true],
  mirrorHidesNotebookManage: [false, false,     false, false, true],
  canWriteNotebook:          [true,  true,      true,  false, false],
  canGovernKnowledge:        [true,  true,      true,  false, false],
  canMergeOwnMemory:         [true,  true,      true,  true,  false],
  canRebuildIndexes:         [true,  true,      true,  false, true],
  canGrantAccess:            [true,  true,      false, false, true],
  canConfigureNotebook:      [true,  false,     false, false, true],
  canManageCollaborators:    [true,  false,     false, false, true],
  canMountBases:             [true,  false,     false, false, false],
  canManageNotebook:         [true,  true,      false, false, false],
  canDeleteAnySource:        [true,  true,      false, false, false],
  canDestroyKnowhowTables:   [true,  true,      false, false, false],
  canDeleteNotebook:         [true,  false,     false, false, false],
  canExportNotebook:         [true,  false,     false, false, true],
  canManageReports:          [true,  true,      true,  true,  true],
  canManageNotebookSchemas:  [true,  true,      true,  false, false],
  canManageGlobalSchemas:    [false, false,     false, false, false],
};

test("workspaceCapabilities:owner / 组管理员 / 编辑者 / 只读 / 镜像 × 每个能力位", () => {
  const names = Object.keys(FORMS);
  for (const [index, form] of names.entries()) {
    const capabilities = workspaceCapabilities(...FORMS[form]);
    // 能力位集合与表逐一相等:新加一位而不登记形态,这里先红。
    assert.deepEqual(
      Object.keys(capabilities).filter((key) => key !== "mirrorOrigin").sort(),
      Object.keys(EXPECTED).sort(),
      `${form}: 能力位集合与逐格表不一致`,
    );
    for (const [bit, row] of Object.entries(EXPECTED)) {
      assert.equal(capabilities[bit], row[index], `${form}.${bit}`);
    }
  }
});

test("编辑者与组管理员的差别只在管理档:改名/tier、群组授权、删除他人来源、整表删除/移动/清理历史", () => {
  const editor = workspaceCapabilities(...FORMS.editor);
  const admin = workspaceCapabilities(...FORMS.groupAdmin);
  const differing = Object.keys(editor).filter((key) => editor[key] !== admin[key]).sort();
  assert.deepEqual(differing, [
    "canDeleteAnySource", "canDestroyKnowhowTables", "canGrantAccess", "canManageNotebook",
  ]);
});

test("canManageNotebook 参数缺省 = 收:旧后端不发 can_manage_notebook 时不给管理档入口", () => {
  const legacy = workspaceCapabilities("reader", "user", true, "");
  assert.equal(legacy.canManageNotebook, false);
  assert.equal(legacy.canGrantAccess, false);
  assert.equal(legacy.canWriteNotebook, true, "内容写入口仍看编辑权");
});

test("sourceDeleteAllowed:编辑者只对本人上传的来源给删除(D14)", () => {
  const editor = workspaceCapabilities(...FORMS.editor);
  assert.equal(sourceDeleteAllowed(editor, { uploaded_by_me: true }), true);
  assert.equal(sourceDeleteAllowed(editor, { uploaded_by_me: false }), false);
  assert.equal(sourceDeleteAllowed(editor, {}), false, "旧后端缺字段按「不是本人」");
  for (const form of ["owner", "groupAdmin"]) {
    const capabilities = workspaceCapabilities(...FORMS[form]);
    assert.equal(sourceDeleteAllowed(capabilities, { uploaded_by_me: false }), true, form);
  }
  for (const form of ["reader", "mirrorOwner"]) {
    const capabilities = workspaceCapabilities(...FORMS[form]);
    assert.equal(sourceDeleteAllowed(capabilities, { uploaded_by_me: true }), false, form);
  }
});
