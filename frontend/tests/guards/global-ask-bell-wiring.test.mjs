// 铃铛里「进行中的全局问答」的接线守卫。
//
// 全局问答作业不属于任何一本库:它的待办条目 notebook_id 为空,点击要把全局问答
// 浮窗开到那个会话上,而不是去开一本笔记本。`page.tsx::openPendingItem` 里这条分支
// 有两处谁都编译得过、组件测试也照样全绿的坏法:
//
//  ① 全局分支挪到 `openNotebook` 那句之后。空 notebook_id 会让那句早退,点击就
//     静默什么都不做——铃铛条目「点不开」,没有任何报错。
//  ② 请求没有接到 `<GlobalAskLauncher openRequest=…>` 上。状态照样被写,却没人读,
//     结果同样是点了没反应。
//
// 判据走 `controlFlowIn` 的语句序列(AST 事实),不做跨块文本匹配。
import test from "node:test";
import assert from "node:assert/strict";

import {
  controlFlowIn,
  findFunctionIn,
  jsxElements,
  parseModule,
} from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");

/** 递归收集一段 flow 里出现的全部调用名。 */
function allCallTargets(entries) {
  const targets = [];
  for (const entry of entries) {
    targets.push(...(entry.calls ?? []).map((call) => call.target));
    for (const key of ["then", "else", "try", "catch", "finally", "body", "flow"]) {
      if (Array.isArray(entry[key])) targets.push(...allCallTargets(entry[key]));
    }
  }
  return targets;
}

test("openPendingItem 在开笔记本之前就把全局条目交给全局问答浮窗并返回", () => {
  const flow = controlFlowIn(findFunctionIn(page, "Home", "openPendingItem"));
  const globalBranch = flow.findIndex((entry) => (
    entry.kind === "if"
    && /item\.type === "ask"/.test(entry.condition)
    && /item\.scope === "global"/.test(entry.condition)
  ));
  const notebookOpen = flow.findIndex((entry) => (
    entry.kind === "if" && /openNotebook\(/.test(entry.condition)
  ));
  // 空转保护:任一锚点找不到说明入口被改名/改写,守卫必须响亮失败。
  assert.notEqual(globalBranch, -1, "找不到全局条目那条分支");
  assert.notEqual(notebookOpen, -1, "找不到开笔记本那一句");
  assert.ok(globalBranch < notebookOpen, "全局分支必须在 openNotebook 之前");

  const branch = flow[globalBranch].then;
  assert.ok(allCallTargets(branch).includes("setGlobalAskRequest"), "全局分支没有发出打开请求");
  assert.equal(branch.at(-1)?.kind, "return", "全局分支必须以 return 结束,不能落进开笔记本");
  assert.ok(!allCallTargets(branch).includes("openNotebook"), "全局分支不许去开笔记本");
});

test("全局问答浮窗读的正是 openPendingItem 写的那份请求", () => {
  const launchers = jsxElements(page, "GlobalAskLauncher");
  assert.equal(launchers.length, 1);
  assert.equal(launchers[0].bindings?.openRequest, "globalAskRequest");
});
