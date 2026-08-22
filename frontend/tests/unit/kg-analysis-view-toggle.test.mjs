// 「图谱分析」弹窗与它的父视图(知识图谱)之间的开关联动守卫。
//
// 钉的是一条 React 里极容易漏的事实:**卸载不等于复位**。`kgAnalysisOpen` 挂在 page.tsx
// 的顶层组件上,弹窗只是渲染在知识图谱视图内部;父视图一关,子组件卸载,可这个 state
// 仍是 true。于是重开知识图谱会立刻自弹一次分析窗 —— 而 `notebookId` 取的是当时的
// `currentNotebookId`,中间换过库的话弹出来的还是**另一个库**的报告。
//
// 所以本文件断言三件事:
//   1. `openKgView` 在**函数体顶层**(不是某个 await 之后的分支里)把它推回 false;
//   2. `closeKgView` 同时关掉父视图与分析窗;
//   3. **`setKgViewOpen` 只允许出现在这两个函数里** —— 这一条才是移动变异的兜底:
//      任何新的内联 `onClick={() => setKgViewOpen(false)}` 都会绕开复位, 必须报红。
import test from "node:test";
import assert from "node:assert/strict";

import {
  callSitesIn,
  controlFlowIn,
  findFunction,
  parseModule,
  scopedCalls,
} from "../../test-support/semantic-source.mjs";


function callsWith(node, target, argument) {
  return callSitesIn(node).some(
    (call) => call.target === target && call.arguments[0] === argument,
  );
}


test("opening the knowledge-graph view resets the analysis modal", async () => {
  const hook = await parseModule("use-kg-workspace.ts");
  const openKgView = findFunction(hook, "openGraph");

  assert.equal(callsWith(openKgView, "setGraphOpen", "true"), true);

  // 顶层语句,而不是 try / await 之后的某个分支 —— 复位必须与打开同步发生,
  // 否则首帧仍会带着上次的弹窗渲染出来。
  const topLevelResets = controlFlowIn(openKgView).filter(
    (statement) => (statement.calls ?? []).some(
      (call) => call.target === "setAnalysisOpen" && call.arguments[0] === "false",
    ),
  );
  assert.equal(topLevelResets.length, 1);
});


test("closing the knowledge-graph view closes the analysis modal with it", async () => {
  const hook = await parseModule("use-kg-workspace.ts");
  const closeKgView = findFunction(hook, "closeGraph");

  assert.equal(callsWith(closeKgView, "setGraphOpen", "false"), true);
  assert.equal(callsWith(closeKgView, "setAnalysisOpen", "false"), true);
});


test("the knowledge-graph view toggles only through openKgView / closeKgView", async () => {
  const hook = await parseModule("use-kg-workspace.ts");
  // JSX 属性里的箭头函数不是具名声明, 它的 scope 会落在外层组件(`<module>.Home`)上 ——
  // 所以一条内联的 `onClick={() => setKgViewOpen(false)}` 在这里是看得见的。
  const scopes = scopedCalls(hook)
    .filter((call) => call.target === "setGraphOpen")
    .map((call) => call.scope)
    .sort();

  assert.deepEqual(scopes, [
    "<module>.useKgWorkspace.clearVisibleState",
    "<module>.useKgWorkspace.closeGraph",
    "<module>.useKgWorkspace.openGraph",
  ]);
});
