// 公共知识库层级切换是 workspace 内的异步写：响应回来前用户可以切到另一库。
// 本守卫用 AST 语义钉住请求 owner，防止旧库响应覆盖新库详情或在新库弹成功提示。
import test from "node:test";
import assert from "node:assert/strict";

import {
  callSitesIn,
  controlFlowIn,
  findFunction,
  ifConditionsIn,
  parseModule,
  variableInitializersIn,
} from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const compact = (value) => value?.replace(/\s+/g, "");

test("tier action binds response, refresh, and feedback to the initiating workspace", () => {
  const action = findFunction(page, "handleTierAction");
  assert.ok(action, "缺 handleTierAction");
  const initializers = variableInitializersIn(action);
  assert.ok(initializers.some((row) => (
    row.name === "notebookId" && row.initializer === "currentNotebook.id"
  )), "tier action 没有冻结发起时的 notebook id");
  assert.ok(initializers.some((row) => (
    row.name === "workspaceEpoch" && row.initializer === "workspaceEpochRef.current"
  )), "tier action 没有冻结发起时的 workspace epoch");

  const calls = callSitesIn(action);
  const ownerCheck = calls.find((call) => call.target === "workspaceRequestIsCurrent");
  assert.deepEqual(ownerCheck?.arguments, [
    "false",
    "workspaceEpoch",
    "workspaceEpochRef.current",
    "notebookId",
    "activeNotebookIdRef.current",
  ]);
  const refresh = calls.find((call) => call.target === "loadNotebookCollection");
  assert.equal(compact(refresh?.arguments[0]), "{guard:stillCurrent}");
  const commits = calls.filter((call) => call.target === "setCurrentNotebook");
  assert.equal(commits.length, 1);
  // 钉的是语义三件事：唯一那次提交是**函数式更新**、以「这一格还是发起时那本库」为
  // 条件、落的是这次写入的响应。表达式的具体写法（三元还是早退、类型断言摆在哪、
  // 可选链有没有）是实现细节——把整条三元逐字钉死会让任何无害重构报红，而它并不比
  // 下面三条更能抓住 owner 边界的退化。
  const commit = commits[0].arguments[0];
  assert.match(compact(commit), /^\(?current\)?=>/, "提交必须是函数式更新，不能直接写渲染期快照");
  assert.match(compact(commit), /current\??\.id===notebookId/, "提交必须以「还是发起时那本库」为条件");
  assert.match(commit, /\bupdated\b/, "提交必须落这次 tier 写入的响应");
  assert.equal(
    ifConditionsIn(action).filter((condition) => condition === "!stillCurrent()").length,
    2,
    "写响应后和清单刷新后都必须重新核对 owner",
  );

  const staleFailureBoundary = [
    {
      kind: "if",
      condition: "stillCurrent()",
      then: [{ kind: "throw", calls: [] }],
      else: [],
    },
    { kind: "return" },
  ];
  for (const target of ["setNotebookTier", "loadNotebookCollection"]) {
    const requestTry = controlFlowIn(action).find((step) => (
      step.kind === "try"
      && step.try.some((effect) => effect.calls?.some((call) => call.target === target))
    ));
    assert.ok(requestTry, `${target} 必须在函数内隔离旧 workspace 的失败响应`);
    assert.deepEqual(requestTry.catch, staleFailureBoundary);
  }
});
