import test from "node:test";
import assert from "node:assert/strict";

import { callsIn, findFunctionIn, importsIn, parseModule } from "../../test-support/semantic-source.mjs";


test("page composes one source-library owner and does not retain source CRUD/detail state", async () => {
  const page = await parseModule("page.tsx");
  const text = page.getText(page);

  assert.equal((text.match(/useSourceLibrary\(/g) ?? []).length, 1);
  for (const legacyOwner of [
    "const [sources, setSources]",
    "const [sourceDetail, setSourceDetail]",
    "const [deletingSourceIds, setDeletingSourceIds]",
    "const pollCountRef",
    "async function openSourceById",
    "async function deleteSource(source",
  ]) {
    assert.equal(text.includes(legacyOwner), false, `page still owns ${legacyOwner}`);
  }

  const sourceImports = importsIn(page)
    .filter((item) => item.module === "./source-api")
    .map((item) => item.imported);
  for (const hookOwned of [
    "getSource",
    "getNotebookSource",
    "getNotebookSourceElementsPage",
    "parseSource",
    "deleteSource",
  ]) {
    assert.equal(sourceImports.includes(hookOwned), false, `page imports hook-owned ${hookOwned}`);
  }
  // sourceLibrary.activateActor 现在只经共享函数 activateWorkspaceOwners 间接
  // 调用；守卫 workspace-owner-transition-guard 钉住「只能从那里发出」。原断言钉
  // 「紧邻 setCurrentUser(u)」的顺序邻接，现拆成两半：共享函数体内确有调用，加上
  // 两个认证站点各自 activateWorkspaceOwners(u.id) 紧邻 setCurrentUser(u)。
  assert.ok(
    callsIn(findFunctionIn(page, "Home", "activateWorkspaceOwners")).includes("sourceLibrary.activateActor"),
    "activateWorkspaceOwners must activate the source-library owner",
  );
  assert.match(
    text,
    /activateWorkspaceOwners\(u\.id\);\s*setCurrentUser\(u\)/,
    "authenticated restoration must activate workspace owners before opening a hash target",
  );
  assert.match(text, /openNotebook\(workspace\.notebookId, "none", u\.id\)/);
  assert.match(text, /openNotebookMemory\(notebookId, u\.id\)/);
});


// PR-5 分片 2:来源搜索框 / 来源行 / 分页整体住进 source-list-panel.tsx。防回填——
// 「顺手在 page 里再写一行来源行」会让呈现判据(ui-mode-wiring 的 .source-row 模板、
// source-agent-badge-guard 的徽标门控、组件测试)全部只盯着组件那一份,page 里的副本
// 无人看管。
test("page renders the source list through SourceListPanel, not inline markup", async () => {
  const page = await parseModule("page.tsx");
  const text = page.getText(page);

  assert.equal((text.match(/<SourceListPanel\b/g) ?? []).length, 1);
  for (const movedMarkup of [
    'className="source-list"',
    'className="source-search"',
    "source-row compact-source-row",
    'className="source-delete-button"',
  ]) {
    assert.equal(text.includes(movedMarkup), false, `page still renders ${movedMarkup} inline`);
  }
});


test("source-library hook is narrow and does not depend on other workspace domains", async () => {
  const hook = await parseModule("use-source-library.ts");
  const modules = importsIn(hook).map((item) => item.module);
  const allowed = new Set([
    "react",
    "./errors.ts",
    "./source-api.ts",
    "./source-delete-state.ts",
    "./source-detail-state.ts",
    "./source-page-state.ts",
    "./source-scope.ts",
    "./workspace-model.ts",
    // 两个纯来源判据模块（PR-E8：收录来源不算处理中、不占文档名额）。它们只许
    // 互相引用，下面钉住，这样放行它们不会把别的工作区领域间接带进 hook。
    "./source-management.ts",
    "./document-limit.ts",
  ]);
  assert.deepEqual(modules.filter((module) => !allowed.has(module)), []);
  for (const [leaf, reachable] of [
    ["source-management.ts", []],
    ["document-limit.ts", ["./source-management.ts"]],
  ]) {
    const leafModules = importsIn(await parseModule(leaf)).map((item) => item.module);
    assert.deepEqual(
      leafModules.filter((module) => !reachable.includes(module)),
      [],
      `${leaf} 必须保持纯判据模块`,
    );
  }
  assert.doesNotMatch(hook.getText(hook), /\b(setCurrentNotebook|setKnowledge|setCheckup)\b/);
  assert.match(
    hook.getText(hook),
    /source\.notebook_id !== ownerAtStart\.notebookId/,
    "delete must revalidate source ownership inside the hook",
  );
});
