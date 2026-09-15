// Static wiring contract for the durable「删除知识图谱」owner. It mirrors
// kg-relink-wiring-guard: the delete is the third kind in the same server-side
// per-notebook maintenance slot, so every shape that keeps relink honest has to
// hold for it too. Runtime claim / poll / scoped-result behaviour is exercised
// by use-kg-workspace.component; the view by kg-graph-view.component.
import test from "node:test";
import assert from "node:assert/strict";

import { findFunction, findFunctionIn, parseModule } from "../../test-support/semantic-source.mjs";

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

test("delete is refused while any maintenance or build bit is busy, and it blocks theirs", () => {
  const start = body("startKgDelete");
  for (const bit of [
    "deletingNotebookIds.has(key)",
    "relinkingNotebookIds.has(key)",
    "rebuildingNotebookIds.has(key)",
    "buildingKg",
  ]) assert.ok(start.includes(bit), `startKgDelete early return lacks ${bit}`);
  // The other three entries must see the delete bit ("any busy bit ⇒ busy").
  assert.match(body("startRelink"), /deletingNotebookIds\.has\(key\)/);
  assert.match(body("launchRebuild"), /deletingNotebookIds\.has\(key\)/);
  assert.match(body("startKgBuild"), /deletingNotebookIds\.has\(ownerKey\(owner\)\)/);
  assert.match(body("decideMerge"), /deletingNotebookIds\.has\(ownerKey\(owner\)\)/);
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

test("delete uses one bounded 409 retry and every adoption probes all three maintenance kinds", () => {
  const start = body("startKgDelete");
  assert.match(start, /for \(const attempt of \[0, 1\]\)/);
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

test("submission windows and stale job ids cannot settle the delete early or borrow its counts", () => {
  const poll = deletePollBody();
  assert.match(poll, /if \(submittingMaintenanceRef\.current\.has\(jobKey\)\) return/);
  assert.match(poll, /expected && status\.job_id !== expected/);
  assert.match(poll, /mismatchStreak \+= 1/);
  assert.match(poll, /mismatchStreak < MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK/);
  assert.match(poll, /if \(!outcome\.done\) \{ mismatchStreak = 0; return; \}/);
  assert.match(poll, /await settle\(mismatched \? KG_DELETE_JOB_MISMATCH : outcome\)/);
});

test("terminal delete stops duplicate ticks, refreshes, shows the result, then releases its own slot", () => {
  const poll = deletePollBody();
  const settledAt = poll.indexOf("settled = true");
  const clearAt = poll.indexOf("window.clearInterval(timer)", settledAt);
  const settleAt = poll.indexOf("await settle(", clearAt);
  assert.ok(settledAt >= 0 && clearAt > settledAt && settleAt > clearAt);
  const settle = poll.slice(poll.indexOf("const settle = async"), poll.indexOf("const timer ="));
  const refreshAt = settle.indexOf("await refreshAfterDelete(owner)");
  const resultAt = settle.indexOf("showDeleteResult(key, outcome.result)");
  const releaseAt = settle.indexOf("releaseNotebookClaim(current, key)");
  assert.ok(refreshAt >= 0 && resultAt > refreshAt && releaseAt > resultAt);
  assert.match(settle, /expectedMaintenanceJobRef\.current\.delete/);
});

test("delete refresh covers graph, merges, notebook summary, and the page-owned dependents", () => {
  const refresh = body("refreshAfterDelete");
  assert.match(refresh, /refreshAfterRebuild\(\)/);
  assert.match(refresh, /effectsRef\.current\.refreshNotebook\(owner\.notebookId, guard\)/);
  assert.match(refresh, /effectsRef\.current\.refreshAfterKgDelete\(owner\.notebookId, guard\)/);
  // refreshAfterRebuild is the one that reads the graph at the current range.
  assert.match(body("refreshAfterRebuild"), /fetchUnifiedGraph\(owner\.notebookId, rangeLimitRef\.current\)/);
  assert.match(body("refreshAfterRebuild"), /fetchPendingMerges\(owner\.notebookId\)/);
});

test("the inline result is keyed by owner and clears itself on its own timer", () => {
  const show = body("showDeleteResult");
  assert.match(show, /window\.setTimeout\(/);
  assert.match(show, /KG_DELETE_RESULT_HOLD_MS/);
  assert.match(show, /current\.get\(key\) !== result/);
  assert.match(source, /deleteResult: visible\s*\?\s*deleteResults\.get\(maintenanceOwnerKey\(currentOwner\(\)!\)\) \?\? null/);
  assert.match(source, /deleting: visible && Boolean\(currentOwner\(\)\s*&& busyForNotebook\(deletingNotebookIds, maintenanceOwnerKey\(currentOwner\(\)!\)\)\)/);
});

test("owner recovery adopts a server-running delete under the actor+notebook key", () => {
  const adopt = body("adoptOwner");
  assert.match(adopt, /fetchKgDeleteStatus\(owner\.notebookId\)/);
  assert.match(adopt, /setDeletingNotebookIds\(\(current\) => claimNotebookSlot\(current, ownerKey\(owner\)\)\)/);
});

test("page confirms before delegating, pins the notebook, and every KG entry sees the delete bit", () => {
  const confirm = findFunctionIn(page, "Home", "confirmDeleteKg").getText(page);
  assert.match(confirm, /if \(kgGraph\.deleting \|\| kgGraph\.rebuilding \|\| kgGraph\.relinking \|\| kgGraph\.buildingKg\) return/);
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
  const pageText = page.getFullText();
  assert.match(pageText, /analysisBlocked=\{kgGraph\.relinking \|\| kgGraph\.buildingKg \|\| kgGraph\.deleting\}/);
  assert.match(pageText, /kgReady=\{Boolean\(currentNotebook\?\.kg_ready\)\}/);
  assert.match(pageText, /confirmDeleteKg=\{confirmDeleteKg\}/);
});

test("view disables all four KG actions on the delete bit and renders the result beside the button", () => {
  const view = kgGraphView.getFullText();
  assert.match(view, /disabled=\{kgGraph\.deleting \|\| kgGraph\.relinking \|\| kgGraph\.rebuilding \|\| kgGraph\.buildingKg \|\| !kgReady\}/);
  assert.match(view, /disabled=\{kgGraph\.relinking \|\| kgGraph\.rebuilding \|\| kgGraph\.buildingKg \|\| kgGraph\.deleting\}/);
  assert.match(view, /disabled=\{kgGraph\.rebuilding \|\| kgGraph\.relinking \|\| kgGraph\.buildingKg \|\| kgGraph\.deleting\}/);
  assert.match(view, /disabled=\{kgGraph\.buildingKg \|\| kgGraph\.deleting\}/);
  assert.match(view, /\{kgGraph\.deleting \? "删除中…" : "删除知识图谱"\}/);
  assert.match(view, /role="status"[\s\S]{0,400}\{kgGraph\.deleteResult\.text\}/);
});
