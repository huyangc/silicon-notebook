import test from "node:test";
import assert from "node:assert/strict";

import {
  RELINK_POLL_MAX_ATTEMPTS,
  RELINK_POLL_TIMED_OUT,
  relinkBusyFor,
  releaseRelinkClaim,
  relinkPollOutcome,
} from "./kg-relink-status.ts";

const base = {
  job_id: "rlj-1",
  notebook_id: "nb-1",
  status: "running",
  running: true,
  isolated_before: 0,
  edges_added: 0,
  isolated_after: 0,
};

test("running → 继续轮询，不解除忙碌位、不弹提示", () => {
  assert.deepEqual(relinkPollOutcome(base), { done: false, refresh: false, toast: null });
  // running 布尔与 status 字符串任一为真都算在跑（两者由同一个服务端字段派生，
  // 但回执缺一不可靠时宁可多等一轮，也不要提前把按钮放开）。
  assert.equal(relinkPollOutcome({ ...base, running: false }).done, false);
  assert.equal(relinkPollOutcome({ ...base, status: "succeeded" }).done, false);
});

test("请求失败/无回执 → 继续轮询（不能把瞬时错误当完成）", () => {
  assert.deepEqual(relinkPollOutcome(null), { done: false, refresh: false, toast: null });
  assert.deepEqual(relinkPollOutcome(undefined), { done: false, refresh: false, toast: null });
});

test("succeeded → 收工、重拉、报真实数字", () => {
  const outcome = relinkPollOutcome({
    ...base, status: "succeeded", running: false,
    isolated_before: 9, edges_added: 4, isolated_after: 5,
  });
  assert.equal(outcome.done, true);
  assert.equal(outcome.refresh, true);
  assert.equal(outcome.toast, "已补上 4 条关联，还有 5 项内容没建立关联");
});

test("succeeded 但一条都没补 → 文案不能说「已补上 0 条」", () => {
  const outcome = relinkPollOutcome({
    ...base, status: "succeeded", running: false, edges_added: 0, isolated_after: 3,
  });
  assert.equal(outcome.done, true);
  assert.equal(outcome.toast, "没有可补的关联，还有 3 项内容没建立关联");
});

test("failed → 收工、重拉、给可操作的提示（不暴露内部原因）", () => {
  const outcome = relinkPollOutcome({ ...base, status: "failed", running: false });
  assert.equal(outcome.done, true);
  assert.equal(outcome.refresh, true);
  assert.equal(outcome.toast, "补上关联没有完成，请稍后重试");
});

test("idle 是终态：收工并重拉，但绝不编造统计", () => {
  const outcome = relinkPollOutcome({
    ...base, job_id: "", status: "idle", running: false,
  });
  assert.equal(outcome.done, true, "把 idle 当运行中会让按钮永远转下去");
  assert.equal(outcome.refresh, true);
  assert.equal(outcome.toast, null);
});

test("终态文案里没有内部黑话", () => {
  const toasts = [
    relinkPollOutcome({ ...base, status: "succeeded", running: false, edges_added: 2 }).toast,
    relinkPollOutcome({ ...base, status: "succeeded", running: false }).toast,
    relinkPollOutcome({ ...base, status: "failed", running: false }).toast,
  ];
  for (const toast of toasts) {
    assert.ok(toast);
    for (const jargon of ["KG", "chunk", "relink", "job", "node", "边", "孤立节点"]) {
      assert.ok(!toast.includes(jargon), `界面文案不得出现「${jargon}」: ${toast}`);
    }
  }
});

// ---------------------------------------------------------------------------
// 忙碌位按笔记本作用域（切库不该互相干扰）
// ---------------------------------------------------------------------------

test("忙碌位只对正在补的那个库为真", () => {
  assert.equal(relinkBusyFor("nb-a", "nb-a"), true);
  // 切到 B：B 的按钮照常可点，A 的轮询在这里停下（切回 A 再接着轮）。
  assert.equal(relinkBusyFor("nb-a", "nb-b"), false);
  assert.equal(relinkBusyFor(null, "nb-a"), false);
  // 两边都空不是「同一个库」——裸 `===` 会让没开库时按钮莫名变灰。
  assert.equal(relinkBusyFor(null, null), false);
});

test("结算只清自己那一格：A 的迟到终态不得抹掉 B 刚置上的忙碌位", () => {
  assert.equal(releaseRelinkClaim("nb-a", "nb-a"), null);
  assert.equal(releaseRelinkClaim("nb-b", "nb-a"), "nb-b");
  assert.equal(releaseRelinkClaim(null, "nb-a"), null);
});

test("轮询尝试上限是有界的，且超限回执中性（不说任务失败了）", () => {
  assert.ok(Number.isInteger(RELINK_POLL_MAX_ATTEMPTS));
  assert.ok(RELINK_POLL_MAX_ATTEMPTS > 0 && RELINK_POLL_MAX_ATTEMPTS <= 5000);
  assert.equal(RELINK_POLL_TIMED_OUT.done, true, "超限必须解除忙碌位，否则按钮永久卡死");
  assert.equal(RELINK_POLL_TIMED_OUT.refresh, true);
  assert.ok(RELINK_POLL_TIMED_OUT.toast);
  for (const word of ["失败", "错误", "job", "KG"]) {
    assert.ok(
      !RELINK_POLL_TIMED_OUT.toast.includes(word),
      `超限只是「不等了」，任务可能仍在跑，文案不得出现「${word}」：${RELINK_POLL_TIMED_OUT.toast}`,
    );
  }
});
