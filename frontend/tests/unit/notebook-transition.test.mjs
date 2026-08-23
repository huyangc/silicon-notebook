// `app/notebook-transition.ts` —— 打开笔记本的单一 transition 编排（纯逻辑）。
//
// 这里钉的是编排器**自己**的确定性，与 page.tsx 接了几个 owner 无关：
//  ① 任一步拒绝、取数失败、被顶替，都要把**已 begin 成功**的步骤按 begin 的逆序
//    全部回滚，一个不落、一个不多；
//  ② 被顶替的旧 transition 只 settle 自己那批 ticket，绝不碰更晚的 transition 刚
//    建立的 owner（这正是「迟到完成不得作废新工作区」在编排层的落点）；
//  ③ 成功路径上每个步骤的 commit 恰好跑一次、按声明序，settle 也恰好一次。
import test from "node:test";
import assert from "node:assert/strict";

import {
  runNotebookTransition,
  transitionStep,
} from "../../app/notebook-transition.ts";

/** 记录 begin / commit / settle 的探针步骤。`refuse` 让 begin 返回 null。 */
function probeStep(name, log, options = {}) {
  let serial = 0;
  return transitionStep({
    name,
    begin: () => {
      log.push({ phase: "begin", name });
      if (options.refuse) return null;
      serial += 1;
      const ticket = { name, serial };
      (options.tickets ?? []).push(ticket);
      return ticket;
    },
    commit: options.commit === false ? undefined : (ticket, context) => {
      log.push({ phase: "commit", name, ticket, outcome: context.outcome });
      return options.async ? Promise.resolve(1) : undefined;
    },
    settle: (ticket, outcome) => {
      log.push({ phase: "settle", name, ticket, outcome });
    },
  });
}

const alwaysCurrent = () => true;

function plan(steps, overrides = {}) {
  return {
    steps,
    load: async () => ({ value: "loaded" }),
    isCurrent: alwaysCurrent,
    apply: () => ({ committed: true }),
    conclude: () => true,
    ...overrides,
  };
}


test("全部成功：每步恰好一次 commit（按声明序）与一次 settle（按逆序）", async () => {
  const log = [];
  const steps = [probeStep("a", log), probeStep("b", log), probeStep("c", log)];
  const result = await runNotebookTransition(plan(steps));

  assert.equal(result.status, "committed");
  assert.deepEqual(result.outcome, { committed: true });

  assert.deepEqual(
    log.filter((entry) => entry.phase === "begin").map((entry) => entry.name),
    ["a", "b", "c"],
  );
  const commits = log.filter((entry) => entry.phase === "commit");
  assert.deepEqual(commits.map((entry) => entry.name), ["a", "b", "c"]);
  for (const entry of commits) assert.deepEqual(entry.outcome, { committed: true });

  const settles = log.filter((entry) => entry.phase === "settle");
  assert.deepEqual(settles.map((entry) => entry.name), ["c", "b", "a"]);
  for (const entry of settles) assert.deepEqual(entry.outcome, { committed: true });
});


test("任一步拒绝：已 begin 的全部逆序回滚，拒绝的那步不 settle，enter/load 不执行", async () => {
  const log = [];
  const steps = [
    probeStep("a", log),
    probeStep("refuser", log, { refuse: true }),
    probeStep("c", log),
  ];
  let entered = 0;
  let loads = 0;
  const result = await runNotebookTransition(plan(steps, {
    enter: () => { entered += 1; },
    load: async () => { loads += 1; return { value: "loaded" }; },
  }));

  assert.equal(result.status, "refused");
  assert.deepEqual(result.refusedBy, ["refuser"]);
  // 拒绝**不**短路：排在拒绝者之后的步骤照常 begin（两个会拒绝的 owner 各按自己的
  // actor ref 判定，短路会让后面的 owner 连 begin 都没跑过）。
  assert.deepEqual(
    log.filter((entry) => entry.phase === "begin").map((entry) => entry.name),
    ["a", "refuser", "c"],
  );
  assert.deepEqual(
    log.filter((entry) => entry.phase === "settle").map((entry) => entry.name),
    ["c", "a"],
  );
  for (const entry of log.filter((entry) => entry.phase === "settle")) {
    assert.equal(entry.outcome, null);
  }
  assert.equal(log.some((entry) => entry.phase === "commit"), false);
  assert.equal(entered, 0);
  assert.equal(loads, 0);
});


test("并行取数其一失败：已执行的 begin 全部逆序回滚，错误原样抛出", async () => {
  const log = [];
  const steps = [probeStep("a", log), probeStep("b", log), probeStep("c", log)];
  const boom = new Error("sources page failed");
  await assert.rejects(
    () => runNotebookTransition(plan(steps, {
      // 两个请求并行，其中一个拒绝 —— Promise.all 整体拒绝。
      load: () => Promise.all([Promise.resolve({}), Promise.reject(boom)]),
    })),
    (error) => error === boom,
  );

  assert.deepEqual(
    log.filter((entry) => entry.phase === "settle").map((entry) => entry.name),
    ["c", "b", "a"],
  );
  for (const entry of log.filter((entry) => entry.phase === "settle")) {
    assert.equal(entry.outcome, null);
  }
  assert.equal(log.some((entry) => entry.phase === "commit"), false);
});


