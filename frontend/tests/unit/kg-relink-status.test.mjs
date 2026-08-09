import test from "node:test";
import assert from "node:assert/strict";

import {
  RELINK_POLL_MAX_ATTEMPTS,
  RELINK_POLL_TIMED_OUT,
  claimRelinkSlot,
  relinkBusyFor,
  releaseRelinkClaim,
  relinkPollOutcome,
} from "../../features/kg-maintenance/kg-relink-status.ts";

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
// 忙碌位按笔记本作用域（一个集合，多个库可以并发挂着，互不干扰）
// ---------------------------------------------------------------------------

test("忙碌位只对正在补的那个库为真", () => {
  assert.equal(relinkBusyFor(new Set(["nb-a"]), "nb-a"), true);
  // 切到 B：B 的按钮照常可点，A 的轮询在这里停下（切回 A 再接着轮）。
  assert.equal(relinkBusyFor(new Set(["nb-a"]), "nb-b"), false);
  assert.equal(relinkBusyFor(new Set(), "nb-a"), false);
  // 没开库时不该报忙——currentNotebookId 为 null 必须短路，不能落到 Set.has(null)。
  assert.equal(relinkBusyFor(new Set(["nb-a"]), null), false);
  assert.equal(relinkBusyFor(new Set(), null), false);
});

test("认领只加自己那一格：B 的认领不动 A 已经挂着的那一格", () => {
  const onlyA = new Set(["nb-a"]);
  const both = claimRelinkSlot(onlyA, "nb-b");
  assert.deepEqual([...both].sort(), ["nb-a", "nb-b"]);
  assert.deepEqual([...onlyA], ["nb-a"], "不得就地修改传入的集合");
  // 重复认领同一个库是幂等的。
  assert.deepEqual([...claimRelinkSlot(both, "nb-a")].sort(), ["nb-a", "nb-b"]);
});

test("结算只清自己那一格：A 的迟到终态不得抹掉 B 刚置上的忙碌位", () => {
  const both = new Set(["nb-a", "nb-b"]);
  const afterA = releaseRelinkClaim(both, "nb-a");
  assert.deepEqual([...afterA], ["nb-b"]);
  assert.deepEqual([...both].sort(), ["nb-a", "nb-b"], "不得就地修改传入的集合");
  assert.deepEqual([...releaseRelinkClaim(new Set(["nb-b"]), "nb-a")], ["nb-b"]);
  assert.deepEqual([...releaseRelinkClaim(new Set(), "nb-a")], []);
});

test("两个库可以真的同时挂着：点完 A 再点 B，A 的认领必须还在", () => {
  // 复现 P2 报的坏形态：单值状态下,起 B 的认领会覆盖 A 的,A 的完成信号从此收不到。
  let claims = new Set();
  claims = claimRelinkSlot(claims, "nb-a");
  claims = claimRelinkSlot(claims, "nb-b");
  assert.equal(relinkBusyFor(claims, "nb-a"), true, "起 B 之后 A 必须仍是忙碌的");
  assert.equal(relinkBusyFor(claims, "nb-b"), true);
  // A 先结算完成，B 的认领必须原样留着。
  claims = releaseRelinkClaim(claims, "nb-a");
  assert.equal(relinkBusyFor(claims, "nb-a"), false);
  assert.equal(relinkBusyFor(claims, "nb-b"), true, "A 的结算不得连带清掉 B");
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
