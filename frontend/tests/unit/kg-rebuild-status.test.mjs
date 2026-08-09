import test from "node:test";
import assert from "node:assert/strict";

import {
  REBUILD_POLL_MAX_ATTEMPTS,
  REBUILD_POLL_TIMED_OUT,
  busyForNotebook,
  claimNotebookSlot,
  rebuildPollOutcome,
  releaseNotebookClaim,
} from "../../features/kg-maintenance/kg-rebuild-status.ts";

const base = {
  job_id: "ukj-1",
  notebook_id: "nb-1",
  status: "running",
  running: true,
  clusters: 0,
};

test("running → 继续轮询，不解除忙碌位、不弹提示", () => {
  assert.deepEqual(rebuildPollOutcome(base), { done: false, refresh: false, toast: null });
  // running 布尔与 status 字符串任一为真都算在跑（两者由同一个服务端字段派生，但回执
  // 缺一不可靠时宁可多等一轮，也不要提前把按钮放开）。
  assert.equal(rebuildPollOutcome({ ...base, running: false }).done, false);
  assert.equal(rebuildPollOutcome({ ...base, status: "succeeded" }).done, false);
});

test("请求失败/无回执 → 继续轮询（不能把瞬时错误当完成）", () => {
  assert.deepEqual(rebuildPollOutcome(null), { done: false, refresh: false, toast: null });
  assert.deepEqual(rebuildPollOutcome(undefined), { done: false, refresh: false, toast: null });
});

test("succeeded → 收工、重拉、报服务端终态里的真实数字", () => {
  const outcome = rebuildPollOutcome({
    ...base, status: "succeeded", running: false, clusters: 128,
  });
  assert.deepEqual(outcome, {
    done: true, refresh: true, toast: "已重新合并，现有 128 组概念",
  });
});

test("succeeded 但一组都没有 → 不说「现有 0 组概念」", () => {
  const outcome = rebuildPollOutcome({
    ...base, status: "succeeded", running: false, clusters: 0,
  });
  assert.equal(outcome.done, true);
  assert.equal(outcome.toast, "已重新合并，暂时没有可合并的概念");
});

test("failed → 收工、重拉、给可操作的提示（不暴露内部原因）", () => {
  const outcome = rebuildPollOutcome({ ...base, status: "failed", running: false });
  assert.deepEqual(outcome, {
    done: true, refresh: true, toast: "重新合并没有完成，请稍后重试",
  });
});

test("idle 是终态：收工并重拉，但不编统计", () => {
  // idle 同时覆盖「从没跑过」「进程重启了」「这一格现在被『补上关联』占着」——后一种
  // 尤其重要:共用单飞槽之后，服务端会对不属于自己的那种任务如实回 idle，轮询必须收工
  // 而不是替别人的任务空等到上限。
  const outcome = rebuildPollOutcome({
    job_id: "", notebook_id: "nb-1", status: "idle", running: false, clusters: 0,
  });
  assert.deepEqual(outcome, { done: true, refresh: true, toast: null });
});

test("轮询上限存在且是有限正数（否则按钮可能永远解锁不了）", () => {
  assert.ok(Number.isInteger(REBUILD_POLL_MAX_ATTEMPTS) && REBUILD_POLL_MAX_ATTEMPTS > 0);
  assert.equal(REBUILD_POLL_TIMED_OUT.done, true);
  assert.equal(REBUILD_POLL_TIMED_OUT.refresh, true);
  // 中性措辞:任务可能还在跑，不能说它失败了。
  assert.ok(!/失败|没有完成/.test(REBUILD_POLL_TIMED_OUT.toast ?? ""));
});

test("忙碌位是按库作用域的集合：认领/解除只动自己那一格", () => {
  // 与「补上关联」共用同一份实现（notebook-busy-set.ts）——这里钉的是「重新合并这条路
  // 也确实用的是集合语义」，点完 A 切到 B 再点一次时两次认领都要活着。
  const afterA = claimNotebookSlot(new Set(), "nb-a");
  const afterB = claimNotebookSlot(afterA, "nb-b");
  assert.deepEqual([...afterB].sort(), ["nb-a", "nb-b"]);
  assert.equal(busyForNotebook(afterB, "nb-a"), true);
  assert.equal(busyForNotebook(afterB, "nb-c"), false);
  assert.equal(busyForNotebook(afterB, null), false);

  const settledA = releaseNotebookClaim(afterB, "nb-a");
  assert.deepEqual([...settledA], ["nb-b"], "A 的终态不能把 B 刚置上的忙碌位一并抹掉");
  assert.equal(releaseNotebookClaim(settledA, "nb-a"), settledA, "重复结算不该造新对象");
});
