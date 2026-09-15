// Static wiring contract for the durable「删除知识图谱」owner. It mirrors
// kg-relink-wiring-guard: the delete is the third kind in the same server-side
// per-notebook maintenance slot, so every shape that keeps relink honest has to
// hold for it too — plus the stricter settlement a destructive action needs.
// Runtime claim / poll / scoped-result behaviour is exercised by
// use-kg-workspace.component; the view by kg-graph-view.component; the page-side
// dependents refresh by tests/unit/kg-delete-dependents.
import test from "node:test";
import assert from "node:assert/strict";

import { findFunction, findFunctionIn, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";

const hook = await parseModule("use-kg-graph.ts");
const page = await parseModule("page.tsx");
const kgGraphView = await parseModule("kg-graph-view.tsx");
const source = hook.getFullText();

function body(name) {
  return findFunction(hook, name).getText(hook);
}

function deletePollBody() {
  const start = source.indexOf("if (!deletingNotebookIds.has(key)) return;");
  assert.ok(start > 0, "missing delete polling effect");
  const end = source.indexOf("}, [deletingNotebookIds, ownerVersion]);", start);
  assert.ok(end > start, "missing delete polling dependencies");
  return source.slice(start, end);
}

function onlyElement(module, tag) {
  const elements = jsxElements(module, tag);
  assert.equal(elements.length, 1, `expected exactly one <${tag}>`);
  return elements[0];
}

test("delete claims the actor+notebook slot before POST and never reads counts from the POST", () => {
  const start = body("startKgDelete");
  const claimAt = start.indexOf("setDeletingNotebookIds((current) => claimNotebookSlot(current, key))");
  const postAt = start.indexOf("deleteKg(");
  assert.ok(claimAt >= 0 && postAt > claimAt);
  for (const forbidden of ["objects_deleted", "relations_deleted"]) {
    assert.equal(start.includes(forbidden), false);
  }
});

test("delete refuses a confirm aimed at a notebook that is no longer the live owner", () => {
  const start = body("startKgDelete");
  assert.match(start, /^startKgDelete = async \(notebookId: string\) =>/);
  assert.match(start, /owner\.notebookId !== notebookId\) return/);
  assert.match(start, /policyRef\.current\.canWriteKg/);
});

test("delete is refused while any maintenance, build, review or merge decision is busy, and it blocks theirs", () => {
  const start = body("startKgDelete");
  for (const bit of [
    "deletingNotebookIds.has(key)",
    "relinkingNotebookIds.has(key)",
    "rebuildingNotebookIds.has(key)",
    "buildingKg",
    "reviewBusy",
    "reviewAllStarting",
    "reviewAllRunning",
    "decidingMerge !== null",
  ]) assert.ok(start.includes(bit), `startKgDelete early return lacks ${bit}`);
  // The other entries must see the delete bit ("any busy bit ⇒ busy").
  assert.match(body("startRelink"), /deletingNotebookIds\.has\(key\)/);
  assert.match(body("launchRebuild"), /deletingNotebookIds\.has\(key\)/);
  for (const name of ["startKgBuild", "decideMerge", "reviewPendingMerges", "reviewAllMerges"]) {
    assert.match(body(name), /deletingNotebookIds\.has\(ownerKey\(owner\)\)/, `${name} lacks the delete bit`);
  }
});

test("delete submission has a kind-specific marker, exact job expectation, and finally cleanup", () => {
  const start = body("startKgDelete");
  assert.match(start, /maintenanceJobKey\(owner, "delete"\)/);
  const addAt = start.indexOf("submittingMaintenanceRef.current.add(jobKey)");
  const postAt = start.indexOf("deleteKg(");
  const expectAt = start.indexOf("expectedMaintenanceJobRef.current.set(jobKey, started.job_id)");
  const deleteAt = start.indexOf("submittingMaintenanceRef.current.delete(jobKey)");
  assert.ok(addAt >= 0 && addAt < postAt && postAt < expectAt && expectAt < deleteAt);
});