test("迟到完成（取数期间被顶替）：不提交，且只 settle 自己那批 ticket", async () => {
  const log = [];
  const mine = [];
  const steps = [
    probeStep("a", log, { tickets: mine }),
    probeStep("b", log, { tickets: mine }),
  ];

  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const newer = [];
  let applied = 0;

  const pending = runNotebookTransition(plan(steps, {
    load: async () => { await gate; return { value: "loaded" }; },
    isCurrent: () => false,
    apply: () => { applied += 1; return { committed: true }; },
  }));

  // async 函数在第一个 await 之前同步执行，所以此刻本次 transition 的 begin 已经全部
  // 跑完；先把它这一批 ticket 定格，再让更晚的 transition 去 begin 同一批 owner。
  const minted = mine.slice();
  assert.equal(minted.length, steps.length);
  // 取数还在飞的时候，一次更晚的 transition 把同一批 owner 重新 begin 了一遍。
  for (const step of steps) newer.push(step.begin());
  release();

  const result = await pending;
  assert.deepEqual(result, { status: "superseded", at: "load" });
  assert.equal(applied, 0);
  assert.equal(log.some((entry) => entry.phase === "commit"), false);

  const settled = log.filter((entry) => entry.phase === "settle");
  assert.deepEqual(settled.map((entry) => entry.name), ["b", "a"]);
  const settledTickets = new Set(settled.map((entry) => entry.ticket));
  assert.equal(settledTickets.size, minted.length);
  for (const ticket of minted) assert.ok(settledTickets.has(ticket));
  // ③ 更晚那次 transition 建立的 ticket 一张都没被这次收尾碰到。
  for (const ticket of newer) assert.equal(settledTickets.has(ticket), false);
});


test("commit 之后被顶替（conclude 判否）：不提交 outcome，逆序回滚", async () => {
  const log = [];
  const steps = [probeStep("a", log), probeStep("b", log, { async: true })];
  const result = await runNotebookTransition(plan(steps, { conclude: () => false }));

  assert.deepEqual(result, { status: "superseded", at: "conclude" });
  // commit 已经跑过（页面状态已写入），但每个 owner 收到的是回滚。
  assert.deepEqual(
    log.filter((entry) => entry.phase === "commit").map((entry) => entry.name),
    ["a", "b"],
  );
  const settles = log.filter((entry) => entry.phase === "settle");
  assert.deepEqual(settles.map((entry) => entry.name), ["b", "a"]);
  for (const entry of settles) assert.equal(entry.outcome, null);
});


test("没有 commit 的步骤被跳过，且 settle 拿到的是自己 begin 出来的那张 ticket", async () => {
  const log = [];
  const tickets = [];
  const steps = [
    probeStep("no-commit", log, { commit: false, tickets }),
    probeStep("with-commit", log, { tickets }),
  ];
  const result = await runNotebookTransition(plan(steps));

  assert.equal(result.status, "committed");
  assert.deepEqual(
    log.filter((entry) => entry.phase === "commit").map((entry) => entry.name),
    ["with-commit"],
  );
  for (const entry of log.filter((entry) => entry.phase === "settle")) {
    assert.equal(entry.ticket.name, entry.name);
    assert.ok(tickets.includes(entry.ticket));
  }
});


test("commit 返回自定义 thenable（非原生 Promise 实例）也会被等待", async () => {
  // 鸭子类型判定的存在理由：`customThenable instanceof Promise` 为 false，
  // 但 `await customThenable` 仍会调用它的 `.then`。旧的 `instanceof Promise`
  // 判据会直接跳过等待，`conclude` 就可能跑在 `.then` 被调用之前。
  let thenCalled = false;
  const customThenable = {
    then(resolve) {
      thenCalled = true;
      resolve(undefined);
    },
  };
  const step = transitionStep({
    name: "custom-thenable",
    begin: () => ({}),
    commit: () => customThenable,
    settle: () => {},
  });
  let thenCalledBeforeConclude = false;
  const result = await runNotebookTransition(plan([step], {
    conclude: () => {
      thenCalledBeforeConclude = thenCalled;
      return true;
    },
  }));

  assert.equal(result.status, "committed");
  assert.equal(thenCalled, true, "commit 返回的自定义 thenable 必须被等待（.then 必须被调用）");
  assert.equal(thenCalledBeforeConclude, true, "conclude 必须排在 thenable 被等待之后");
});


test("enter 抛出：不取数、不提交，已 begin 的逆序回滚后原样抛出", async () => {
  const log = [];
  const steps = [probeStep("a", log), probeStep("b", log)];
  const boom = new Error("enter failed");
  let loads = 0;
  await assert.rejects(
    () => runNotebookTransition(plan(steps, {
      enter: () => { throw boom; },
      load: async () => { loads += 1; return { value: "loaded" }; },
    })),
    (error) => error === boom,
  );
  assert.equal(loads, 0);
  assert.deepEqual(
    log.filter((entry) => entry.phase === "settle").map((entry) => entry.name),
    ["b", "a"],
  );
});
