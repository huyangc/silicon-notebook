// Static wiring contract for the durable unified-KG rebuild owner. Runtime owner,
// A→B→A, timer, and permission behavior is exercised by use-kg-workspace.component.
import test from "node:test";
import assert from "node:assert/strict";

import { findFunction, parseModule } from "../../test-support/semantic-source.mjs";

// 拆分后 rebuild 追踪落在 KG 图谱领域 owner 里（`use-kg-workspace.ts` 只剩组合层）。
const hook = await parseModule("use-kg-graph.ts");
const page = await parseModule("page.tsx");
const source = hook.getFullText();

function body(name) {
  return findFunction(hook, name).getText(hook);
}

function rebuildPollBody() {
  const start = source.indexOf("if (!rebuildingNotebookIds.has(key)) return;");
  assert.ok(start > 0, "missing rebuild polling effect");
  const end = source.indexOf("}, [rebuildingNotebookIds, ownerVersion]);", start);
  assert.ok(end > start, "missing rebuild polling dependencies");
  return source.slice(start, end);
}

test("rebuild claims the actor+notebook slot before POST and never reads result counts", () => {
  const launch = body("launchRebuild");
  const claimAt = launch.indexOf("claimNotebookSlot(current, key)");
  const postAt = launch.indexOf("rebuildUnifiedKg(");
  assert.ok(claimAt >= 0 && postAt > claimAt);
  for (const forbidden of ["clusters", "cluster_count"]) assert.equal(launch.includes(forbidden), false);
  assert.match(body("maintenanceOwnerKey"), /ownerKey\(owner\)/);
});

test("rebuild submission has a kind-specific marker, exact job expectation, and finally cleanup", () => {
  const launch = body("launchRebuild");
  assert.match(launch, /maintenanceJobKey\(owner, "rebuild"\)/);
  const addAt = launch.indexOf("submittingMaintenanceRef.current.add(jobKey)");
  const postAt = launch.indexOf("rebuildUnifiedKg(");
  const expectAt = launch.indexOf("expectedMaintenanceJobRef.current.set(jobKey, started.job_id)");
  const deleteAt = launch.indexOf("submittingMaintenanceRef.current.delete(jobKey)");
  assert.ok(addAt >= 0 && addAt < postAt && postAt < expectAt && expectAt < deleteAt);
});