test("every maintenance start re-claims its own slot inside the retry loop, before the POST", () => {
  // An all-idle 409 adoption releases the caller's own slot; without this the retried
  // POST can succeed with no busy bit and no poll.
  for (const [name, setter, post] of [
    ["startKgDelete", "setDeletingNotebookIds", "deleteKg("],
    ["startRelink", "setRelinkingNotebookIds", "relinkKg("],
    ["launchRebuild", "setRebuildingNotebookIds", "rebuildUnifiedKg("],
  ]) {
    const start = body(name);
    const loopAt = start.indexOf("for (const attempt of [0, 1])");
    const reclaimAt = start.indexOf(`if (attempt > 0) ${setter}((current) => claimNotebookSlot(current, key))`);
    const postAt = start.indexOf(post);
    assert.ok(loopAt >= 0 && reclaimAt > loopAt && postAt > reclaimAt, `${name} must re-claim before its retry POST`);
  }
});

test("delete uses one bounded 409 retry and every adoption probes all three kinds, recording an observed delete", () => {
  const start = body("startKgDelete");
  assert.match(start, /httpErrorStatus\(error\) === 409/);
  assert.match(start, /await adoptRunningMaintenance\(owner\)/);
  assert.match(start, /if \(verdict === "idle" && attempt === 0\) continue/);
  // A refused start is explained beside the button, not only in the banner.
  assert.match(start, /showDeleteResult\(key,/);
  const adopt = body("adoptRunningMaintenance");
  assert.match(adopt, /Promise\.allSettled\(\[/);
  assert.match(adopt, /fetchKgDeleteStatus\(owner\.notebookId\)/);
  assert.match(adopt, /setDeletingNotebookIds/);
  assert.match(adopt, /setRebuildingNotebookIds/);
  assert.match(adopt, /setRelinkingNotebookIds/);
  assert.match(
    adopt,
    /if \(deleteRunning\) \{\s*expectedMaintenanceJobRef\.current\.set\(\s*maintenanceJobKey\(owner, "delete"\), deletionResult\.value\.job_id,?\s*\)/,
  );
});

test("delete polling is bounded, single-flight, identity-aware, and range-stable", () => {
  const poll = deletePollBody();
  assert.match(poll, /let inFlight = false/);
  assert.match(poll, /if \(stopped \|\| settled \|\| inFlight\) return/);
  assert.match(poll, /KG_DELETE_POLL_MAX_ATTEMPTS/);
  assert.match(poll, /KG_DELETE_POLL_TIMED_OUT/);
  assert.match(poll, /fetchKgDeleteStatus\(owner\.notebookId\)/);
  assert.match(poll, /kgDeletePollOutcome\(status\)/);
  assert.match(poll, /!ownsIdentity\(owner\)/);
  assert.equal(poll.includes("rangeLimit"), false);
});

test("a terminal status is reported only for the expected job; unobserved terminals settle silently", () => {
  const poll = deletePollBody();
  assert.match(poll, /if \(submittingMaintenanceRef\.current\.has\(jobKey\)\) return/);
  assert.match(poll, /kgDeleteTerminalSettlement\(\s*status, expectedMaintenanceJobRef\.current\.get\(jobKey\),?\s*\)/);
  assert.match(poll, /if \(settlement === "mismatch"\) \{\s*mismatchStreak \+= 1;/);
  assert.match(poll, /mismatchStreak < MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK/);
  assert.match(
    poll,
    /await settle\(settlement === "report"\s*\? outcome\s*: settlement === "mismatch" \? KG_DELETE_JOB_MISMATCH : KG_DELETE_UNOBSERVED\)/,
  );
  // Timeout with nothing ever expected is silent too.
  assert.match(
    poll,
    /await settle\(expectedMaintenanceJobRef\.current\.get\(jobKey\)\s*\? KG_DELETE_POLL_TIMED_OUT\s*: KG_DELETE_UNOBSERVED\)/,
  );
  // A running status observed while nothing is expected becomes the expected job.
  assert.match(poll, /expectedMaintenanceJobRef\.current\.set\(jobKey, status\.job_id\)/);
});

test("the terminal branch stops duplicate ticks before settling; settle refreshes, then reports, then releases", () => {
  const poll = deletePollBody();
  // Slice from the status read so the timeout branch above cannot satisfy the order.
  const terminal = poll.slice(poll.indexOf("const status = await fetchKgDeleteStatus"));
  const settledAt = terminal.indexOf("settled = true");
  const clearAt = terminal.indexOf("window.clearInterval(timer)", settledAt);
  const settleAt = terminal.indexOf("await settle(", clearAt);
  assert.ok(settledAt >= 0 && clearAt > settledAt && settleAt > clearAt);
  const settle = poll.slice(poll.indexOf("const settle = async"), poll.indexOf("const timer ="));
  const refreshAt = settle.indexOf("await refreshAfterDelete(owner)");
  const notifyAt = settle.indexOf("effectsRef.current.notify(outcome.result.text)");
  const resultAt = settle.indexOf("showDeleteResult(key, outcome.result)");
  const releaseAt = settle.indexOf("releaseNotebookClaim(current, key)");
  assert.ok(
    refreshAt >= 0 && notifyAt > refreshAt && resultAt > refreshAt && releaseAt > resultAt,
    "the result and its toast must land after the refresh, and the slot is released last",
  );
  assert.match(settle, /expectedMaintenanceJobRef\.current\.delete/);
});

test("delete refresh clears search and selection through their request sequences, then reloads the graph", () => {
  const refresh = body("refreshAfterDelete");
  for (const clear of [
    "clearSearchTimer()",
    "graphSearchRequestRef.current += 1",
    "graphNodeRequestRef.current += 1",
    'setSearch("")',
    "setSearchHits([])",
    "setSearchBusy(false)",
    "setSelectedNodeId(null)",
    "setConceptDetailFirstPage(null, null)",
    "setNodeContext(null)",
  ]) assert.ok(refresh.includes(clear), `refreshAfterDelete lacks ${clear}`);
  assert.equal(refresh.includes("refreshAfterRebuild"), false, "rebuild's refresh re-fetches the deleted selection");
  assert.match(refresh, /fetchUnifiedGraph\(owner\.notebookId, rangeLimitRef\.current\)/);
  assert.match(refresh, /fetchPendingMerges\(owner\.notebookId\)/);
  assert.match(refresh, /effectsRef\.current\.refreshNotebook\(owner\.notebookId, guard\)/);
  assert.match(refresh, /effectsRef\.current\.refreshAfterKgDelete\(owner\.notebookId, guard\)/);
});

test("the inline result is keyed by owner, clears itself on its own timer, and unmount clears pending timers", () => {
  const show = body("showDeleteResult");
  assert.match(show, /window\.setTimeout\(/);
  assert.match(show, /KG_DELETE_RESULT_HOLD_MS/);
  assert.match(show, /current\.get\(key\) !== result/);
  assert.match(source, /deleteResult: visible\s*\?\s*deleteResults\.get\(maintenanceOwnerKey\(currentOwner\(\)!\)\) \?\? null/);
  assert.match(source, /deleting: visible && Boolean\(currentOwner\(\)\s*&& busyForNotebook\(deletingNotebookIds, maintenanceOwnerKey\(currentOwner\(\)!\)\)\)/);
  assert.match(
    source,
    /const timers = deleteResultTimersRef\.current;\s*return \(\) => \{\s*for \(const timer of timers\.values\(\)\) window\.clearTimeout\(timer\);/,
  );
});

test("owner recovery adopts a server-running delete under the actor+notebook key and records its job id", () => {
  const adopt = body("adoptOwner");
  assert.match(adopt, /fetchKgDeleteStatus\(owner\.notebookId\)/);
  assert.match(adopt, /expectedMaintenanceJobRef\.current\.set\(maintenanceJobKey\(owner, "delete"\), deletion\.job_id\)/);
  assert.match(adopt, /setDeletingNotebookIds\(\(current\) => claimNotebookSlot\(current, ownerKey\(owner\)\)\)/);
});

test("page confirms before delegating, pins the notebook, and every KG entry sees the delete bit", () => {
  const confirm = findFunctionIn(page, "Home", "confirmDeleteKg").getText(page);
  assert.match(confirm, /if \(kgGraph\.deleting \|\| kgGraph\.rebuilding \|\| kgGraph\.relinking \|\| kgGraph\.buildingKg\) return/);
  assert.match(
    confirm,
    /if \(kgGraph\.reviewBusy \|\| kgGraph\.reviewAllStarting \|\| kgGraph\.reviewAllRunning\s*\|\| kgGraph\.decidingMerge !== null\) return/,
  );
  assert.match(confirm, /confirmIndexAction\(\s*"删除知识图谱？\\n\\n/);
  assert.match(confirm, /if \(activeNotebookIdRef\.current === nb\) void kgWorkspace\.startKgDelete\(nb\)/);
  const confirmAt = confirm.indexOf("confirmIndexAction(");
  const startAt = confirm.indexOf("kgWorkspace.startKgDelete(");
  assert.ok(confirmAt >= 0 && startAt > confirmAt, "the hook may only run inside the confirm callback");

  for (const name of ["confirmRefreshUnifiedKg", "confirmGenerateKgAnalysis"]) {
    assert.match(
      findFunctionIn(page, "Home", name).getText(page),
      /kgGraph\.deleting\) return/,
      `${name} must treat a running delete as busy`,
    );
  }

  // Props are read off the exact elements, so a same-named prop on another component
  // (SourceListPanel also takes kgReady) cannot satisfy these.
  const view = onlyElement(page, "KgGraphView");
  assert.equal(view.bindings.kgReady, "Boolean(currentNotebook?.kg_ready)");
  assert.equal(view.bindings.confirmDeleteKg, "confirmDeleteKg");
  const analysis = onlyElement(page, "KgAnalysisView");
  assert.equal(analysis.bindings.analysisBlocked, "kgGraph.relinking || kgGraph.buildingKg || kgGraph.deleting");
  assert.equal(analysis.bindings.dataInvalidating, "kgGraph.deleting");
});

test("page refreshes the KG-external dependents through the guarded helper, invalidating Knowledge", () => {
  const pageText = page.getFullText();
  const at = pageText.indexOf("refreshAfterKgDelete: (targetNotebookId, guard) => refreshKgDeleteDependents(");
  assert.ok(at > 0, "refreshAfterKgDelete must delegate to refreshKgDeleteDependents");
  const wiring = pageText.slice(at, pageText.indexOf("focusGraphNode:", at));
  assert.match(wiring, /activeNotebookId: \(\) => activeNotebookIdRef\.current/);
  assert.match(wiring, /invalidateKnowledge: \(\) => kgWorkspace\.invalidateKnowledge\(\)/);
  assert.match(wiring, /knowledgeBrowserOpen: \(\) => chatMode === "rules"/);
  assert.match(wiring, /reenterKnowledge: \(\) => kgWorkspace\.enterKnowledge\(\)/);
  assert.match(wiring, /indexPanelOpen: \(\) => analytics !== null/);
  assert.match(wiring, /guard: stillCurrent/);
  // While the delete runs, the index panel's KG card must not keep describing the last
  // build of a graph that is being deleted.
  assert.match(pageText, /\{kgGraph\.deleting \? "正在删除知识图谱…" : view\.label\}/);
  assert.match(pageText, /const busy = kg\.job\?\.status === "running"[\s\S]{0,120}\|\| kgGraph\.deleting;/);
});

test("view disables every KG action on the delete bit and renders the result beside the button", () => {
  const buttons = jsxElements(kgGraphView, "button");
  const byClick = (match) => buttons.filter((element) => (element.bindings?.onClick ?? "").includes(match));

  const [deleteButton] = byClick("confirmDeleteKg");
  assert.ok(deleteButton, "delete button not found");
  for (const bit of [
    "kgGraph.deleting",
    "kgGraph.relinking",
    "kgGraph.rebuilding",
    "kgGraph.buildingKg",
    "!kgReady",
    "kgGraph.reviewBusy",
    "kgGraph.reviewAllStarting",
    "kgGraph.reviewAllRunning",
    "kgGraph.decidingMerge !== null",
  ]) assert.ok(deleteButton.bindings.disabled.includes(bit), `delete button disabled lacks ${bit}`);

  for (const match of ["relinkFromKgView", "confirmRefreshUnifiedKg", "startKgRebuild", "reviewPendingMerges", "reviewAllMerges", "decideMerge("]) {
    const matched = byClick(match);
    assert.ok(matched.length > 0, `${match} button not found`);
    for (const element of matched) {
      assert.ok(element.bindings.disabled?.includes("kgGraph.deleting"), `${match} disabled lacks kgGraph.deleting`);
    }
  }
  const viewText = kgGraphView.getFullText();
  assert.match(viewText, /\{kgGraph\.deleting \? "删除中…" : "删除知识图谱"\}/);
  assert.match(viewText, /role="status"[\s\S]{0,400}\{kgGraph\.deleteResult\.text\}/);
});
