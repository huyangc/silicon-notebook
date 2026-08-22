import test from "node:test";
import assert from "node:assert/strict";

import {
  callSitesIn,
  findFunctionIn,
  ifConditionsIn,
  importsIn,
  parseModule,
} from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const hook = await parseModule("use-root-modal-coordinator.ts");
const modelPanel = await parseModule("model-service-panel.tsx");
const pageText = page.getText(page);
const hookText = hook.getText(hook);

test("page composes one typed root-modal coordinator and has no legacy modal booleans", () => {
  assert.equal((pageText.match(/useRootModalCoordinator\(/g) ?? []).length, 1);
  for (const legacy of [
    "passwordModalOpen", "searchProfileModalOpen", "sourceModalOpen",
    "modelPanelOpen", "sharedByMeOpen", "promoOpen", "edgeReviewOpen",
    "understandingOpen",
  ]) assert.equal(pageText.includes(legacy), false, legacy);
  for (const slot of ["notebook-editor", "notebook-delete", "kg-schema", "kg-analysis"]) {
    assert.match(pageText, new RegExp(`rootModals\\.view\\(\\"${slot}\\"\\)`), slot);
  }
  assert.doesNotMatch(pageText, /\{editingNotebook && \([\s\S]{0,120}aria-modal="true"/);
  assert.doesNotMatch(pageText, /\{deleteNotebook && \([\s\S]{0,120}aria-modal="true"/);
  assert.doesNotMatch(pageText, /\{schemaModalOpen && \([\s\S]{0,120}aria-modal="true"/);
});

test("coordinator is presentation-only: React is its sole dependency and it owns no I/O or timer", () => {
  assert.deepEqual([...new Set(importsIn(hook).map(({ module }) => module))], ["react"]);
  assert.doesNotMatch(hookText, /\b(fetch|setTimeout|setInterval|XMLHttpRequest|AbortController)\b/);
  assert.doesNotMatch(hookText, /\b(Api|Repository|Service|Busy|Payload|setCurrentNotebook)\b/);
});

test("workspace transitions invalidate root slots before old-domain work and close source intake through one sink", () => {
  const openCalls = callSitesIn(findFunctionIn(page, "Home", "openNotebook")).map(({ target }) => target);
  assert.ok(openCalls.includes("rootModals.beginWorkspaceTransition"));
  assert.ok(openCalls.includes("rootModals.finishWorkspaceTransition"));
  const collectionCalls = callSitesIn(findFunctionIn(page, "Home", "showCollection")).map(({ target }) => target);
  assert.ok(collectionCalls.includes("rootModals.leaveWorkspace"));
  const closeCalls = callSitesIn(findFunctionIn(page, "Home", "handleRootModalClosed")).map(({ target }) => target);
  assert.ok(closeCalls.includes("resetStagedIntake"));
  assert.ok(closeCalls.includes("setLinkSectionOpen"));
});

test("authenticated bootstrap activates modal authority before publishing the user", () => {
  assert.equal(
    (pageText.match(/rootModals\.activateActor\(u\.id\);[\s\S]{0,220}setCurrentUser\(u\)/g) ?? []).length,
    2,
  );
  const logoutCalls = callSitesIn(findFunctionIn(page, "Home", "handleLogout")).map(({ target }) => target);
  assert.ok(logoutCalls.includes("rootModals.leaveActor"));
  assert.ok(logoutCalls.includes("logoutUser"));
});

test("deferred root openers publish frozen tickets instead of opening from live state", () => {
  for (const name of ["openShareModal", "openSharedByMe", "openPromoQueue", "openEdgeReviewQueue"]) {
    const calls = callSitesIn(findFunctionIn(page, "Home", name)).map(({ target }) => target);
    assert.ok(calls.includes("rootModals.issue"), name);
    assert.ok(calls.includes("rootModals.publish"), name);
  }
  assert.match(pageText, /previewShared\(token\)[\s\S]*rootModals\.publish\(modalLease\)/);
  for (const name of ["presentNotebookEditor", "presentNotebookDelete"]) {
    const calls = callSitesIn(findFunctionIn(page, "Home", name)).map(({ target }) => target);
    assert.ok(calls.includes("rootModals.issue"), name);
    assert.ok(calls.includes("rootModals.publish"), name);
  }
});

test("collection and KG payload owners are coordinated only through typed presentation adapters", () => {
  const expected = new Map([
    ["openKgSchemas", ["rootModals.open", "kgWorkspace.openSchemas"]],
    ["openKgAnalysis", ["rootModals.open", "kgWorkspace.openAnalysis"]],
    ["closeKgSchemas", ["rootModals.requestClose"]],
    ["closeKgAnalysis", ["rootModals.requestClose"]],
  ]);
  for (const [name, targets] of expected) {
    const calls = callSitesIn(findFunctionIn(page, "Home", name)).map(({ target }) => target);
    for (const target of targets) assert.ok(calls.includes(target), `${name}: ${target}`);
  }
  const closeCalls = callSitesIn(findFunctionIn(page, "Home", "handleRootModalClosed"))
    .map(({ target }) => target);
  for (const target of [
    "notebookCollection.closeEditor",
    "notebookCollection.closeDelete",
    "kgWorkspace.closeSchemas",
    "kgWorkspace.closeAnalysis",
  ]) assert.ok(closeCalls.includes(target), target);
});

test("only the coordinator top layer drives the model focus trap", () => {
  assert.match(pageText, /interactive=\{modelPanel\.topmost\}/);
  assert.match(modelPanel.getText(modelPanel), /if \(!interactive\) return;/);
  assert.match(modelPanel.getText(modelPanel), /onClose\("escape"\)/);
});

test("presentation close never releases an in-flight domain operation", () => {
  const close = findFunctionIn(page, "Home", "handleRootModalClosed");
  const text = close.getText(page);
  assert.doesNotMatch(text, /OperationRef\.current\s*=\s*null/);
  assert.doesNotMatch(text, /set(?:Share|Promo|Edge)Busy\(false\)/);
  for (const [name, operation] of [
    ["openShareModal", "shareOperationRef.current"],
    ["openSharedByMe", "shareOperationRef.current"],
    ["openPromoQueue", "promoOperationRef.current"],
    ["openEdgeReviewQueue", "edgeOperationRef.current"],
  ]) {
    assert.ok(
      ifConditionsIn(findFunctionIn(page, "Home", name))
        .some((condition) => condition.includes(operation)),
      `${name} must not replace an in-flight domain operation`,
    );
  }
});

test("modal mutations suppress errors after their frozen lease becomes stale", () => {
  for (const name of [
    "enableShareLink",
    "handleUnshare",
    "handleUnshareFromOverview",
    "decidePromotion",
    "decideEdge",
  ]) {
    const text = findFunctionIn(page, "Home", name).getText(page);
    assert.match(text, /catch \(error\)[\s\S]*rootModals\.owns\(modalLease\)[\s\S]*throw error/, name);
  }
  const promotion = findFunctionIn(page, "Home", "submitPromotion").getText(page);
  assert.match(promotion, /const actorId = currentUser\?\.id/);
  assert.match(promotion, /const workspaceEpoch = workspaceEpochRef\.current/);
  assert.match(promotion, /if \(isCurrent\(\)\) setToast/);
  assert.match(promotion, /catch \(error\)[\s\S]*if \(isCurrent\(\)\) throw error/);
});
