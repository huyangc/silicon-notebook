import test from "node:test";
import assert from "node:assert/strict";

import { relinkPollOutcome } from "./kg-relink-status.ts";

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
