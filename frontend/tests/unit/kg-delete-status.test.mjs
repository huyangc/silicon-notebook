import test from "node:test";
import assert from "node:assert/strict";

import {
  KG_DELETE_BUSY_MESSAGE,
  KG_DELETE_FAILED_MESSAGE,
  KG_DELETE_JOB_MISMATCH,
  KG_DELETE_POLL_MAX_ATTEMPTS,
  KG_DELETE_POLL_TIMED_OUT,
  KG_DELETE_RESULT_HOLD_MS,
  KG_DELETE_TIMEOUT_MESSAGE,
  KG_DELETE_UNKNOWN_MESSAGE,
  KG_DELETE_UNOBSERVED,
  kgDeletePollOutcome,
  kgDeleteTerminalSettlement,
  kgDeleteUnobservedOutcome,
} from "../../features/kg-maintenance/kg-delete-status.ts";

const base = {
  job_id: "kdj-1",
  notebook_id: "nb-1",
  status: "running",
  running: true,
  objects_deleted: 0,
  relations_deleted: 0,
};

test("running → 继续轮询，不解除忙碌位、不给结果", () => {
  assert.deepEqual(kgDeletePollOutcome(base), { done: false, refresh: false, result: null });
  // running 布尔与 status 字符串任一为真都算在跑：宁可多等一轮，也不提前放开按钮。
  assert.equal(kgDeletePollOutcome({ ...base, running: false }).done, false);
  assert.equal(kgDeletePollOutcome({ ...base, status: "succeeded" }).done, false);
});

test("请求失败/无回执 → 继续轮询（不能把瞬时错误当完成）", () => {
  assert.deepEqual(kgDeletePollOutcome(null), { done: false, refresh: false, result: null });
  assert.deepEqual(kgDeletePollOutcome(undefined), { done: false, refresh: false, result: null });
});

test("succeeded → 收工、刷新、按钮旁报服务端终态里的真实数字", () => {
  const outcome = kgDeletePollOutcome({
    ...base, status: "succeeded", running: false, objects_deleted: 12, relations_deleted: 30,
  });
  assert.equal(outcome.done, true);
  assert.equal(outcome.refresh, true);
  assert.deepEqual(outcome.result, { tone: "success", text: "已删除 12 个知识对象" });
});

test("succeeded 但一个都没删 → 文案不能说「已删除 0 个」", () => {
  const outcome = kgDeletePollOutcome({ ...base, status: "succeeded", running: false });
  assert.equal(outcome.done, true);
  assert.deepEqual(outcome.result, { tone: "success", text: "没有可删除的知识对象" });
});

test("failed → 收工、刷新(分页删除可能已删掉一部分)、给可操作的失败提示", () => {
  const outcome = kgDeletePollOutcome({ ...base, status: "failed", running: false });
  assert.equal(outcome.done, true);
  assert.equal(outcome.refresh, true, "删除分页提交，失败时图谱可能已变，必须重拉");
  assert.deepEqual(outcome.result, { tone: "failed", text: KG_DELETE_FAILED_MESSAGE });
  assert.equal(KG_DELETE_FAILED_MESSAGE, "删除没有完成，请重试");
});

test("idle 是终态：收工并刷新，结果是中性的「不知道」，绝不编造统计", () => {
  const outcome = kgDeletePollOutcome({ ...base, job_id: "", status: "idle", running: false });
  assert.equal(outcome.done, true, "把 idle 当运行中会让按钮永远转下去");
  assert.equal(outcome.refresh, true);
  assert.deepEqual(outcome.result, { tone: "neutral", text: KG_DELETE_UNKNOWN_MESSAGE });
  assert.equal(KG_DELETE_UNKNOWN_MESSAGE, "删除状态未知，请刷新后查看");
});

test("job_id 对不上时的收工回执不带任何数字", () => {
  assert.equal(KG_DELETE_JOB_MISMATCH.done, true);
  assert.equal(KG_DELETE_JOB_MISMATCH.refresh, true);
  assert.equal(KG_DELETE_JOB_MISMATCH.result?.tone, "neutral");
  assert.ok(!/\d/.test(KG_DELETE_JOB_MISMATCH.result?.text ?? "0"));
});