test("rebuild uses exactly one bounded 409 retry and adopts either running maintenance kind", () => {
  const launch = body("launchRebuild");
  assert.match(launch, /for \(const attempt of \[0, 1\]\)/);
  assert.match(launch, /httpErrorStatus\(error\) === 409/);
  assert.match(launch, /await adoptRunningMaintenance\(owner\)/);
  assert.match(launch, /if \(verdict !== "idle"\) return/);
  assert.match(body("adoptRunningMaintenance"), /Promise\.allSettled\(\[/);
  assert.match(body("adoptRunningMaintenance"), /setRebuildingNotebookIds/);
  assert.match(body("adoptRunningMaintenance"), /setRelinkingNotebookIds/);
});

test("manual rebuild and merge-confirm share the same mutually-exclusive busy slot", () => {
  const launch = body("launchRebuild");
  assert.match(launch, /rebuildingNotebookIds\.has\(key\) \|\| relinkingNotebookIds\.has\(key\) \|\| buildingKg/);
  assert.match(body("startRebuild"), /await launchRebuild\(owner\)/);
  const decision = body("decideMerge");
  assert.match(decision, /if \(confirm\) await confirmMerge/);
  assert.match(decision, /else await rejectMerge/);
  assert.match(decision, /if \(confirm\) await launchRebuild/);
});

test("a confirmed merge records a pending rebuild before adopting a 409 task", () => {
  const launch = body("launchRebuild");
  const pendingAt = launch.indexOf("pendingRebuildRef.current.add(key)");
  const adoptAt = launch.indexOf("await adoptRunningMaintenance(owner)");
  assert.ok(pendingAt >= 0 && adoptAt > pendingAt);
  assert.match(launch, /pendingRebuildRef\.current\.delete\(key\)/);
});

test("rebuild polling is bounded, single-flight, generation-aware, and range-stable", () => {
  const poll = rebuildPollBody();
  assert.match(poll, /let inFlight = false/);
  assert.match(poll, /if \(stopped \|\| settled \|\| inFlight\) return/);
  assert.match(poll, /REBUILD_POLL_MAX_ATTEMPTS/);
  assert.match(poll, /REBUILD_POLL_TIMED_OUT/);
  assert.match(poll, /fetchUnifiedKgRebuildStatus\(owner\.notebookId\)/);
  assert.match(poll, /!ownsIdentity\(owner\)/);
  assert.equal(poll.includes("rangeLimit"), false, "range changes must not restart the maintenance poll");
});

test("submission windows and stale job ids cannot settle the rebuild early", () => {
  const poll = rebuildPollBody();
  assert.match(poll, /submittingMaintenanceRef\.current\.has\(jobKey\)/);
  assert.match(poll, /expected && status\.job_id !== expected/);
  assert.match(poll, /mismatchStreak \+= 1/);
  assert.match(poll, /mismatchStreak < MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK/);
  assert.match(poll, /if \(!outcome\.done\) \{ mismatchStreak = 0; return; \}/);
});

test("terminal observation stops duplicate ticks before refresh and releases only after refresh", () => {
  const poll = rebuildPollBody();
  const settledAt = poll.indexOf("settled = true");
  const settleAt = poll.indexOf("await settle(outcome,", settledAt);
  assert.ok(settledAt >= 0 && settleAt > settledAt);
  assert.match(
    poll.slice(settleAt, settleAt + 120),
    /status\.job_id[\s\S]*status\.status/,
    "the terminal receipt must remain tied to the observed job and status",
  );
  const settle = poll.slice(poll.indexOf("const settle = async"), poll.indexOf("const timer ="));
  const refreshAt = settle.indexOf("await refreshAfterRebuild()");
  const releaseAt = settle.indexOf("releaseNotebookClaim(current, key)");
  assert.ok(refreshAt >= 0 && releaseAt > refreshAt);
  assert.match(settle, /expectedMaintenanceJobRef\.current\.delete/);
});

test("pending rebuild retry stays claimed and reuses the same durable poll", () => {
  const poll = rebuildPollBody();
  assert.match(poll, /pendingRebuildRef\.current\.has\(key\)/);
  assert.match(poll, /const launch = await launchRebuild\(owner, \{[\s\S]*pendingRetry: true/);
  assert.match(poll, /launch === "started" \|\| launch === "adopted" \|\| launch === "waiting"/);
  assert.match(poll, /settled = false/);
  assert.match(poll, /attempts = 0/);
  assert.match(poll, /mismatchStreak = 0/);
});

test("rebuild refresh reaccounts the selected concept after replacing the graph", () => {
  const refresh = body("refreshAfterRebuild");
  assert.match(refresh, /const selection = selectedNodeIdRef\.current/);
  assert.match(refresh, /setUnifiedGraph\(graph\)/);
  assert.match(refresh, /fetchConceptDetail\(owner\.notebookId, selected\.id\)/);
  assert.match(refresh, /setConceptDetail\(detail\)/);
  assert.match(refresh, /setNodeContext\(null\)/);
});

// 见 kg-relink-wiring-guard 里同名测试的说明：owner 建立时的恢复探针现在是这个
// 领域 owner 的具名命令 `adoptOwner`，不再是一段按文本切出来的 effect 前缀。
test("owner recovery adopts server-running rebuild without inferring pending work from dirty", () => {
  const adopt = body("adoptOwner");
  assert.match(adopt, /fetchUnifiedKgRebuildStatus\(owner\.notebookId\)/);
  assert.match(adopt, /claimNotebookSlot\(current, ownerKey\(owner\)\)/);
  assert.equal(adopt.includes("pendingRebuildRef.current.add"), false);
  assert.equal(adopt.includes("dirty"), false);
});

test("presentation disables both maintenance actions while either shared task is busy", () => {
  const text = page.getFullText();
  assert.match(text, /if \(kgGraph\.rebuilding \|\| kgGraph\.relinking \|\| kgGraph\.buildingKg\) return/);
  assert.match(text, /disabled=\{kgGraph\.rebuilding \|\| kgGraph\.relinking \|\| kgGraph\.buildingKg\}/);
});
