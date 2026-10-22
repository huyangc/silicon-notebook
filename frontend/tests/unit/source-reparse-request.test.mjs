// 「重新解析」请求的一次提交(体检批量修复与图谱分析问题清单共用,source-reparse-request.ts)。
//
// 钉的是归属:请求在路上时切了笔记本或账号(`stillOwned` 不再成立),回来的结果属于被放弃的
// 工作区——不弹提示、不报错、不刷新体检与来源列表、不开修复轮询窗口,只回 "abandoned"。
// 仍归属时的三种结果(排上 / 一篇都没排上 / 请求失败)各自的副作用也一并钉住。
import test from "node:test";
import assert from "node:assert/strict";

import { requestReparse } from "../../app/source-reparse-request.ts";

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