test("终态回执只有 job_id 正是期望的那个才以它的名义结算", () => {
  const succeeded = { ...base, status: "succeeded", running: false, objects_deleted: 120 };
  assert.equal(kgDeleteTerminalSettlement(succeeded, "kdj-1"), "report");
  assert.equal(kgDeleteTerminalSettlement(succeeded, "kdj-2"), "mismatch");
  // 进程重启后的 idle 恒为空 job_id：期望过某个任务却只剩 idle，也是对不上。
  assert.equal(kgDeleteTerminalSettlement({ job_id: "" }, "kdj-1"), "mismatch");
  // 没期望过任何任务（本标签页没提交成功、也没见过它在跑）：同进程更早一次删除的
  // succeeded 与这次点击无关，一律静默。
  assert.equal(kgDeleteTerminalSettlement(succeeded, undefined), "unobserved");
  assert.equal(kgDeleteTerminalSettlement(succeeded, ""), "unobserved");
});

test("静默收工：放掉忙碌位，但不刷新、不给结果", () => {
  assert.deepEqual(KG_DELETE_UNOBSERVED, { done: true, refresh: false, result: null });
});

test("没期望过的终态：确有删除跑完（非空 job_id 的 succeeded/failed）才刷新，仍不给结果；idle 是纯释放", () => {
  for (const status of ["succeeded", "failed"]) {
    assert.deepEqual(
      kgDeleteUnobservedOutcome({ job_id: "kdj-other-tab", status }),
      { done: true, refresh: true, result: null },
      status,
    );
  }
  assert.deepEqual(kgDeleteUnobservedOutcome({ job_id: "", status: "idle" }), KG_DELETE_UNOBSERVED);
  // 防御：空 job_id 的终态说明不了任何一次具体的删除。
  assert.deepEqual(kgDeleteUnobservedOutcome({ job_id: "", status: "succeeded" }), KG_DELETE_UNOBSERVED);
});

test("轮询尝试上限有界，超限回执中性（任务可能仍在跑，不说它失败了）", () => {
  assert.ok(Number.isInteger(KG_DELETE_POLL_MAX_ATTEMPTS));
  assert.ok(KG_DELETE_POLL_MAX_ATTEMPTS > 0 && KG_DELETE_POLL_MAX_ATTEMPTS <= 5000);
  assert.equal(KG_DELETE_POLL_TIMED_OUT.done, true, "超限必须解除忙碌位，否则按钮永久卡死");
  assert.equal(KG_DELETE_POLL_TIMED_OUT.refresh, true);
  assert.equal(KG_DELETE_POLL_TIMED_OUT.result?.tone, "neutral");
  assert.equal(KG_DELETE_POLL_TIMED_OUT.result?.text, KG_DELETE_TIMEOUT_MESSAGE);
  assert.equal(KG_DELETE_TIMEOUT_MESSAGE, "删除可能仍在进行，稍后重新打开知识图谱查看");
  for (const word of ["失败", "错误", "job", "KG"]) {
    assert.ok(
      !KG_DELETE_POLL_TIMED_OUT.result.text.includes(word),
      `超限只是「不等了」，文案不得出现「${word}」：${KG_DELETE_POLL_TIMED_OUT.result.text}`,
    );
  }
});

test("结果保留时长是有限的正数（结果必须按自己的计时器消失）", () => {
  assert.ok(Number.isInteger(KG_DELETE_RESULT_HOLD_MS));
  assert.ok(KG_DELETE_RESULT_HOLD_MS >= 1000 && KG_DELETE_RESULT_HOLD_MS <= 30_000);
});

test("结果与兜底文案里没有内部黑话", () => {
  const texts = [
    kgDeletePollOutcome({ ...base, status: "succeeded", running: false, objects_deleted: 3 }).result.text,
    kgDeletePollOutcome({ ...base, status: "succeeded", running: false }).result.text,
    kgDeletePollOutcome({ ...base, status: "failed", running: false }).result.text,
    kgDeletePollOutcome({ ...base, status: "idle", running: false }).result.text,
    KG_DELETE_BUSY_MESSAGE,
    KG_DELETE_TIMEOUT_MESSAGE,
  ];
  for (const text of texts) {
    assert.ok(/[一-龥]/.test(text), `结果文案必须是中文：${text}`);
    for (const jargon of ["KG", "kg", "job", "node", "节点", "边", "建图", "slot"]) {
      assert.ok(!text.includes(jargon), `界面文案不得出现「${jargon}」: ${text}`);
    }
  }
});
