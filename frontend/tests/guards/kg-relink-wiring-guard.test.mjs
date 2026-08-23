// Static wiring contract for the durable isolated-node relink owner.
import test from "node:test";
import assert from "node:assert/strict";

import { findFunction, parseModule } from "../../test-support/semantic-source.mjs";

// 拆分后 relink 追踪落在 KG 图谱领域 owner 里（`use-kg-workspace.ts` 只剩组合层）。
const hook = await parseModule("use-kg-graph.ts");
const page = await parseModule("page.tsx");
const source = hook.getFullText();

function body(name) {
  return findFunction(hook, name).getText(hook);
}

function relinkPollBody() {
  const start = source.indexOf("if (!relinkingNotebookIds.has(key)) return;");
  assert.ok(start > 0, "missing relink polling effect");
  const end = source.indexOf("}, [relinkingNotebookIds, ownerVersion]);", start);
  assert.ok(end > start, "missing relink polling dependencies");
  return source.slice(start, end);
}

test("relink claims the actor+notebook slot before POST and never reads old synchronous stats", () => {
  const start = body("startRelink");
  const claimAt = start.indexOf("claimNotebookSlot(current, key)");
  const postAt = start.indexOf("relinkKg(");
  assert.ok(claimAt >= 0 && postAt > claimAt);
  for (const forbidden of ["edges_added", "isolated_after", "isolated_before"]) {
    assert.equal(start.includes(forbidden), false);
  }
  assert.match(start, /relinkingNotebookIds\.has\(key\) \|\| rebuildingNotebookIds\.has\(key\) \|\| buildingKg/);
});

test("relink submission has a kind-specific marker, exact job expectation, and finally cleanup", () => {
  const start = body("startRelink");
  assert.match(start, /maintenanceJobKey\(owner, "relink"\)/);
  const addAt = start.indexOf("submittingMaintenanceRef.current.add(jobKey)");
  const postAt = start.indexOf("relinkKg(");
  const expectAt = start.indexOf("expectedMaintenanceJobRef.current.set(jobKey, started.job_id)");
  const deleteAt = start.indexOf("submittingMaintenanceRef.current.delete(jobKey)");
  assert.ok(addAt >= 0 && addAt < postAt && postAt < expectAt && expectAt < deleteAt);
});

test("relink uses one bounded 409 retry and adopts either running maintenance kind", () => {
  const start = body("startRelink");
  assert.match(start, /for \(const attempt of \[0, 1\]\)/);
  assert.match(start, /httpErrorStatus\(error\) === 409/);
  assert.match(start, /await adoptRunningMaintenance\(owner\)/);
  assert.match(start, /if \(verdict !== "idle"\) return/);
  const adopt = body("adoptRunningMaintenance");
  assert.match(adopt, /Promise\.allSettled\(\[/);
  assert.match(adopt, /setRebuildingNotebookIds/);
  assert.match(adopt, /setRelinkingNotebookIds/);
});

test("relink polling is bounded, single-flight, identity-aware, and range-stable", () => {
  const poll = relinkPollBody();
  assert.match(poll, /let inFlight = false/);
  assert.match(poll, /if \(stopped \|\| settled \|\| inFlight\) return/);
  assert.match(poll, /RELINK_POLL_MAX_ATTEMPTS/);
  assert.match(poll, /RELINK_POLL_TIMED_OUT/);
  assert.match(poll, /fetchRelinkStatus\(owner\.notebookId\)/);
  assert.match(poll, /!ownsIdentity\(owner\)/);
  assert.equal(poll.includes("rangeLimit"), false);
});

test("submission windows and stale job ids cannot settle relink early", () => {
  const poll = relinkPollBody();
  assert.match(poll, /if \(submittingMaintenanceRef\.current\.has\(jobKey\)\) return/);
  assert.match(poll, /expected && status\.job_id !== expected/);
  assert.match(poll, /mismatchStreak \+= 1/);
  assert.match(poll, /mismatchStreak < MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK/);
  assert.match(poll, /if \(!outcome\.done\) \{ mismatchStreak = 0; return; \}/);
});

test("terminal relink stops duplicate ticks, refreshes, then releases its own slot", () => {
  const poll = relinkPollBody();
  const settledAt = poll.indexOf("settled = true");
  const clearAt = poll.indexOf("window.clearInterval(timer)", settledAt);
  const settleAt = poll.indexOf("await settle(outcome)", clearAt);
  assert.ok(settledAt >= 0 && clearAt > settledAt && settleAt > clearAt);
  const settle = poll.slice(poll.indexOf("const settle = async"), poll.indexOf("const timer ="));
  const refreshAt = settle.indexOf("await refreshAfterRelink()");
  const releaseAt = settle.indexOf("releaseNotebookClaim(current, key)");
  assert.ok(refreshAt >= 0 && releaseAt > refreshAt);
  assert.match(settle, /expectedMaintenanceJobRef\.current\.delete/);
});

test("relink refresh reads the current range through its ref, not poll dependencies", () => {
  const refresh = body("refreshAfterRelink");
  assert.match(refresh, /fetchUnifiedGraph\(owner\.notebookId, rangeLimitRef\.current\)/);
  assert.match(refresh, /fetchUnifiedKgStatus\(owner\.notebookId\)/);
  assert.match(refresh, /setUnifiedGraph\(graph\)/);
});

// 新 owner 建立时的维护态恢复探针现在是这个领域 owner 自己的具名命令
// `adoptOwner(owner, notebookSnapshot)`——由 `use-kg-owner.ts` 的 owner 建立
// effect 经组合层扇出调用。按函数体断言比原来的「从第一个 useEffect 切到
// beginNotebookTransition」文本切片更精确，且 findFunction 找不到就直接抛，
// 天然是空转保护。
test("owner recovery adopts a server-running relink under the actor+notebook key", () => {
  const adopt = body("adoptOwner");
  assert.match(adopt, /fetchRelinkStatus\(owner\.notebookId\)/);
  assert.match(adopt, /claimNotebookSlot\(current, ownerKey\(owner\)\)/);
});

test("presentation delegates relink to the hook and disables it during either maintenance kind", () => {
  const text = page.getFullText();
  assert.match(body("startRelink"), /policyRef\.current\.canWriteKg/);
  assert.match(text, /async function relinkFromKgView\(\)[\s\S]{0,120}kgWorkspace\.startRelink\(\)/);
  assert.match(text, /disabled=\{kgGraph\.relinking \|\| kgGraph\.rebuilding \|\| kgGraph\.buildingKg\}/);
});
