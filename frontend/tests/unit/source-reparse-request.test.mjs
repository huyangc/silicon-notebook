// 「重新解析」请求的一次提交(体检批量修复与图谱分析问题清单共用,source-reparse-request.ts)。
//
// 钉的是归属:请求在路上时切了笔记本或账号(`stillOwned` 不再成立),回来的结果属于被放弃的
// 工作区——不弹提示、不报错、不刷新体检与来源列表、不开修复轮询窗口,只回 "abandoned"。
// 仍归属时的三种结果(排上 / 一篇都没排上 / 请求失败)各自的副作用也一并钉住。
import test from "node:test";
import assert from "node:assert/strict";

import { reparseSkippedBusyText, requestReparse } from "../../app/source-reparse-request.ts";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** 记下每一个副作用;`reparse` 交给调用方控制什么时候回来。 */
function recorder(reparse) {
  const calls = [];
  const effects = {
    reparse: (notebookId, sourceIds) => {
      calls.push(["reparse", notebookId, sourceIds]);
      return reparse();
    },
    toast: (text) => calls.push(["toast", text]),
    refresh: () => calls.push(["refresh"]),
    armRepairPoll: () => calls.push(["armRepairPoll"]),
    reportError: (error) => calls.push(["reportError", error.message]),
  };
  return { calls, effects };
}

test("请求在路上时切了笔记本 / 账号：结果回来不弹提示、不刷新、不开轮询窗口，回 abandoned", async () => {
  for (const scheduled of [["src-d"], []]) {
    const pending = deferred();
    const { calls, effects } = recorder(() => pending.promise);
    let owned = true;
    const outcome = requestReparse("nb-a", ["src-d"], () => owned, effects);
    assert.deepEqual(calls, [["reparse", "nb-a", ["src-d"]]]);
    owned = false;
    pending.resolve({ scheduled });
    assert.equal(await outcome, "abandoned");
    assert.deepEqual(calls, [["reparse", "nb-a", ["src-d"]]], `scheduled=${scheduled.length}`);
  }
});

test("请求在路上时切了笔记本 / 账号，请求随后失败：也不在新工作区报错", async () => {
  const pending = deferred();
  const { calls, effects } = recorder(() => pending.promise);
  let owned = true;
  const outcome = requestReparse("nb-a", ["src-d"], () => owned, effects);
  owned = false;
  pending.reject(new Error("boom"));
  assert.equal(await outcome, "abandoned");
  assert.deepEqual(calls, [["reparse", "nb-a", ["src-d"]]]);
});

test("仍归属：排上了就提示、开轮询窗口、刷新；一篇都没排上只提示并刷新；失败只报错", async () => {
  const started = recorder(async () => ({ scheduled: ["src-d", "src-e"] }));
  assert.equal(await requestReparse("nb-a", ["src-d", "src-e"], () => true, started.effects), "started");
  assert.deepEqual(started.calls.slice(1), [
    ["toast", "已开始重新解析 2 篇来源；完成后会自动更新"],
    ["armRepairPoll"],
    ["refresh"],
  ]);

  const none = recorder(async () => ({ scheduled: [] }));
  assert.equal(await requestReparse("nb-a", ["src-d"], () => true, none.effects), "none");
  assert.deepEqual(none.calls.slice(1), [["toast", "这些来源已不在或无需重新解析；已刷新状态"], ["refresh"]]);

  const failed = recorder(async () => { throw new Error("boom"); });
  assert.equal(await requestReparse("nb-a", ["src-d"], () => true, failed.effects), "failed");
  assert.deepEqual(failed.calls.slice(1), [["reportError", "boom"]]);
});

test("没有来源要重新解析：不发请求，回 none", async () => {
  const { calls, effects } = recorder(async () => ({ scheduled: [] }));
  assert.equal(await requestReparse("nb-a", [], () => true, effects), "none");
  assert.deepEqual(calls, []);
});

test("分享编辑 P1(D8):全部跳过(skipped_busy)回 busy,不弹提示、不开轮询、不报错,只刷新", async () => {
  const { calls, effects } = recorder(async () => ({ scheduled: [], skipped_busy: ["src-a"] }));
  const outcome = await requestReparse("nb-1", ["src-a"], () => true, effects);
  assert.equal(outcome, "busy");
  assert.deepEqual(calls.map((call) => call[0]), ["reparse", "refresh"]);
});

test("分享编辑 P1(D8):部分跳过时照常开始,提示里说清跳过了几篇", async () => {
  const { calls, effects } = recorder(async () => ({ scheduled: ["src-a"], skipped_busy: ["src-b", "src-c"] }));
  const outcome = await requestReparse("nb-1", ["src-a", "src-b", "src-c"], () => true, effects);
  assert.equal(outcome, "started");
  assert.ok(calls.some((call) => call[0] === "toast" && call[1] === "已开始重新解析 1 篇来源；另有 2 篇正在解析，已跳过"));
});

test("分享编辑 P1(D8):部分跳过时把跳过的篇数交给调用方写在按钮旁(skippedBusy),不只靠 toast", async () => {
  const { calls, effects } = recorder(async () => ({ scheduled: ["src-a"], skipped_busy: ["src-b", "src-c"] }));
  effects.skippedBusy = (count) => calls.push(["skippedBusy", count]);
  assert.equal(await requestReparse("nb-1", ["src-a", "src-b", "src-c"], () => true, effects), "started");
  assert.deepEqual(calls.filter((call) => call[0] === "skippedBusy"), [["skippedBusy", 2]]);
  assert.equal(reparseSkippedBusyText(2), "另有 2 篇正在解析，已跳过");
  // 一篇都没跳过:不回调(按钮旁不挂一句「另有 0 篇」)。
  const clean = recorder(async () => ({ scheduled: ["src-a"], skipped_busy: [] }));
  clean.effects.skippedBusy = (count) => clean.calls.push(["skippedBusy", count]);
  await requestReparse("nb-1", ["src-a"], () => true, clean.effects);
  assert.equal(clean.calls.some((call) => call[0] === "skippedBusy"), false);
  // 等待期间换了工作区:结果不归当前,也不回调。
  const gone = recorder(async () => ({ scheduled: ["src-a"], skipped_busy: ["src-b"] }));
  gone.effects.skippedBusy = (count) => gone.calls.push(["skippedBusy", count]);
  assert.equal(await requestReparse("nb-1", ["src-a", "src-b"], () => false, gone.effects), "abandoned");
  assert.equal(gone.calls.some((call) => call[0] === "skippedBusy"), false);
});

test("批量重新解析的管线 409 保留原错误，不能被调用方笼统改报为忙碌", async () => {
  const conflict = Object.assign(new Error("当前索引管线不可用，请在设置中切回内建管线"), { status: 409 });
  const { calls, effects } = recorder(async () => { throw conflict; });
  // 复现旧页面把全部 409 当作忙碌的接线。批量忙碌由成功响应的 skipped_busy 表达，
  // 旧调用方的宽泛分类不得吞掉实际请求失败。
  effects.isBusyError = (error) => error === conflict;
  const outcome = await requestReparse("nb-1", ["src-a"], () => true, effects);
  assert.equal(outcome, "failed");
  assert.deepEqual(calls.slice(1), [["reportError", conflict.message]]);
});
